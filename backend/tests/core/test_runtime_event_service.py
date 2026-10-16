from __future__ import annotations

import asyncio
from dataclasses import replace
from threading import RLock
from typing import Any, cast

import pytest

from companion.realtime.runtime_event_service import (
    RuntimeEventService,
    RuntimeEventSessionBindings,
)
from companion.realtime.visible_output_journal import VisibleOutputSettlement


class _SignalledDrainer:
    """用显式信号模拟进程级队列通知和排水。"""

    def __init__(self) -> None:
        self.notification = asyncio.Event()
        self.drained = asyncio.Event()
        self.drain_count = 0

    async def wait_for_events(self, *, timeout: float) -> bool:
        """等待测试投递的队列通知。"""

        del timeout
        await self.notification.wait()
        self.notification.clear()
        return True

    async def drain_once(self) -> None:
        """记录唯一消费者完成了一次排水。"""

        self.drain_count += 1
        self.drained.set()


class _RecordingSelfReflectionService:
    """记录进程循环是否持续回收超过绝对期限的自我反思。"""

    def __init__(self) -> None:
        self.reap_count = 0

    def reap_expired(self) -> None:
        """记录一次由运行期循环驱动的自我反思过期回收。"""

        self.reap_count += 1


class _RecordingTurnAdmission:
    """记录 output settlement 是否越过业务提交边界释放了 Turn。"""

    def __init__(self) -> None:
        self.released_request_ids: list[str] = []

    def handle_output_settled(self, settlement: VisibleOutputSettlement) -> None:
        """记录一次真正允许释放的结算。"""

        self.released_request_ids.append(settlement.request_id)


def _self_reflection_settlement() -> VisibleOutputSettlement:
    """构造 RuntimeEventService 合同测试使用的最小反思结算。"""

    return VisibleOutputSettlement(
        settlement_id="settlement-reflection",
        request_id="request-reflection",
        generation_id="generation-reflection",
        history_policy="stable_dialogue",
        outcome="completed",
        visible_text="我重新想清楚了。",
        progress_references=(),
        derived_from_activity_ids=(),
        settled_at=3.0,
        request_source="self_reflection_recovery",
        self_reflection_id="reflection-a",
        episode_id="episode-a",
        runtime_epoch=7,
    )


def _service_shell(
    drainer: _SignalledDrainer,
    *,
    self_reflection_service: _RecordingSelfReflectionService | None = None,
) -> RuntimeEventService:
    """构造只覆盖进程生命周期职责的 RuntimeEventService 测试壳。"""

    service = object.__new__(RuntimeEventService)
    service.drainer = cast(Any, drainer)
    service._task = None
    service._drain_lock = asyncio.Lock()
    service._bindings_lock = RLock()
    service._bindings = None
    service._self_reflection_service = (
        self_reflection_service or _RecordingSelfReflectionService()
    )
    return service


@pytest.mark.asyncio
async def test_service_keeps_one_consumer_after_media_session_unbind() -> None:
    """媒体会话解绑后，进程级消费者仍会立即处理 handoff 补偿通知。"""

    drainer = _SignalledDrainer()
    service = _service_shell(drainer)
    bindings = RuntimeEventSessionBindings()
    service.bind_session(bindings)

    await service.start()
    first_task = service._task
    await service.start()

    assert service._task is first_task
    service.unbind_session(bindings)
    drainer.notification.set()
    await asyncio.wait_for(drainer.drained.wait(), timeout=0.5)

    assert service._bindings_snapshot() is None
    assert drainer.drain_count == 1
    await service.stop()


@pytest.mark.asyncio
async def test_lifecycle_and_background_drain_never_run_concurrently() -> None:
    """生命周期显式排水与后台消费必须共享同一把进程级锁。"""

    active_drains = 0
    max_active_drains = 0

    class _BlockingDrainer(_SignalledDrainer):
        async def drain_once(self) -> None:
            """让两个调用有机会重叠，以验证 Service 会把它们串行化。"""

            nonlocal active_drains, max_active_drains
            active_drains += 1
            max_active_drains = max(max_active_drains, active_drains)
            await asyncio.sleep(0)
            active_drains -= 1

    service = _service_shell(_BlockingDrainer())

    await asyncio.gather(service.drain_once(), service.drain_once())

    assert max_active_drains == 1


