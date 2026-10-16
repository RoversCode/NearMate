from __future__ import annotations

from threading import Event
from types import SimpleNamespace

import pytest

from companion.foreground_tools.cognitive.schemas import (
    ANALYZE_DEEPLY_TOOL,
    cognitive_tool_schemas,
)
from companion.foreground_tools.cognitive.tools import (
    CognitiveToolContext,
    CognitiveToolExecutor,
)
from companion.llm.foreground_models import ForegroundToolCall
from companion.materials.foreground_images import VisualEvidence


def test_lookup_and_analysis_share_code_prepared_visual_evidence() -> None:
    """核查与分析合并一次请求，直接继承代码提供的图片。"""

    activity = RecordingActivityExecutor()
    results = CognitiveToolExecutor(activity_executor=activity).execute_many(
        (_call("lookup", "lookup_current_fact", {"brief": "核查图中商品当前价格"}),
         _call("analysis", "analyze_deeply", {"brief": "深入分析两款商品的差异"})),
        context=_context(),
    )
    assert [item["state"] for item in results] == ["accepted", "merged"]
    assert len(activity.plans) == 1
    assert activity.plans[0].images == _context().images
    assert activity.plans[0].reasoning == "high"
    assert activity.plans[0].web_search is True


@pytest.mark.parametrize("field,value", [
    ("image_paths", ["C:/private.png"]),
    ("image_urls", ["https://example.com/private.png"]),
    ("include_current_scene", True),
])
def test_model_cannot_select_or_replace_frozen_visual_evidence(field: str, value: object) -> None:
    """已退休的图片选择参数必须被拒绝，不能触发其它路径读取。"""

    activity = RecordingActivityExecutor()
    result = CognitiveToolExecutor(activity_executor=activity).execute_many(
        (_call("lookup", "lookup_current_fact", {"brief": "核查", field: value}),),
        context=_context(),
    )
    assert result[0]["reason"] == "invalid_cognitive_arguments"
    assert not activity.plans


def test_cognitive_catalog_has_only_lookup_and_analysis_without_image_selectors() -> None:
    """模型只决定认知目标，图片由本轮冻结输入自动传递。"""

    schemas = {item["function"]["name"]: item["function"] for item in cognitive_tool_schemas()}
    assert set(schemas) == {"lookup_current_fact", "analyze_deeply"}
    for schema in schemas.values():
        assert not {"image_paths", "image_urls", "include_current_scene"} & set(schema["parameters"]["properties"])


def test_retired_scene_tool_cannot_prepare_an_activity() -> None:
    """纯视觉工具已删除，误发旧工具名也不能创建活动。"""

    activity = RecordingActivityExecutor()
    result = CognitiveToolExecutor(activity_executor=activity).execute_many(
        (_call("old", "inspect_current_scene", {"brief": "读图"}),), context=_context(),
    )
    assert result[0]["state"] == "rejected"
    assert not activity.plans


class RecordingActivityExecutor:
    """记录 Cognitive plan 提交的测试 Activity Executor。"""

    def __init__(self) -> None:
        """初始化提交记录。"""

        self.plans: list[object] = []

    def snapshot(self, _session_id: str) -> object:
        """返回空闲任务状态。

        Args:
            _session_id: 当前会话编号。

        Returns:
            空闲状态快照。
        """

        return SimpleNamespace(state="none", activity_id=None, to_payload=lambda: {})

    def prepare(
        self,
        *,
        interaction_session_id: str,
        plan: object,
        source_user_text: str,
    ) -> tuple[bool, list[dict[str, object]]]:
        """记录唯一计划并返回 accepted。

        Args:
            interaction_session_id: 当前会话编号。
            plan: 合并后的认知计划。
            source_user_text: 当前用户原话。

        Returns:
            模拟 Activity Executor 接收结果。
        """

        del interaction_session_id, source_user_text
        self.plans.append(plan)
        return True, [{
            "type": "cognitive_task.prepared",
            "payload": {"activity_id": "cognitive-a"},
        }]






