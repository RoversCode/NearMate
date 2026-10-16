from __future__ import annotations

from companion.foreground_tools.catalog.service import ForegroundToolService
from companion.foreground_tools.activity_correction.policy import (
    CorrectableActivityContext,
    select_correctable_activity_contexts,
)


def test_start_background_work_is_a_stable_user_turn_capability() -> None:
    """普通用户轮始终看得到后台入口，配置状态只由 prepare 裁决。"""

    assert "start_background_work" in _tool_names(
        ForegroundToolService().tool_schemas()
    )


def test_start_background_work_declares_stable_v1_contract() -> None:
    schemas = ForegroundToolService().tool_schemas()
    schema = next(
        item for item in schemas if item["function"]["name"] == "start_background_work"
    )
    parameters = schema["function"]["parameters"]

    assert parameters["required"] == ["goal", "deliverable"]
    assert set(parameters["properties"]) == {
        "goal",
        "deliverable",
        "requirements",
        "include_current_scene",
        "material_paths",
        "based_on_activity_id",
    }
    description = schema["function"]["description"]
    assert "只打开一个 Windows 已安装应用本身" in description
    assert "不能先预开应用" in description


def test_start_background_work_remains_visible_when_the_single_slot_is_occupied() -> None:
    """槽位占用由 Activity 约束表达，不能通过删工具让模型误判能力消失。"""

    service = ForegroundToolService()

    assert "start_background_work" in _tool_names(service.tool_schemas())


def test_tool_schema_result_cannot_mutate_later_turns() -> None:
    """调用方改动本轮 schema 列表时，下一轮仍要得到完整稳定工具目录。"""

    service = ForegroundToolService()
    first_turn_schemas = service.tool_schemas()
    first_turn_schemas.clear()

    assert "start_background_work" in _tool_names(service.tool_schemas())


def test_respond_to_work_interaction_only_appears_with_pending_interaction() -> None:
    """统一回答工具随当前权限或资料请求动态注册，不受后台凭据开关影响。"""

    service = ForegroundToolService()

    assert "respond_to_work_interaction" not in _tool_names(service.tool_schemas())
    schemas = service.tool_schemas(has_pending_work_interaction=True)
    assert "respond_to_work_interaction" in _tool_names(schemas)
    schema = next(
        item for item in schemas
        if item["function"]["name"] == "respond_to_work_interaction"
    )
    parameters = schema["function"]["parameters"]
    assert parameters["required"] == ["request_id", "outcome"]
    assert set(parameters["properties"]) == {"request_id", "outcome", "answers"}


def test_activity_correction_tools_only_appear_for_correctable_activity() -> None:
    """纠正工具只属于仍可中断的活跃任务，不是稳定常驻能力。"""

    service = ForegroundToolService()
    assert not {
        "correct_cognitive_activity",
        "correct_background_work",
    } & _tool_names(service.tool_schemas())

    for state in ("waiting_user", "attention"):
        schemas = service.tool_schemas(
            correctable_activities=(
                CorrectableActivityContext(
                    activity_id="activity-a",
                    source="background_agent",
                    interaction_session_id="session-a",
                    state=state,
                    phase="reasoning",
                    goal="错误目标",
                ),
            )
        )

        assert "correct_cognitive_activity" not in _tool_names(schemas)
        assert "correct_background_work" in _tool_names(schemas)
        background_schema = next(
            item for item in schemas
            if item["function"]["name"] == "correct_background_work"
        )
        assert "后台工作" in background_schema["function"]["description"]
        assert "尚未结束" in background_schema["function"]["description"]
        assert "Claude" not in background_schema["function"]["description"]
        activity_id_schema = (
            background_schema["function"]["parameters"]["properties"]["activity_id"]
        )
        assert activity_id_schema["enum"] == ["activity-a"]