@pytest.mark.asyncio
async def test_empty_polling_cycle_does_not_drain_queue() -> None:
    """空轮询不排队列，但仍要驱动监督绝对期限回收。"""

    class _EmptyPollingDrainer(_SignalledDrainer):
        def __init__(self) -> None:
            super().__init__()
            self.wait_count = 0

        async def wait_for_events(self, *, timeout: float) -> bool:
            """连续返回空轮询，第二次后结束测试协程。"""

            assert timeout == 0.1
            self.wait_count += 1
            if self.wait_count == 2:
                raise asyncio.CancelledError
            return False

    drainer = _EmptyPollingDrainer()
    supervision_service = _RecordingSelfReflectionService()
    service = _service_shell(
        drainer,
        self_reflection_service=supervision_service,
    )

    with pytest.raises(asyncio.CancelledError):
        await service._run()

    assert drainer.wait_count == 2
    assert drainer.drain_count == 0
    assert supervision_service.reap_count == 1


@pytest.mark.asyncio
async def test_event_drain_precedes_deadline_reaper_when_notification_arrives() -> None:
    """已到达的正文或工具事实必须先于 deadline 回收被消费。"""

    order: list[str] = []

    class _OrderedDrainer(_SignalledDrainer):
        async def drain_once(self) -> None:
            order.append("drain")
            self.drained.set()

    class _OrderedSelfReflectionService(_RecordingSelfReflectionService):
        def reap_expired(self) -> None:
            order.append("reap")
            self.reap_count += 1

    drainer = _OrderedDrainer()
    supervision_service = _OrderedSelfReflectionService()
    service = _service_shell(
        drainer,
        self_reflection_service=supervision_service,
    )

    await service.start()
    drainer.notification.set()
    await asyncio.wait_for(drainer.drained.wait(), timeout=0.5)
    # 让循环完成同一轮的 reaper，再取消它。
    while order != ["drain", "reap"]:
        await asyncio.sleep(0)
    observed_order = list(order)
    await service.stop()

    assert observed_order == ["drain", "reap"]


@pytest.mark.asyncio
async def test_reaper_failure_does_not_kill_event_loop() -> None:
    """reaper 单轮失败只能记录日志，进程唯一事件循环必须继续消费后续通知。"""

    class _FlakyReaper(_RecordingSelfReflectionService):
        """第一轮回收抛错、之后恢复正常的自我反思服务。"""

        def __init__(self) -> None:
            super().__init__()
            self.failures = 0

        def reap_expired(self) -> None:
            self.reap_count += 1
            if self.failures == 0:
                self.failures += 1
                raise RuntimeError("transient reaper failure")

    drainer = _SignalledDrainer()
    supervision_service = _FlakyReaper()
    service = _service_shell(
        drainer,
        self_reflection_service=supervision_service,
    )

    await service.start()
    try:
        drainer.notification.set()
        await asyncio.wait_for(drainer.drained.wait(), timeout=0.5)
        # 第一轮的 reap_expired 抛错；循环若被杀死，第二次通知永远等不到排水。
        drainer.drained.clear()
        drainer.notification.set()
        await asyncio.wait_for(drainer.drained.wait(), timeout=0.5)
        # stop() 会做一次收尾排水，必须在停止前冻结计数。
        observed_drain_count = drainer.drain_count
        observed_reap_count = supervision_service.reap_count
    finally:
        await service.stop()

    # 第二次排水必然发生在第一轮 reap 抛错之后，它就是循环存活的最直接证据。
    assert observed_drain_count == 2
    assert observed_reap_count >= 1


