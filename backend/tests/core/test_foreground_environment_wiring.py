from pathlib import Path
from types import MethodType, SimpleNamespace
from typing import Any, cast

import pytest

from companion.activities.foreground_snapshot import ForegroundActivitySnapshot
from companion.background_agent.contracts import (
    BackgroundLaneAvailability,
    BackgroundWorkAvailability,
)
from companion.character_cards.models import CharacterCardDefinition
from companion.character_cards.store import CharacterCardStore
from companion.environment.models import FrozenEnvironmentSnapshot
from companion.foreground_tools.catalog.service import ForegroundToolService
from companion.llm.foreground_turn_processor import ForegroundTurnProcessor
from companion.llm.foreground_models import (
    ForegroundExecutionContext,
    ForegroundToolCall,
)
from companion.llm.output_policies import LlmProtocolLeakError
from companion.llm.request_intent import LlmInvocationIntent
from companion.runtime_memory.store import RuntimeMemoryStore
from companion.session.context import SessionContext
from companion.prompt.builder import PromptBuilder
from backend.tests.foreground_processor_factory import (
    build_foreground_processor_for_test,
)


CARDS_DIR = Path(__file__).parents[2] / "data" / "character-cards"
CHARACTER_CARD = CharacterCardStore(CARDS_DIR).load_card("xiaolian")


def _execution_context() -> ForegroundExecutionContext:
    """构造环境工具测试使用的空执行期上下文。"""

    return ForegroundExecutionContext(
        available_tools=(),
        prompt_environment={},
    )


class StopAfterPromptBuild(RuntimeError):
    """标记测试已经越过 prompt 构建边界。"""


def test_execution_context_does_not_mutate_frozen_request_facts() -> None:
    """执行期派生字段必须进入新环境，不能反向污染主进程冻结快照。"""

    agent = build_foreground_processor_for_test()
    agent._cognitive_activity_executor = None
    agent._background_agent_activity_executor = None
    agent._foreground_tools = SimpleNamespace(
        tool_schemas=lambda **_kwargs: [
            {"type": "function", "function": {"name": "start_background_work"}}
        ]
    )
    agent._session_context = SessionContext()
    frozen_foreground_context: dict[str, object] = {
        "has_base_material_inputs": True,
        "material_workload_profile": {
            "task_result_material_eligible": False,
        },
    }

    execution_context = agent._build_execution_context(
        frozen_foreground_context=frozen_foreground_context,
        interaction_session_id="interaction-a",
        request_mode="user_turn",
        foreground_text_available=True,
    )

    assert "has_material_inputs" not in frozen_foreground_context
    assert "foreground_text_available" not in frozen_foreground_context
    assert frozen_foreground_context["material_workload_profile"] == {
        "task_result_material_eligible": False,
    }
    assert execution_context.prompt_environment["has_material_inputs"] is True
    assert execution_context.prompt_environment["foreground_text_available"] is True
    assert execution_context.available_tools[0]["function"]["name"] == "start_background_work"


def test_execution_context_keeps_background_tool_and_projects_occupied_activity() -> None:
    """跨会话只投影目标摘要，普通槽占用由独立可用性事实表达。"""

    snapshot = ForegroundActivitySnapshot(
        activity_id="background-a",
        interaction_session_id="interaction-old",
        source="background_agent",
        category="后台工作",
        goal="包含客户姓名和完整路径的敏感任务目标",
        goal_summary="整理客户材料",
        state="waiting_user",
        progress_reasoning_text="不应跨会话暴露的详细进度",
        progress_sequence=3,
        previous_reports=("不应跨会话暴露的历史播报",),
    )
    agent = build_foreground_processor_for_test()
    agent._cognitive_activity_executor = None
    agent._background_agent_activity_executor = SimpleNamespace(
        activity_snapshots=lambda _interaction_session_id: (snapshot,),
        background_availability_snapshot=lambda: BackgroundWorkAvailability(
            ordinary=BackgroundLaneAvailability(
                can_accept_submission=False,
                busy=True,
                runner_available=True,
                configured=True,
                shutdown=False,
            ),
            recovery=BackgroundLaneAvailability(
                can_accept_submission=True,
                busy=False,
                runner_available=True,
                configured=True,
                shutdown=False,
            ),
            active_count=1,
        ),
    )
    agent._foreground_tools = ForegroundToolService()
    agent._session_context = SessionContext()

    execution_context = agent._build_execution_context(
        frozen_foreground_context={},
        interaction_session_id="interaction-new",
        request_mode="user_turn",
        foreground_text_available=False,
    )

    tool_names = {
        str(tool["function"]["name"])
        for tool in execution_context.available_tools
    }
    assert "start_background_work" in tool_names
    projected_activity = execution_context.prompt_environment["active_activities"][0]
    assert projected_activity["activity_id"] == ""
    assert projected_activity["interaction_session_id"] == "interaction-new"
    assert projected_activity["goal"] == "整理客户材料"
    assert projected_activity["progress_reasoning_text"] == ""
    assert projected_activity["previous_reports"] == []
    assert "敏感任务目标" not in str(execution_context.prompt_environment)
    availability = execution_context.prompt_environment[
        "background_work_availability"
    ]
    assert availability["ordinary_can_accept"] is False
    assert availability["ordinary_busy"] is True
    assert availability["recovery_busy"] is False
    assert availability["active_count"] == 1
    assert availability["queue_supported"] is False
    assert "correct_cognitive_activity" not in tool_names
    assert "correct_background_work" not in tool_names
    assert "background_work_status" not in execution_context.prompt_environment