def test_correction_tools_keep_ids_in_their_own_source_family() -> None:
    """两类活跃任务同时存在时，每个纠正工具只能看到同源编号。"""

    schemas = ForegroundToolService().tool_schemas(
        correctable_activities=(
            CorrectableActivityContext(
                activity_id="cognitive-a",
                source="cognitive",
                interaction_session_id="session-a",
                state="running",
                phase="reasoning",
                goal="认知旧目标",
                cognitive_tool_types=("analyze_deeply",),
            ),
            CorrectableActivityContext(
                activity_id="background-a",
                source="background_agent",
                interaction_session_id="session-a",
                state="waiting_user",
                phase="reasoning",
                goal="后台旧目标",
            ),
        )
    )
    by_name = {item["function"]["name"]: item for item in schemas}
    assert by_name["correct_cognitive_activity"]["function"]["parameters"]["properties"]["activity_id"]["enum"] == [
        "cognitive-a"
    ]
    assert by_name["correct_background_work"]["function"]["parameters"]["properties"]["activity_id"]["enum"] == [
        "background-a"
    ]
    assert "认知任务" in by_name["correct_cognitive_activity"]["function"]["description"]
    assert "后台工作" in by_name["correct_background_work"]["function"]["description"]
    assert "文件清理" not in by_name["correct_background_work"]["function"]["description"]


def test_cognitive_correction_schema_uses_new_goal_and_excludes_inherited_types() -> None:
    """Cognitive 纠正只要求完整新目标，追加能力不能重复声明父任务已有类型。"""

    schemas = ForegroundToolService().tool_schemas(
        correctable_activities=(
            CorrectableActivityContext(
                activity_id="cognitive-a",
                source="cognitive",
                interaction_session_id="session-a",
                state="running",
                phase="reasoning",
                goal="查询 Chrome 版本并分析兼容性",
                cognitive_tool_types=("lookup_current_fact",),
            ),
        )
    )
    schema = next(
        item
        for item in schemas
        if item["function"]["name"] == "correct_cognitive_activity"
    )
    parameters = schema["function"]["parameters"]
    properties = parameters["properties"]

    assert parameters["required"] == ["activity_id", "correction_goal"]
    assert "replacement_tool" not in properties
    assert "brief" not in properties
    assert "response_goal" not in properties
    assert "additional_cognitive_types" in properties
    assert properties["additional_cognitive_types"]["items"]["enum"] == [
        "analyze_deeply"
    ]


def test_cognitive_correction_schema_omits_additional_types_when_all_are_inherited() -> None:
    """A 已包含全部 Cognitive 类型时，不向 Foreground 暴露空的追加能力参数。"""

    schemas = ForegroundToolService().tool_schemas(
        correctable_activities=(
            CorrectableActivityContext(
                activity_id="cognitive-a",
                source="cognitive",
                interaction_session_id="session-a",
                state="running",
                phase="reasoning",
                goal="综合核查任务",
                cognitive_tool_types=(
                    "lookup_current_fact",
                    "lookup_current_fact",
                    "analyze_deeply",
                ),
            ),
        )
    )
    schema = next(
        item
        for item in schemas
        if item["function"]["name"] == "correct_cognitive_activity"
    )

    assert (
        "additional_cognitive_types"
        not in schema["function"]["parameters"]["properties"]
    )


def test_activity_correction_tools_exclude_cancelling_and_finalizing_activity() -> None:
    """取消中和结果交付中的任务都不能再进入纠正事务。"""

    service = ForegroundToolService()
    for activity in (
        {
            "activity_id": "cancelling-a",
            "interaction_session_id": "session-a",
            "source": "cognitive",
            "state": "cancelling",
            "phase": "reasoning",
        },
        {
            "activity_id": "finalizing-a",
            "interaction_session_id": "session-a",
            "source": "background_agent",
            "state": "running",
            "phase": "finalizing",
        },
        {
            "activity_id": "terminal-a",
            "interaction_session_id": "session-a",
            "source": "cognitive",
            "state": "completed",
            "phase": "reasoning",
        },
    ):
        assert not {
            "correct_cognitive_activity",
            "correct_background_work",
        } & _tool_names(
            service.tool_schemas(
                correctable_activities=select_correctable_activity_contexts(
                    [activity],
                    interaction_session_id="session-a",
                )
            )
        )


def test_include_current_scene_only_covers_desktop_view() -> None:
    """include_current_scene 只表达当前屏幕需求，用户上传图片由系统自动附带。"""

    schemas = ForegroundToolService().tool_schemas()
    schema = next(
        item for item in schemas if item["function"]["name"] == "start_background_work"
    )
    description = schema["function"]["parameters"]["properties"]["include_current_scene"][
        "description"
    ]

    assert "desktop_view" in description
    assert "由系统自动附带" in description


def _tool_names(schemas: list[dict[str, object]]) -> set[str]:
    """提取工具 schema 中的函数名。"""

    return {
        str(schema["function"]["name"])
        for schema in schemas
        if isinstance(schema.get("function"), dict)
    }