def test_retryable_self_reflection_settlement_does_not_release_turn() -> None:
    """来源字段损坏也必须进入 reservation 校验，不能提前释放 Turn。"""

    class _RetryableSelfReflectionService(_RecordingSelfReflectionService):
        def observe_expression_settled(
            self,
            _settlement: VisibleOutputSettlement,
        ) -> str:
            """模拟 episode 提交边界的瞬时失败。"""

            return "retryable"

    turn_admission = _RecordingTurnAdmission()
    service = _service_shell(
        _SignalledDrainer(),
        self_reflection_service=_RetryableSelfReflectionService(),
    )
    service._turn_admission = cast(Any, turn_admission)

    with pytest.raises(
        RuntimeError,
        match="self_reflection_settlement_retry_required",
    ):
        service._handle_output_settled(
            replace(
                _self_reflection_settlement(),
                request_source="plain_foreground",
            )
        )

    assert turn_admission.released_request_ids == []


def test_plain_settlement_is_offered_to_self_reflection_before_turn_release() -> None:
    """普通输出也先经过 reservation 查询，再由 ignored 裁决放行。"""

    observed_request_ids: list[str] = []

    class _IgnoringSelfReflectionService(_RecordingSelfReflectionService):
        def observe_expression_settled(
            self,
            settlement: VisibleOutputSettlement,
        ) -> str:
            """记录普通 settlement 并确认它不属于 Self-Reflection。"""

            observed_request_ids.append(settlement.request_id)
            return "ignored"

    turn_admission = _RecordingTurnAdmission()
    service = _service_shell(
        _SignalledDrainer(),
        self_reflection_service=_IgnoringSelfReflectionService(),
    )
    service._turn_admission = cast(Any, turn_admission)
    settlement = replace(
        _self_reflection_settlement(),
        request_id="plain-request",
        request_source="plain_foreground",
        self_reflection_id=None,
        episode_id=None,
    )

    service._handle_output_settled(settlement)

    assert observed_request_ids == ["plain-request"]
    assert turn_admission.released_request_ids == ["plain-request"]


def test_session_observes_settlement_after_turn_barrier_is_released() -> None:
    """媒体会话只能在 Turn 屏障释放后尝试投递挂起的权限短确认。"""

    order: list[str] = []

    class _OrderedTurnAdmission(_RecordingTurnAdmission):
        """记录 Turn 释放与会话通知的先后顺序。"""

        def handle_output_settled(self, settlement: VisibleOutputSettlement) -> None:
            """先记录 Turn 屏障释放。

            Args:
                settlement: 已完成归档的可见输出结算。
            """

            order.append("turn_released")
            super().handle_output_settled(settlement)

    service = _service_shell(_SignalledDrainer())
    service._turn_admission = cast(Any, _OrderedTurnAdmission())
    service.bind_session(
        RuntimeEventSessionBindings(
            output_settled_handler=lambda _settlement: order.append(
                "session_notified"
            )
        )
    )

    service._handle_output_settled(
        replace(
            _self_reflection_settlement(),
            request_id="plain-request",
            request_source="plain_foreground",
            self_reflection_id=None,
            episode_id=None,
        )
    )

    assert order == ["turn_released", "session_notified"]


@pytest.mark.parametrize("disposition", ["committed", "ignored"])
def test_final_self_reflection_settlement_releases_turn(disposition: str) -> None:
    """已提交或确定迟到的反思 settlement 可以正常释放 Turn。"""

    class _SettledSelfReflectionService(_RecordingSelfReflectionService):
        def observe_expression_settled(
            self,
            _settlement: VisibleOutputSettlement,
        ) -> str:
            """返回测试指定的终局裁决。"""

            return disposition

    turn_admission = _RecordingTurnAdmission()
    service = _service_shell(
        _SignalledDrainer(),
        self_reflection_service=_SettledSelfReflectionService(),
    )
    service._turn_admission = cast(Any, turn_admission)
    settlement = _self_reflection_settlement()

    service._handle_output_settled(settlement)

    assert turn_admission.released_request_ids == [settlement.request_id]