def test_execution_context_uses_live_activity_for_prompt_and_correction_tools() -> None:
    """同一份实时快照必须同时驱动 Prompt 和纠正工具，不能先生成空 schema。"""

    snapshot = ForegroundActivitySnapshot(
        activity_id="cognitive-live",
        interaction_session_id="interaction-a",
        source="cognitive",
        category="深度分析",
        goal="错误目标",
        state="running",
    )
    agent = build_foreground_processor_for_test()
    agent._cognitive_activity_executor = SimpleNamespace(
        activity_snapshots=lambda _interaction_session_id: (snapshot,)
    )
    agent._background_agent_activity_executor = None
    agent._foreground_tools = ForegroundToolService()

    execution_context = agent._build_execution_context(
        frozen_foreground_context={"active_activities": []},
        interaction_session_id="interaction-a",
        request_mode="user_turn",
        foreground_text_available=False,
    )

    tool_by_name = {
        str(tool["function"]["name"]): tool
        for tool in execution_context.available_tools
    }
    assert execution_context.prompt_environment["active_activities"][0][
        "activity_id"
    ] == "cognitive-live"
    assert tool_by_name["correct_cognitive_activity"]["function"]["parameters"][
        "properties"
    ]["activity_id"]["enum"] == ["cognitive-live"]
    assert "correct_background_work" not in tool_by_name


def test_execution_context_drops_stale_frozen_activity_when_live_executor_is_empty() -> None:
    """实时 Executor 已无任务时，旧冻结快照不能继续暴露纠正入口。"""

    agent = build_foreground_processor_for_test(prompt_builder=PromptBuilder())
    agent._cognitive_activity_executor = SimpleNamespace(
        activity_snapshots=lambda _interaction_session_id: ()
    )
    agent._background_agent_activity_executor = None
    agent._foreground_tools = ForegroundToolService()

    execution_context = agent._build_execution_context(
        frozen_foreground_context={
            "active_activities": [
                {
                    "activity_id": "cognitive-stale",
                    "interaction_session_id": "interaction-a",
                    "source": "cognitive",
                    "goal": "已经结束的旧目标",
                    "state": "running",
                    "phase": "reasoning",
                }
            ]
        },
        interaction_session_id="interaction-a",
        request_mode="user_turn",
        foreground_text_available=False,
    )

    tool_names = {
        str(tool["function"]["name"])
        for tool in execution_context.available_tools
    }
    assert execution_context.prompt_environment["active_activities"] == []
    assert "correct_cognitive_activity" not in tool_names
    assert "correct_background_work" not in tool_names

    prompt_messages = agent._build_prompt_messages(
        current_user_text="继续处理",
        current_user_message=None,
        character_card=CHARACTER_CARD,
        user_call_name="用户",
        user_self_intro="",
        user_extra_notes="",
        streamvox_model="",
        current_time="",
        foreground_context=execution_context.prompt_environment,
        request_mode="user_turn",
        available_tool_names=frozenset(tool_names),
        correctable_activities=execution_context.correctable_activities,
        current_text_materials=[],
    )
    rendered_prompt = "\n".join(
        str(message.get("content") or "") for message in prompt_messages
    )
    assert "correct_cognitive_activity" not in rendered_prompt
    assert "correct_background_work" not in rendered_prompt


