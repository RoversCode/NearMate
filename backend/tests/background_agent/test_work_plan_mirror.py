from __future__ import annotations

import pytest

from claude_agent_sdk.types import ToolResultBlock, ToolUseBlock

from companion.background_agent.work_plan_mirror import AgentWorkPlanMirror


def _tool_use(tool_use_id: str, name: str, **tool_input: object) -> ToolUseBlock:
    """构造一条 Claude Code Task 工具调用。"""

    return ToolUseBlock(id=tool_use_id, name=name, input=dict(tool_input))


def _success(tool_use_id: str, content: str = "ok") -> ToolResultBlock:
    """构造一条成功工具结果。"""

    return ToolResultBlock(tool_use_id=tool_use_id, content=content, is_error=False)


def test_task_create_updates_plan_only_after_successful_tool_result() -> None:
    """TaskUse 只表达修改意图，成功结果到达前不能在任务中心显示。"""

    mirror = AgentWorkPlanMirror()
    mirror.record_tool_use(
        _tool_use(
            "create-a",
            "TaskCreate",
            subject="理解参考项目",
            description="读取并对照参考实现",
            activeForm="正在理解参考项目",
        )
    )

    assert mirror.snapshot is None

    plan = mirror.record_tool_result(
        _success("create-a"),
        tool_use_result={"taskId": "1"},
    )

    assert plan is not None
    assert plan.revision == 1
    assert [item.to_payload() for item in plan.items] == [
        {
            "task_id": "1",
            "subject": "理解参考项目",
            "active_form": "正在理解参考项目",
            "status": "pending",
            "order": 1,
        }
    ]


def test_failed_task_operation_is_discarded() -> None:
    """失败的 TaskCreate 不能留下伪造 Todo。"""

    mirror = AgentWorkPlanMirror()
    mirror.record_tool_use(
        _tool_use(
            "create-a",
            "TaskCreate",
            subject="不会创建",
            description="测试失败路径",
            activeForm="正在创建",
        )
    )

    plan = mirror.record_tool_result(
        ToolResultBlock(
            tool_use_id="create-a",
            content="创建失败",
            is_error=True,
        ),
        tool_use_result=None,
    )

    assert plan is None
    assert mirror.snapshot is None


def test_task_create_reads_narrow_text_id_fallback() -> None:
    """结构化结果缺失时只解析 Claude Code 明确返回的创建编号。"""

    mirror = AgentWorkPlanMirror()
    mirror.record_tool_use(
        _tool_use(
            "create-a",
            "TaskCreate",
            subject="整理建议",
            description="形成建议",
            activeForm="正在整理建议",
        )
    )

    plan = mirror.record_tool_result(
        _success("create-a", "Task #7 created successfully: 整理建议"),
        tool_use_result=None,
    )

    assert plan is not None
    assert plan.items[0].task_id == "7"


def test_out_of_order_task_updates_never_regress_status() -> None:
    """较早 Update 的迟到结果不能把已完成任务退回 pending。"""

    mirror = AgentWorkPlanMirror()
    mirror.record_tool_use(
        _tool_use(
            "create-a",
            "TaskCreate",
            subject="核对 Notice",
            description="核对完成链路",
            activeForm="正在核对 Notice",
        )
    )
    assert mirror.record_tool_result(
        _success("create-a"),
        tool_use_result={"taskId": "1"},
    ) is not None
    mirror.record_tool_use(
        _tool_use("update-old", "TaskUpdate", taskId="1", status="pending")
    )
    mirror.record_tool_use(
        _tool_use("update-new", "TaskUpdate", taskId="1", status="completed")
    )

    completed = mirror.record_tool_result(
        _success("update-new"),
        tool_use_result={},
    )
    late = mirror.record_tool_result(
        _success("update-old"),
        tool_use_result={},
    )

    assert completed is not None and completed.items[0].status == "completed"
    assert late is None
    assert mirror.snapshot is not None
    assert mirror.snapshot.revision == 2
    assert mirror.snapshot.items[0].status == "completed"


