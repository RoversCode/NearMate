"""保存跨媒体会话重建仍未结算的助手请求所有权。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from threading import Condition, RLock
from time import monotonic
from typing import Literal

from companion.realtime.assistant_events import AssistantRequest, RuntimeEvent
from companion.realtime.runtime_identity import AssistantRequestIdentityConflict


AssistantRequestOwnershipPhase = Literal["queued", "submitted"]


@dataclass(slots=True)
class AssistantRequestOwnership:
    """记录一个已进入助手输出链路的请求，直到两个终态事实都送达。

    Args:
        request: 请求完整事实，包含来源、材料和历史策略。
        owner_bridge_id: 当前负责提交请求的 Bridge；交接窗口内可以暂时为空。
        phase: 请求还在队列中，或已经提交给助手运行时。
        runtime_epoch: 请求实际提交到的运行时世代。
        generation_id: 子进程为本请求创建的输出代际。
        visible_response_completed: 是否已经产生过最终可见文本。
        delivery_terminal_received: 音频/取消交付终态是否已送达。
        output_settled_received: 可见正文结算是否已送达。
    """

    request: AssistantRequest
    owner_bridge_id: str | None
    phase: AssistantRequestOwnershipPhase = "queued"
    runtime_epoch: int | None = None
    generation_id: str | None = None
    visible_response_completed: bool = False
    delivery_terminal_received: bool = False
    output_settled_received: bool = False


class AssistantRequestOwnershipLedger:
    """在媒体会话重建期间继续持有已接管请求，并只结算一次。"""

    def __init__(self, *, clock: Callable[[], float] = monotonic) -> None:
        """初始化进程级请求所有权账本。

        Args:
            clock: 返回单调时间的函数，测试可注入固定时钟。
        """

        self._clock = clock
        self._records: dict[str, AssistantRequestOwnership] = {}
        self._condition = Condition(RLock())

    def claim(
        self,
        request: AssistantRequest,
        *,
        bridge_id: str,
    ) -> AssistantRequestOwnership:
        """把完整请求登记到某个 Bridge，重复身份直接拒绝。

        Args:
            request: 已完成上下文冻结的助手请求。
            bridge_id: 当前接收请求的 Bridge 身份。

        Returns:
            新建的所有权记录。

        Raises:
            AssistantRequestIdentityConflict: 请求已经被当前 Backend 进程登记。
        """

        request_id = request.request_id.strip()
        if not request_id:
            raise ValueError("助手请求编号不能为空")
        normalized_bridge_id = bridge_id.strip()
        if not normalized_bridge_id:
            raise ValueError("Bridge 身份不能为空")
        with self._condition:
            if request_id in self._records:
                raise AssistantRequestIdentityConflict(
                    request_id=request_id,
                    field="request_id",
                    expected="unused",
                    actual="already_owned",
                )
            record = AssistantRequestOwnership(
                request=request,
                owner_bridge_id=normalized_bridge_id,
            )
            self._records[request_id] = record
            self._condition.notify_all()
            return record

    def adopt_queued_requests(self, bridge_id: str) -> tuple[AssistantRequestOwnership, ...]:
        """把旧媒体会话留下的排队请求交给新 Bridge。

        Args:
            bridge_id: 新 Bridge 身份。

        Returns:
            已经重新绑定到新 Bridge 的排队请求记录。
        """

        with self._condition:
            records = tuple(
                record
                for record in self._records.values()
                if record.phase == "queued" and record.owner_bridge_id is None
            )
            for record in records:
                record.owner_bridge_id = bridge_id
            self._condition.notify_all()
            return records

    def release_queued_requests(
        self,
        bridge_id: str,
    ) -> tuple[AssistantRequestOwnership, ...]:
        """在旧 Bridge 交接前释放尚未提交请求的会话所有权。

        Args:
            bridge_id: 即将关闭的旧 Bridge 身份。

        Returns:
            已经等待新 Bridge 接管的排队请求记录。
        """

        with self._condition:
            records = tuple(
                record
                for record in self._records.values()
                if record.owner_bridge_id == bridge_id and record.phase == "queued"
            )
            for record in records:
                record.owner_bridge_id = None
            self._condition.notify_all()
            return records

    def mark_submitted(self, request_id: str, *, bridge_id: str) -> None:
        """把请求从 Bridge 队列状态切换为已提交状态。"""

        with self._condition:
            record = self._require_owned(request_id, bridge_id=bridge_id)
            if record.phase != "queued":
                raise AssistantRequestIdentityConflict(
                    request_id=request_id,
                    field="phase",
                    expected="queued",
                    actual=record.phase,
                )
            record.phase = "submitted"
            self._condition.notify_all()

    def bind_runtime_epoch(
        self,
        request_id: str,
        runtime_epoch: int,
        *,
        bridge_id: str,
    ) -> None:
        """绑定请求实际使用的运行时世代，已绑定值不能改写。"""

        with self._condition:
            record = self._require_owned(request_id, bridge_id=bridge_id)
            if (
                record.runtime_epoch is not None
                and record.runtime_epoch != runtime_epoch
            ):
                raise AssistantRequestIdentityConflict(
                    request_id=request_id,
                    field="runtime_epoch",
                    expected=record.runtime_epoch,
                    actual=runtime_epoch,
                )
            record.runtime_epoch = runtime_epoch
            self._condition.notify_all()

    def observe_event(self, event: RuntimeEvent) -> AssistantRequestOwnership | None:
        """记录已成功交给主进程或音频调度器的运行时事件。

        Args:
            event: 已经完成下游交接的运行时事件。

        Returns:
            仍然存在的请求记录；两个终态都送达后返回 None。
        """

        request_id = event.payload.get("request_id")
        if not isinstance(request_id, str) or not request_id.strip():
            return None
        normalized_request_id = request_id.strip()
        with self._condition:
            record = self._records.get(normalized_request_id)
            if record is None:
                return None
            if (
                event.runtime_epoch is not None
                and record.runtime_epoch is not None
                and event.runtime_epoch != record.runtime_epoch
            ):
                raise AssistantRequestIdentityConflict(
                    request_id=normalized_request_id,
                    field="runtime_epoch",
                    expected=record.runtime_epoch,
                    actual=event.runtime_epoch,
                )
            if record.runtime_epoch is None and event.runtime_epoch is not None:
                record.runtime_epoch = event.runtime_epoch
            generation_id = event.payload.get("generation_id")
            if isinstance(generation_id, str) and generation_id.strip():
                if (
                    record.generation_id is not None
                    and record.generation_id != generation_id.strip()
                ):
                    raise AssistantRequestIdentityConflict(
                        request_id=normalized_request_id,
                        field="generation_id",
                        expected=record.generation_id,
                        actual=generation_id.strip(),
                    )
                record.generation_id = generation_id.strip()
            if event.type == "assistant.text_final":
                record.visible_response_completed = True
            if event.type == "assistant.output_settled":
                record.output_settled_received = True
            if _is_delivery_terminal_event(event):
                record.delivery_terminal_received = True
            if record.delivery_terminal_received and record.output_settled_received:
                self._records.pop(normalized_request_id, None)
                self._condition.notify_all()
                return None
            self._condition.notify_all()
            return record

    def remove(self, request_id: str, *, bridge_id: str) -> AssistantRequestOwnership | None:
        """移除提交失败且尚未进入运行时的请求记录。"""

        with self._condition:
            record = self._records.get(request_id)
            if record is None or record.owner_bridge_id != bridge_id:
                return None
            self._records.pop(request_id, None)
            self._condition.notify_all()
            return record

    def take_for_failure(
        self,
        *,
        bridge_id: str,
        include_queued: bool,
        runtime_epoch: int | None = None,
    ) -> dict[str, AssistantRequestOwnership]:
        """原子取走某个 Bridge 需要补偿的请求。"""

        with self._condition:
            selected: dict[str, AssistantRequestOwnership] = {}
            for request_id, record in tuple(self._records.items()):
                if record.owner_bridge_id != bridge_id:
                    continue
                if not include_queued and record.phase == "queued":
                    continue
                if runtime_epoch is not None and record.runtime_epoch != runtime_epoch:
                    continue
                selected[request_id] = record
                self._records.pop(request_id, None)
            self._condition.notify_all()
            return selected

    def wait_for_submitted_completion(self, *, bridge_id: str, timeout: float) -> bool:
        """等待某个 Bridge 的已提交请求全部收到双终态。"""

        deadline = self._clock() + max(timeout, 0.0)
        with self._condition:
            while self._has_submitted_for_bridge(bridge_id):
                remaining = deadline - self._clock()
                if remaining <= 0:
                    return False
                self._condition.wait(timeout=remaining)
            return True

    def snapshot(self) -> tuple[AssistantRequestOwnership, ...]:
        """返回当前未结算请求快照，供测试和诊断使用。"""

        with self._condition:
            return tuple(self._records.values())

    def get_request(self, request_id: str) -> AssistantRequest | None:
        """按请求编号读取来源请求的不可变引用。

        Args:
            request_id: 需要冻结来源上下文的助手请求编号。

        Returns:
            已登记请求的完整快照；请求已经完成双终态结算时返回空。
        """

        normalized_request_id = request_id.strip()
        if not normalized_request_id:
            return None
        with self._condition:
            record = self._records.get(normalized_request_id)
            return record.request if record is not None else None

    def _require_owned(
        self,
        request_id: str,
        *,
        bridge_id: str,
    ) -> AssistantRequestOwnership:
        record = self._records.get(request_id)
        if record is None:
            raise AssistantRequestIdentityConflict(
                request_id=request_id,
                field="request_id",
                expected="owned",
                actual="missing",
            )
        if record.owner_bridge_id != bridge_id:
            raise AssistantRequestIdentityConflict(
                request_id=request_id,
                field="bridge_id",
                expected=record.owner_bridge_id,
                actual=bridge_id,
            )
        return record

    def _has_submitted_for_bridge(self, bridge_id: str) -> bool:
        return any(
            record.owner_bridge_id == bridge_id and record.phase == "submitted"
            for record in self._records.values()
        )


def _is_delivery_terminal_event(event: RuntimeEvent) -> bool:
    """判断事件是否已经结束音频/取消交付侧生命周期。"""

    if event.type in {
        "assistant.audio_finished",
        "assistant.generation_interrupted",
        "assistant.generation_superseded",
    }:
        return True
    return (
        event.type == "assistant.state_changed"
        and event.payload.get("assistant_state") == "failed"
    )