def test_unknown_terminal_event_does_not_create_task_memory() -> None:
    """主进程只接受明确登记的 Cognitive 和后台工作终态。"""

    store = RuntimeMemoryStore()

    record = store.record_task_terminal(
        "future_background_source.completed",
        {"activity_id": "future-a", "goal": "未知任务"},
    )

    assert record is None
    assert store.snapshot().recent_messages == ()


class StopClient:
    """在 prompt 构建完成后终止模型调用的测试客户端。"""

    def stream_message(self, **_: object) -> list[object]:
        """阻止测试进入真实模型流。

        Returns:
            不返回流；始终抛出测试终止异常。
        """

        raise StopAfterPromptBuild


class EmptyForegroundTools:
    """提供空工具声明，隔离本测试与工具目录。"""

    def tool_schemas(self, **_kwargs: object) -> list[dict[str, Any]]:
        """返回空工具列表。

        Returns:
            空工具声明列表。
        """

        return []


class ProtocolLeakClient:
    """模拟厂商协议守卫拒绝 system notice 输出。"""

    def __init__(self) -> None:
        self.intent: object | None = None

    def stream_message(self, **kwargs: object):  # type: ignore[no-untyped-def]
        """记录工具策略并在消费流时抛出协议异常。

        Args:
            **kwargs: ForegroundTurnProcessor 传入的模型调用参数。
        """

        self.intent = kwargs.get("intent")
        if False:
            yield None
        raise LlmProtocolLeakError(provider_id="deepseek", protocol="dsml")


class ImmediateProtocolLeakClient:
    """模拟客户端在返回迭代器前直接报告协议泄漏。"""

    def __init__(self) -> None:
        self.intent: object | None = None

    def stream_message(self, **kwargs: object) -> list[object]:
        """记录工具策略并立即抛出协议异常。

        Args:
            **kwargs: ForegroundTurnProcessor 传入的模型调用参数。

        Returns:
            本方法不会正常返回。

        Raises:
            LlmProtocolLeakError: 始终模拟厂商协议泄漏。
        """

        self.intent = kwargs.get("intent")
        raise LlmProtocolLeakError(provider_id="deepseek", protocol="dsml")


def test_stream_user_text_only_forwards_environment_metadata_to_prompt_builder() -> None:
    """Prompt 构建只接收环境元数据，不接收原始冻结快照。"""

    snapshot = FrozenEnvironmentSnapshot(
        snapshot_id="environment-a",
        captured_at="2026-07-12T14:34:00+08:00",
        active_window_title="Code",
        screenshot_mime_type="image/png",
        screenshot_bytes=b"turn-a",
        capture_error=None,
    )
    captured_arguments: dict[str, object] = {}
    agent = build_foreground_processor_for_test()
    agent._session_context = SessionContext()
    agent._foreground_tools = EmptyForegroundTools()
    agent._client = StopClient()

    def capture_prompt_arguments(_self: ForegroundTurnProcessor, **kwargs: object) -> list[dict[str, Any]]:
        captured_arguments.update(kwargs)
        return []

    agent._build_prompt_messages = MethodType(capture_prompt_arguments, agent)  # type: ignore[method-assign]

    events = agent.stream_user_text(
        "你好",
        now=0.0,
        interaction_session_id="interaction-a",
        character_card=cast(CharacterCardDefinition, None),
        user_call_name="用户",
        user_self_intro="",
        user_extra_notes="",
        foreground_context={"environment_snapshot": snapshot.to_payload()},
        environment_snapshot=snapshot,
    )

    with pytest.raises(StopAfterPromptBuild):
        next(iter(events))

    assert "environment_snapshot" not in captured_arguments
    captured_foreground_context = captured_arguments["foreground_context"]
    assert isinstance(captured_foreground_context, dict)
    assert captured_foreground_context["environment_snapshot"] == {**snapshot.to_payload(), "screen_available": False}
    assert captured_arguments["frozen_visual_input"].limitations