def test_newer_task_update_can_reopen_completed_task() -> None:
    """Agent 明确提交的较新更新可以把已完成任务重新打开。"""

    mirror = AgentWorkPlanMirror()
    mirror.record_tool_use(_tool_use("initial-list", "TaskList"))
    assert mirror.record_tool_result(
        _success("initial-list"),
        tool_use_result={
            "tasks": [
                {"id": "1", "subject": "核对结果", "status": "completed"}
            ]
        },
    ) is not None
    mirror.record_tool_use(
        _tool_use("reopen", "TaskUpdate", taskId="1", status="in_progress")
    )

    reopened = mirror.record_tool_result(_success("reopen"), tool_use_result={})

    assert reopened is not None
    assert reopened.revision == 2
    assert reopened.items[0].status == "in_progress"


def test_newer_task_list_can_reconcile_reopened_task() -> None:
    """没有并发新提交时，最新 TaskList 的完整事实可以重新打开任务。"""

    mirror = AgentWorkPlanMirror()
    mirror.record_tool_use(_tool_use("initial-list", "TaskList"))
    assert mirror.record_tool_result(
        _success("initial-list"),
        tool_use_result={
            "tasks": [
                {"id": "1", "subject": "核对结果", "status": "completed"}
            ]
        },
    ) is not None
    mirror.record_tool_use(_tool_use("reconcile-list", "TaskList"))

    reopened = mirror.record_tool_result(
        _success("reconcile-list"),
        tool_use_result={
            "tasks": [
                {"id": "1", "subject": "核对结果", "status": "in_progress"}
            ]
        },
    )

    assert reopened is not None
    assert reopened.revision == 2
    assert reopened.items[0].status == "in_progress"


def test_task_list_reconciles_full_plan_and_ignores_stale_result() -> None:
    """TaskList 可补齐全量清单，但请求后的新变化会让旧列表失效。"""

    mirror = AgentWorkPlanMirror()
    mirror.record_tool_use(_tool_use("list-a", "TaskList"))
    plan = mirror.record_tool_result(
        _success("list-a"),
        tool_use_result={
            "tasks": [
                {
                    "id": "1",
                    "subject": "理解参考项目",
                    "activeForm": "正在理解参考项目",
                    "status": "completed",
                },
                {
                    "id": "2",
                    "subject": "整理建议",
                    "activeForm": "正在整理建议",
                    "status": "in_progress",
                },
            ]
        },
    )
    assert plan is not None
    assert plan.revision == 1
    assert [item.status for item in plan.items] == ["completed", "in_progress"]

    mirror.record_tool_use(_tool_use("stale-list", "TaskList"))
    mirror.record_tool_use(
        _tool_use("update-a", "TaskUpdate", taskId="2", status="completed")
    )
    updated = mirror.record_tool_result(_success("update-a"), tool_use_result={})
    stale = mirror.record_tool_result(
        _success("stale-list"),
        tool_use_result={
            "tasks": [
                {
                    "id": "2",
                    "subject": "整理建议",
                    "activeForm": "正在整理建议",
                    "status": "in_progress",
                }
            ]
        },
    )

    assert updated is not None and updated.revision == 2
    assert stale is None
    assert mirror.snapshot is not None
    assert mirror.snapshot.items[1].status == "completed"


@pytest.mark.parametrize(
    "content",
    [
        '[{"id":"1","subject":"保留我","status":"completed"},'
        '{"id":"2","status":"pending"}]',
        "#1 [completed] 保留我\n#1 [pending] 重复编号",
        "#1 [completed] 保留我\n#2 [unknown] 非法状态",
    ],
)
def test_task_list_fallback_never_commits_partial_snapshot(content: str) -> None:
    """JSON 或文本回退只要有一项非法，就必须保留上一版完整计划。"""

    mirror = AgentWorkPlanMirror()
    mirror.record_tool_use(_tool_use("initial-list", "TaskList"))
    initial = mirror.record_tool_result(
        _success("initial-list"),
        tool_use_result={
            "tasks": [
                {"id": "1", "subject": "保留我", "status": "completed"},
                {"id": "2", "subject": "也保留我", "status": "pending"},
            ]
        },
    )
    assert initial is not None

    mirror.record_tool_use(_tool_use("fallback-list", "TaskList"))
    changed = mirror.record_tool_result(
        _success("fallback-list", content),
        tool_use_result=None,
    )

    assert changed is None
    assert mirror.snapshot == initial