def test_cognitive_response_goal_is_optional_and_preserved_in_plan() -> None:
    """跨轮讨论目的应独立保留，直接问题不需要填写该字段。"""

    activity_executor = RecordingActivityExecutor()
    executor = CognitiveToolExecutor(activity_executor=activity_executor)

    executor.execute_many(
        (
            _call(
                "lookup-a",
                "lookup_current_fact",
                {
                    "brief": "核查周翊然的出生年份",
                    "response_goal": "回应他是否更适合与田曦薇搭档的讨论。",
                },
            ),
            _call("lookup-b", "lookup_current_fact", {"brief": "核查周翊然几岁"}),
        ),
        context=_context(),
    )

    goals = activity_executor.plans[0].goals
    assert goals[0].response_goal == "回应他是否更适合与田曦薇搭档的讨论。"
    assert goals[1].response_goal == ""






def test_cross_turn_running_task_rejects_new_cognitive_batch() -> None:
    """同 session 已有 prepared/running 任务时，新一轮认知目标必须保持 already_running。"""

    activity_executor = RecordingActivityExecutor()
    activity_executor.snapshot = lambda _session_id: SimpleNamespace(
        state="running",
        activity_id="cognitive-running",
        to_payload=lambda: {
            "activity_id": "cognitive-running",
            "kind": "lookup_current_fact",
            "brief": "旧查询",
        },
    )
    executor = CognitiveToolExecutor(
        activity_executor=activity_executor,
    )

    results = executor.execute_many(
        (_call("lookup-new", "lookup_current_fact", {"brief": "新查询"}),),
        context=_context(),
    )

    assert activity_executor.plans == []
    assert results[0]["state"] == "already_running"
    assert results[0]["activity_id"] == "cognitive-running"


def test_cancelled_generation_never_prepares_cognitive_task() -> None:
    """图片准备和 Activity Executor prepare 前已经取消时，不得创建任何后台占位。"""

    cancellation = Event()
    cancellation.set()
    activity_executor = RecordingActivityExecutor()
    executor = CognitiveToolExecutor(
        activity_executor=activity_executor,
    )
    base_context = _context()
    context = CognitiveToolContext(
        interaction_session_id=base_context.interaction_session_id,
        source_user_message_id=base_context.source_user_message_id,
        source_user_text=base_context.source_user_text,
        foreground_context=base_context.foreground_context,
        images=base_context.images,
        cancellation_event=cancellation,
    )

    results = executor.execute_many(
        (_call("inspect-a", "lookup_current_fact", {"brief": "看屏幕"}),),
        context=context,
    )

    assert activity_executor.plans == []
    assert results[0]["state"] == "rejected"
    assert results[0]["reason"] == "generation_cancelled"




def test_invalid_lookup_url_is_dropped_without_rejecting_merged_goal() -> None:
    """非法 URL 只应被丢弃，lookup 与深度分析仍共享截图并合并执行。"""

    activity_executor = RecordingActivityExecutor()
    executor = CognitiveToolExecutor(
        activity_executor=activity_executor,
    )

    results = executor.execute_many(
        (
            _call(
                "lookup-a",
                "lookup_current_fact",
                {
                    "brief": "核查屏幕内容和相关资料",
                    "source_urls": ["not-a-url"],
                },
            ),
            _call("analyze-a", "analyze_deeply", {"brief": "深入分析这件事"}),
        ),
        context=_context(),
    )

    assert len(activity_executor.plans) == 1
    plan = activity_executor.plans[0]
    assert [goal.kind for goal in plan.goals] == [
        "lookup_current_fact",
        "analyze_deeply",
    ]
    assert plan.reasoning == "high"
    assert plan.web_search is True
    assert plan.web_extract is False
    assert plan.source_urls == ()
    assert plan.encoded_images == ("data:image/png;base64,screen",)
    assert plan.images[0].source == "desktop"
    assert plan.images[0].label == "桌面截图，采集于 2026-09-13 10:00"
    assert [result["state"] for result in results] == ["accepted", "merged"]


