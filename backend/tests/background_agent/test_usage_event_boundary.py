from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest

from companion.activities.runtime.models import ActivityApplyResult, ActivityProjection
from companion.background_agent.usage_service import BackgroundAgentUsageService
from companion.realtime.assistant_events import RuntimeEvent
from backend.tests.runtime_public_event_support import runtime_event_router


@pytest.mark.asyncio
async def test_usage_is_persisted_then_removed_before_public_activity_handlers() -> None:
    """内部用量可被主进程保存，但公共 Notice 永远看不到该字段。"""

    persisted_payloads: list[dict[str, object]] = []
    activity_payloads: list[dict[str, object]] = []
    notice_payloads: list[dict[str, object]] = []

    def persist_usage(_event_type: str, payload: dict[str, object]) -> None:
        """记录写入端口收到的原始内部载荷。"""

        persisted_payloads.append(payload)

    def apply_activity(
        _event_type: str,
        payload: dict[str, object],
        _runtime_epoch: int,
    ) -> ActivityApplyResult:
        """记录公共 Activity 适配边界实际收到的载荷。"""

        activity_payloads.append(payload)
        return ActivityApplyResult("changed", ActivityProjection(revision=1))

    router = runtime_event_router(
        publish=_ignore_broadcast,
        publish_ephemeral=_ignore_broadcast,
        realtime_input_runtime=SimpleNamespace(),
        background_agent_usage_handler=persist_usage,
        activity_apply_handler=apply_activity,
        activity_broadcast_handler=_ignore_projection,
        activity_notice_handler=lambda _event_type, payload, _runtime_epoch: (
            notice_payloads.append(payload)
        ),
    )

    await router.dispatch(_non_deliverable_acceptance_event())
    await router.dispatch(_terminal_event())

    assert persisted_payloads[0]["internal_usage"] == _internal_usage()
    assert "internal_usage" not in activity_payloads[0]
    assert "internal_usage" not in notice_payloads[0]


@pytest.mark.asyncio
async def test_usage_handler_failure_does_not_change_terminal_dispatch() -> None:
    """用量写入异常只能记录诊断，任务终态仍继续进入公共链路。"""

    activity_payloads: list[dict[str, object]] = []

    def fail_usage_write(_event_type: str, _payload: dict[str, object]) -> None:
        """模拟 SQLite 写入故障。"""

        raise OSError("disk unavailable")

    router = runtime_event_router(
        publish=_ignore_broadcast,
        publish_ephemeral=_ignore_broadcast,
        realtime_input_runtime=SimpleNamespace(),
        background_agent_usage_handler=fail_usage_write,
        activity_apply_handler=lambda _event_type, payload, _runtime_epoch: (
            activity_payloads.append(payload)
            or ActivityApplyResult("changed", ActivityProjection(revision=1))
        ),
        activity_broadcast_handler=_ignore_projection,
    )

    await router.dispatch(_terminal_event())

    assert len(activity_payloads) == 1
    assert "internal_usage" not in activity_payloads[0]


@pytest.mark.asyncio
async def test_stale_runtime_terminal_still_persists_usage_without_public_projection() -> None:
    """热重配后的旧 Runner 终态只补记用量，不能改写新世代公共状态。"""

    persisted_payloads: list[dict[str, object]] = []
    activity_payloads: list[dict[str, object]] = []
    router = runtime_event_router(
        publish=_ignore_broadcast,
        publish_ephemeral=_ignore_broadcast,
        realtime_input_runtime=SimpleNamespace(),
        current_runtime_epoch=lambda: 2,
        background_agent_usage_handler=lambda _event_type, payload: (
            persisted_payloads.append(payload)
        ),
        activity_apply_handler=lambda _event_type, payload, _runtime_epoch: (
            activity_payloads.append(payload)
            or ActivityApplyResult("changed", ActivityProjection(revision=1))
        ),
        activity_broadcast_handler=_ignore_projection,
    )

    await router.dispatch(_terminal_event())

    assert len(persisted_payloads) == 1
    assert persisted_payloads[0]["internal_usage"] == _internal_usage()
    assert activity_payloads == []


@pytest.mark.asyncio
async def test_blocked_usage_write_does_not_delay_public_terminal_projection() -> None:
    """磁盘线程被锁住时，公开 Activity 终态仍应先完成投影。"""

    repository = BlockingUsageRepository()
    service = BackgroundAgentUsageService(repository)
    await service.startup()
    activity_payloads: list[dict[str, object]] = []
    router = runtime_event_router(
        publish=_ignore_broadcast,
        publish_ephemeral=_ignore_broadcast,
        realtime_input_runtime=SimpleNamespace(),
        background_agent_usage_handler=service.record_terminal_event,
        activity_apply_handler=lambda _event_type, payload, _runtime_epoch: (
            activity_payloads.append(payload)
            or ActivityApplyResult("changed", ActivityProjection(revision=1))
        ),
        activity_broadcast_handler=_ignore_projection,
    )

    await router.dispatch(_terminal_event())

    assert len(activity_payloads) == 1
    assert "internal_usage" not in activity_payloads[0]
    assert await asyncio.to_thread(repository.write_started.wait, 1)

    repository.release_write.set()
    await service.shutdown()


class BlockingUsageRepository:
    """让写入线程停住，验证 Router 不会同步等待 Repository。"""

    def __init__(self) -> None:
        self.write_started = threading.Event()
        self.release_write = threading.Event()

    def initialize(self) -> None:
        """测试替身不需要真实数据库。"""

    def record_terminal_usage(self, **_payload: object) -> bool:
        """通知测试写入已开始，并等待测试释放。"""

        self.write_started.set()
        self.release_write.wait(timeout=2)
        return True

    def query_tasks(self, **_payload: object) -> tuple[()]:
        """本测试不会查询，返回空任务集合。"""

        return ()


async def _ignore_broadcast(_event: dict[str, object]) -> None:
    """吸收与本测试无关的广播。"""


async def _ignore_projection(_projection: ActivityProjection) -> None:
    """吸收与本测试无关的 Activity 投影广播。"""


def _terminal_event() -> RuntimeEvent:
    """构造包含内部用量的后台工作取消终态。"""

    return RuntimeEvent(
        type="background_agent.cancelled",
        source="assistant_runtime",
        runtime_epoch=1,
        payload={
            "activity_id": "11111111-1111-4111-8111-111111111111",
            "interaction_session_id": "session-a",
            "goal": "公开目标",
            "created_at": 1_000,
            "reason": "agent_interrupted",
            "internal_usage": _internal_usage(),
        },
    )


def _non_deliverable_acceptance_event() -> RuntimeEvent:
    """构造无需结果卡和请求确认即可进入 Notice 的后台工作 acceptance。"""

    return RuntimeEvent(
        type="llm.foreground.tool_call_completed",
        source="assistant_runtime",
        runtime_epoch=1,
        payload={
            "name": "start_background_work",
            "state": "accepted",
            "deliverable": False,
            "activity_id": "11111111-1111-4111-8111-111111111111",
            "request_id": "request-a",
        },
    )


def _internal_usage() -> dict[str, object]:
    """构造不含任务内容的内部用量快照。"""

    return {
        "is_complete": False,
        "reference_cost_usd": None,
        "by_model": [],
    }