@pytest.mark.parametrize(
    "client",
    [ProtocolLeakClient(), ImmediateProtocolLeakClient()],
    ids=["stream_iteration", "stream_creation"],
)
def test_system_notice_protocol_leak_returns_empty_failed_final(
    client: ProtocolLeakClient | ImmediateProtocolLeakClient,
) -> None:
    """协议泄漏不得产生可见文本，并应进入 notice 专用失败闭环。"""

    agent = build_foreground_processor_for_test()
    agent._session_context = SessionContext(token_counter=len)
    agent._foreground_tools = EmptyForegroundTools()
    agent._client = client
    agent._cognitive_activity_executor = None
    agent._background_agent_activity_executor = None
    agent._build_prompt_messages = MethodType(  # type: ignore[method-assign]
        lambda _self, **_kwargs: [{"role": "system", "content": "notice"}],
        agent,
    )

    events = list(
        agent.stream_user_text(
            "请汇报当前提醒。",
            now=0.0,
            interaction_session_id="interaction-a",
            character_card=cast(CharacterCardDefinition, None),
            user_call_name="用户",
            user_self_intro="",
            user_extra_notes="",
            foreground_context={
                "notice_batch": [
                    {
                        "activity_id": "cognitive-a",
                        "source": "cognitive",
                        "intent": "cognitive_result",
                        "category": "当前画面理解",
                        "goal": "描述图片",
                        "result_context": "图片里有一位少女。",
                    }
                ]
            },
            request_mode="system_notice",
        )
    )

    assert client.intent == LlmInvocationIntent.text()
    assert [event.type for event in events] == ["assistant.text_final"]
    assert events[0].payload == {
        "text": "",
        "notice_delivery_failed": True,
        "notice_failure_reason": "provider_protocol_leak",
    }
    assert agent._session_context.recent_messages() == []


def test_stream_user_text_loads_small_text_only_for_current_prompt(tmp_path: Path) -> None:
    """验证完整小文本只传给当前 prompt，历史仅记录材料事实摘要。"""

    path = tmp_path / "notes.txt"
    path.write_text("当前材料正文", encoding="utf-8")
    captured_arguments: dict[str, object] = {}
    agent = build_foreground_processor_for_test()
    agent._session_context = SessionContext()
    agent._foreground_tools = EmptyForegroundTools()
    agent._client = StopClient()

    def capture_prompt_arguments(_self: ForegroundTurnProcessor, **kwargs: object) -> list[dict[str, Any]]:
        captured_arguments.update(kwargs)
        return []

    agent._build_prompt_messages = MethodType(capture_prompt_arguments, agent)  # type: ignore[method-assign]
    foreground_context = {
        "active_turn_material_context": {
            "attachments": [{
                "material_id": "material-a",
                "display_name": "notes.txt",
                "path": str(path),
                "media_class": "text",
            }],
        },
        "turn_material_summary": {
            "attachment_count": 1,
            "shown_count": 1,
            "omitted_count": 0,
            "items": [{
                "display_name": "notes.txt",
                "material_kind": "文本材料",
                "image_path": str(path),
            }],
        },
    }

    events = agent.stream_user_text(
        "解释这份材料",
        now=0.0,
        interaction_session_id="interaction-a",
        character_card=cast(CharacterCardDefinition, None),
        user_call_name="用户",
        user_self_intro="",
        user_extra_notes="",
        foreground_context=foreground_context,
    )

    with pytest.raises(StopAfterPromptBuild):
        next(iter(events))

    assert captured_arguments["current_text_materials"] == [{
        "material_id": "material-a",
        "display_name": "notes.txt",
        "text": "当前材料正文",
    }]
    recorded = agent._session_context.recent_messages()[0]
    assert recorded.content == "解释这份材料"
    assert "当前材料正文" not in recorded.llm_visible_content()
    assert "notes.txt" in recorded.llm_visible_content()
    assert str(path) not in recorded.llm_visible_content()