def test_valid_lookup_url_enables_extract_in_merged_plan() -> None:
    """合法 URL 应直接进入合并计划并开启网页提取能力。"""

    activity_executor = RecordingActivityExecutor()
    executor = CognitiveToolExecutor(
        activity_executor=activity_executor,
    )
    context = CognitiveToolContext(
        interaction_session_id="session-a",
        source_user_message_id=1,
        source_user_text="核查 https://allowed.example/article 并深入分析",
        foreground_context={},
    )

    results = executor.execute_many(
        (
            _call(
                "lookup-a",
                "lookup_current_fact",
                {
                    "brief": "核查网页事实",
                    "source_urls": ["https://allowed.example/article"],
                },
            ),
            _call("analyze-a", "analyze_deeply", {"brief": "深入分析网页观点"}),
        ),
        context=context,
    )

    plan = activity_executor.plans[0]
    assert plan.reasoning == "high"
    assert plan.web_search is True
    assert plan.web_extract is True
    assert plan.source_urls == ("https://allowed.example/article",)
    assert [result["state"] for result in results] == ["accepted", "merged"]








def test_deep_analysis_does_not_extract_web_references_without_lookup_goal() -> None:
    """单独深度分析不得自动读取当前轮网页 URL 或抢占材料职责。"""

    activity_executor = RecordingActivityExecutor()
    executor = CognitiveToolExecutor(
        activity_executor=activity_executor,
    )
    context = CognitiveToolContext(
        interaction_session_id="session-a",
        source_user_message_id=1,
        source_user_text="深入分析这个观点",
        foreground_context={},
    )

    results = executor.execute_many(
        (_call("analyze-a", "analyze_deeply", {"brief": "分析这个观点"}),),
        context=context,
    )

    plan = activity_executor.plans[0]
    assert plan.source_urls == ()
    assert plan.web_extract is False
    assert results[0]["state"] == "accepted"




def test_cognitive_acceptance_relies_on_state_without_reply_message() -> None:
    """认知接受结果只保存事务事实，等待反馈由续写状态协议确定。"""

    activity_executor = RecordingActivityExecutor()
    executor = CognitiveToolExecutor(activity_executor=activity_executor)
    context = CognitiveToolContext(
        interaction_session_id="interaction-a",
        source_user_message_id=1,
        source_user_text="查一下 Kimi 新模型",
        foreground_context={},
    )

    results = executor.execute_many(
        (_call("lookup-a", "lookup_current_fact", {"brief": "核查 Kimi 新模型"}),),
        context=context,
    )

    assert results[0]["state"] == "accepted"
    assert results[0]["activity_id"] == "cognitive-a"
    assert "instruction" not in results[0]
    assert "safe_message" not in results[0]


def test_cognitive_derivation_never_reuses_pixels_from_task_memory() -> None:
    """同源派生应复用图片证据，但父任务文本上下文不能重复携带图片编码。"""

    activity_executor = RecordingActivityExecutor()
    executor = CognitiveToolExecutor(
        activity_executor=activity_executor,
    )
    parent_result = {
        "state": "completed",
        "activity_id": "cognitive-parent",
        "source": "cognitive",
        "goal": "识别旧截图",
        "terminal_state": "completed",
        "outcome_brief": "旧截图里有一个报错。",
        "result_text": "旧截图报错代码为 E100。",
        "output_paths": [],
        "safe_interactions": [],
        "original_character_count": 600_000,
        "returned_character_count": 500_000,
        "truncated": True,
        "input_snapshot": {
            "source_urls": ["https://example.com/error"],
            "encoded_images": ["data:image/png;base64,parent-image"],
        },
    }
    context = CognitiveToolContext(
        interaction_session_id="session-a",
        source_user_message_id=2,
        source_user_text="继续分析旧截图",
        foreground_context={},
        task_memory_lookup=lambda _activity_id, _cancellation: dict(parent_result),
    )

    results = executor.execute_many(
        (
            _call(
                "analyze-derived",
                "analyze_deeply",
                {
                    "brief": "分析旧报错的原因",
                    "based_on_activity_id": "cognitive-parent",
                },
            ),
        ),
        context=context,
    )

    plan = activity_executor.plans[0]
    assert results[0]["based_on_activity_id"] == "cognitive-parent"
    assert plan.based_on_activity_id == "cognitive-parent"
    assert plan.images == ()
    assert plan.source_urls == ("https://example.com/error",)
    # 父任务上下文只保留新任务真正需要的事实：图片 data URL、内部编号、
    # 召回计数和旧任务执行参数一律不得进入这份文本投影。
    assert plan.parent_task_context == {
        "source": "cognitive",
        "goal": "识别旧截图",
        "terminal_state": "completed",
        "outcome_brief": "旧截图里有一个报错。",
        "result_text": "旧截图报错代码为 E100。",
        "truncated": True,
        "source_urls": ["https://example.com/error"],
    }


def test_cognitive_derivation_accepts_cross_source_background_parent() -> None:
    """Cognitive 可以基于后台任务结果衍生新分析，且不会修改父任务。"""

    activity_executor = RecordingActivityExecutor()
    executor = CognitiveToolExecutor(activity_executor=activity_executor)
    context = CognitiveToolContext(
        interaction_session_id="session-a",
        source_user_message_id=2,
        source_user_text="分析刚才生成的报告",
        foreground_context={},
        task_memory_lookup=lambda _activity_id, _cancellation: {
            "state": "completed",
            "activity_id": "background-parent",
            "source": "background_agent",
            "goal": "生成报告",
            "terminal_state": "completed",
            "outcome_brief": "报告已经生成。",
            "result_text": "报告正文",
            "output_paths": [r"C:\outputs\report.md"],
            "safe_interactions": [],
            "input_snapshot": {
                "materials": [{"local_path": r"C:\inputs\source.txt"}]
            },
        },
    )

    results = executor.execute_many(
        (
            _call(
                "analyze-report",
                "analyze_deeply",
                {
                    "brief": "分析报告中的结论",
                    "based_on_activity_id": "background-parent",
                },
            ),
        ),
        context=context,
    )

    assert results[0]["state"] == "accepted"
    assert activity_executor.plans[0].parent_task_context["source"] == "background_agent"


def test_missing_parent_rejects_cognitive_derivation_before_prepare() -> None:
    """父任务详情已被压缩后，派生调用必须在创建 Activity 前明确拒绝。"""

    activity_executor = RecordingActivityExecutor()
    executor = CognitiveToolExecutor(activity_executor=activity_executor)
    context = CognitiveToolContext(
        interaction_session_id="session-a",
        source_user_message_id=2,
        source_user_text="继续旧任务",
        foreground_context={},
        task_memory_lookup=lambda _activity_id, _cancellation: {
            "state": "not_available",
            "reason": "task_memory_compacted",
            "safe_message": "旧任务详情已经不在本次运行记忆中。",
        },
    )

    results = executor.execute_many(
        (
            _call(
                "analyze-missing",
                "analyze_deeply",
                {
                    "brief": "继续分析",
                    "based_on_activity_id": "missing-parent",
                },
            ),
        ),
        context=context,
    )

    assert activity_executor.plans == []
    assert results[0]["state"] == "rejected"
    assert results[0]["reason"] == "task_memory_compacted"


def _call(
    tool_call_id: str,
    name: str,
    arguments: dict[str, object],
) -> ForegroundToolCall:
    """构造测试认知工具调用。

    Args:
        tool_call_id: 工具调用编号。
        name: 工具名称。
        arguments: 工具参数。

    Returns:
        Foreground 工具调用对象。
    """

    return ForegroundToolCall(tool_call_id, name, arguments)


def _context() -> CognitiveToolContext:
    """构造带当前屏幕的认知工具上下文。

    Returns:
        固定测试上下文。
    """

    return CognitiveToolContext(
        interaction_session_id="session-a",
        source_user_message_id=1,
        source_user_text="测试请求",
        foreground_context={},
        images=(
            VisualEvidence("desktop", "桌面截图，采集于 2026-09-13 10:00", "data:image/png;base64,screen"),
        ),
    )