def test_handle_tool_calls_forwards_frozen_environment_to_cognitive_executor() -> None:
    """工具分支必须把冻结环境快照继续透传给认知工具执行。"""

    snapshot = FrozenEnvironmentSnapshot(
        snapshot_id="environment-a",
        captured_at="2026-07-12T14:34:00+08:00",
        active_window_title="Code",
        screenshot_mime_type="image/png",
        screenshot_bytes=b"turn-a",
        capture_error=None,
    )
    captured_arguments: dict[str, object] = {}
    agent = build_foreground_processor_for_test()
    agent._session_context = SessionContext()

    class EmptyClient:
        def stream_message(self, **_: object) -> list[object]:
            return []

    agent._client = EmptyClient()

    def capture_cognitive_calls(
        _self: ForegroundTurnProcessor,
        **kwargs: object,
    ) -> tuple[dict[str, object], ...]:
        captured_arguments.update(kwargs)
        return ({
            "state": "accepted",
            "activity_id": "task-a",
            "kind": "lookup_current_fact",
        },)

    agent._execute_cognitive_tool_calls = MethodType(capture_cognitive_calls, agent)  # type: ignore[method-assign]
    agent._finish_tool_result_text_stream = MethodType(lambda _self, **_kwargs: [], agent)  # type: ignore[method-assign]
    agent._batch_waiting_phrase_events = MethodType(lambda _self, **_kwargs: [], agent)  # type: ignore[method-assign]

    events = list(agent._handle_tool_calls(
        tool_calls=[ForegroundToolCall(
            tool_call_id="tool-call-a",
            name="lookup_current_fact",
            arguments={"brief": "确认当前画面内容"},
        )],
        character_card=CHARACTER_CARD,
        streamvox_model="",
        current_user_text="看看现在屏幕上是什么",
        current_time="2026-09-05 13:25:00 UTC+08:00",
        current_user_message=None,
        interaction_session_id="interaction-a",
        execution_context=_execution_context(),
        environment_snapshot=snapshot,
        tool_followup_visible_dialogue=(),
    ))

    assert captured_arguments["frozen_visual_input"].images == ()
    assert captured_arguments["tool_calls"] == (ForegroundToolCall(
        tool_call_id="tool-call-a",
        name="lookup_current_fact",
        arguments={"brief": "确认当前画面内容"},
    ),)
    assert any(event.type == "llm.foreground.tool_call_completed" for event in events)


def test_start_background_work_forwards_only_frozen_scene_bytes() -> None:
    """后台任务只接收截图原始字节，不接收活动窗口元数据。"""

    snapshot = FrozenEnvironmentSnapshot(
        snapshot_id="environment-a",
        captured_at="2026-07-12T14:34:00+08:00",
        active_window_title="绝不应传给后台",
        screenshot_mime_type="image/png",
        screenshot_bytes=b"frozen-scene-bytes",
        capture_error=None,
    )
    captured: dict[str, object] = {}

    class RecordingExecutor:
        def prepare(self, **kwargs: object) -> tuple[bool, list[dict[str, object]]]:
            captured.update(kwargs)
            return True, [
                {
                    "type": "background_agent.prepared",
                    "payload": {"activity_id": "activity-a"},
                }
            ]

    agent = build_foreground_processor_for_test(
        background_agent_activity_executor=RecordingExecutor()
    )
    result = agent._background_agent_tool_executor.execute_start_background_work(
        execution_lane="ordinary",
        tool_call=ForegroundToolCall(
            tool_call_id="tool-call-a",
            name="start_background_work",
            arguments={
                "goal": "翻译当前屏幕上的文档",
                "deliverable": True,
                "include_current_scene": True,
            },
        ),
        interaction_session_id="interaction-a",
        foreground_context={"active_turn_material_context": {"items": []}},
        environment_snapshot=snapshot,
        protocol_transaction_id="tool-scene",
    )

    assert result == {
        "state": "accepted",
        "activity_id": "activity-a",
        "goal": "翻译当前屏幕上的文档",
        "deliverable": True,
    }
    assert captured["current_scene_bytes"] == b"frozen-scene-bytes"
    assert "active_window_title" not in captured


def test_start_background_work_rejects_missing_current_scene() -> None:
    """要求现场输入但截图不可用时，Foreground 必须拒绝空任务。"""

    class FailingIfCalledExecutor:
        def prepare(self, **_kwargs: object) -> tuple[bool, list[dict[str, object]]]:
            raise AssertionError("截图缺失时不应进入后台执行器")

    agent = build_foreground_processor_for_test(
        background_agent_activity_executor=FailingIfCalledExecutor()
    )
    result = agent._background_agent_tool_executor.execute_start_background_work(
        execution_lane="ordinary",
        tool_call=ForegroundToolCall(
            tool_call_id="tool-call-a",
            name="start_background_work",
            arguments={
                "goal": "翻译当前屏幕上的文档",
                "deliverable": True,
                "include_current_scene": True,
            },
        ),
        interaction_session_id="interaction-a",
        foreground_context={},
        environment_snapshot=None,
        protocol_transaction_id="tool-missing-scene",
    )

    assert result["state"] == "rejected"
    assert result["reason"] == "current_scene_unavailable"
