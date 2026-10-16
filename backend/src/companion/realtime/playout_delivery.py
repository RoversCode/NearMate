"""记录一次助手语音交付的真实播放事实。"""

from __future__ import annotations

from dataclasses import dataclass
from threading import RLock
from typing import Literal


PlayoutDeliveryStatus = Literal[
    "awaiting_start",
    "playing",
    "delivered",
    "aborted",
    "failed",
    "unconfirmed",
    "interrupted",
]
PlayoutAckOutcome = Literal["playing", "failed"]
PlayoutCompletion = Literal[
    "drained",
    "unconfirmed_timeout",
    "unconfirmed_error",
]
_TERMINAL_PLAYOUT_STATUSES = frozenset(
    {"delivered", "aborted", "failed", "unconfirmed", "interrupted"}
)
DEFAULT_MAX_TERMINAL_PLAYOUT_DELIVERIES = 256


class PlayoutDeliveryConflictError(ValueError):
    """表示回执试图修改另一条交付或已经收口的交付。"""


class PlayoutDeliveryNotFoundError(KeyError):
    """表示 Desktop 回执引用了不存在的交付。"""


@dataclass(frozen=True)
class PlayoutDelivery:
    """保存一次语音交付从首帧发布到最终收口的身份和状态。

    Args:
        delivery_id: 本次语音交付的唯一编号。
        request_id: 生成这段语音的助手请求编号。
        generation_id: 生成这段语音的 generation 编号。
        status: 当前播放交付状态。
        client_instance_id: 最后一次 Desktop 回执的实例编号。
        failure_code: 对用户公开的稳定失败码。
        failure_message: 对用户公开的失败说明。
        occurred_at: Backend 首次登记这条交付事实的时间。
    """

    delivery_id: str
    request_id: str
    generation_id: str
    status: PlayoutDeliveryStatus
    client_instance_id: str | None = None
    failure_code: str | None = None
    failure_message: str | None = None
    occurred_at: float = 0.0


@dataclass(frozen=True)
class PlayoutDeliveryUpdate:
    """表示一次账本写入是否改变了交付状态及需要广播的事件。"""

    changed: bool
    delivery: PlayoutDelivery
    public_event: dict[str, object] | None


class PlayoutDeliveryRegistry:
    """在当前 Backend 进程内登记并收口一次性语音交付。

    这个账本只回答“这一次语音发布是否被 Desktop 真正接管”。它不等待用户听完，
    不创建新的 generation，也不触碰 Notice。Backend 重启后账本自然清空，符合当前
    运行事实只在本次进程内有效的边界。
    """

    def __init__(
        self,
        *,
        max_terminal_records: int = DEFAULT_MAX_TERMINAL_PLAYOUT_DELIVERIES,
    ) -> None:
        """创建当前进程的播放交付账本。

        Args:
            max_terminal_records: 最多保留的终态记录数量，避免长期运行时无限增长。

        Raises:
            ValueError: 终态记录上限不是正数。
        """

        if max_terminal_records <= 0:
            raise ValueError("max_terminal_records 必须大于零")
        self._deliveries: dict[str, PlayoutDelivery] = {}
        self._max_terminal_records = max_terminal_records
        self._lock = RLock()

    def start_publishing(
        self,
        *,
        delivery_id: str,
        request_id: str,
        generation_id: str,
        occurred_at: float,
    ) -> PlayoutDeliveryUpdate:
        """登记首帧已经发布到 LiveKit 的事实。

        Args:
            delivery_id: 本次语音交付编号。
            request_id: 关联的助手请求编号。
            generation_id: 关联的 generation 编号。
            occurred_at: 首帧发布的单调时间。

        Returns:
            交付更新；第一次登记会产生 Desktop 需要处理的 ready 事件。
        """

        with self._lock:
            existing = self._deliveries.get(delivery_id)
            if existing is not None:
                self._ensure_identity(existing, request_id, generation_id)
                # 同一身份的迟到首帧不能重新打开已收口交付；身份不同仍由上面的护栏报错。
                return PlayoutDeliveryUpdate(False, existing, None)

            delivery = PlayoutDelivery(
                delivery_id=delivery_id,
                request_id=request_id,
                generation_id=generation_id,
                status="awaiting_start",
                occurred_at=occurred_at,
            )
            self._store_delivery_locked(delivery)
            return PlayoutDeliveryUpdate(
                True,
                delivery,
                {
                    "type": "assistant.playout_delivery_ready",
                    "payload": {
                        "delivery_id": delivery_id,
                        "request_id": request_id,
                        "generation_id": generation_id,
                    },
                },
            )

    def acknowledge(
        self,
        *,
        delivery_id: str,
        request_id: str,
        generation_id: str,
        client_instance_id: str,
        outcome: PlayoutAckOutcome,
        occurred_at: float,
        failure_code: str | None = None,
        failure_message: str | None = None,
    ) -> PlayoutDeliveryUpdate:
        """应用 Desktop 对真实 attach/play 结果的一次性回执。

        Args:
            delivery_id: 回执对应的语音交付编号。
            request_id: 回执携带的助手请求编号。
            generation_id: 回执携带的 generation 编号。
            client_instance_id: 发送回执的 Desktop 实例编号。
            outcome: `playing` 表示已开始播放，`failed` 表示播放失败。
            occurred_at: Desktop 产生回执的时间。
            failure_code: 失败时的稳定错误码。
            failure_message: 失败时的用户可理解说明。

        Returns:
            账本更新；播放确认本身不广播事件，播放失败会广播安全失败事件。

        Raises:
            PlayoutDeliveryNotFoundError: 找不到对应交付。
            PlayoutDeliveryConflictError: 身份不一致或交付已被其它结果收口。
        """

        with self._lock:
            delivery = self._require_delivery(delivery_id)
            self._ensure_identity(delivery, request_id, generation_id)
            if not client_instance_id.strip():
                raise ValueError("client_instance_id 必须是非空字符串")

            if delivery.status == "awaiting_start":
                if outcome == "playing":
                    updated = PlayoutDelivery(
                        **{
                            **delivery.__dict__,
                            "status": "playing",
                            "client_instance_id": client_instance_id,
                            "occurred_at": delivery.occurred_at,
                        }
                    )
                    self._store_delivery_locked(updated)
                    return PlayoutDeliveryUpdate(True, updated, None)
                updated = self._failed_delivery(
                    delivery,
                    client_instance_id=client_instance_id,
                    failure_code=failure_code or "audio_play_failed",
                    failure_message=failure_message or "助手语音没有播放成功，文字回复仍可查看。",
                )
                self._store_delivery_locked(updated)
                return PlayoutDeliveryUpdate(True, updated, self._failure_event(updated))

            if delivery.status == "playing" and outcome == "playing":
                return PlayoutDeliveryUpdate(False, delivery, None)

            raise PlayoutDeliveryConflictError(
                f"交付 {delivery_id} 当前状态为 {delivery.status}，不能应用 {outcome} 回执"
            )

    def finish(
        self,
        *,
        delivery_id: str,
        request_id: str,
        generation_id: str,
        completion: PlayoutCompletion,
        occurred_at: float,
    ) -> PlayoutDeliveryUpdate | None:
        """登记 Backend 音频源的最终等待结果并收口交付。

        Args:
            delivery_id: 语音交付编号。
            request_id: 关联的助手请求编号。
            generation_id: 关联的 generation 编号。
            completion: 音频源确认排空，或看门狗判定无法确认的具体结果。
            occurred_at: 等待播放完成的收口时间。

        Returns:
            找到交付时返回更新；未知交付返回空值，已收口交付返回未变化更新。
        """

        with self._lock:
            delivery = self._deliveries.get(delivery_id)
            if delivery is None:
                return None
            self._ensure_identity(delivery, request_id, generation_id)
            if delivery.status in _TERMINAL_PLAYOUT_STATUSES:
                return PlayoutDeliveryUpdate(False, delivery, None)
            if completion != "drained":
                updated = PlayoutDelivery(**{**delivery.__dict__, "status": "unconfirmed"})
            elif delivery.status == "awaiting_start":
                updated = PlayoutDelivery(**{**delivery.__dict__, "status": "unconfirmed"})
            elif delivery.status == "playing":
                updated = PlayoutDelivery(**{**delivery.__dict__, "status": "delivered"})
            else:
                return PlayoutDeliveryUpdate(False, delivery, None)
            self._store_delivery_locked(updated)
            return PlayoutDeliveryUpdate(True, updated, None)

    def abort(
        self,
        *,
        delivery_id: str,
        request_id: str,
        generation_id: str,
    ) -> PlayoutDeliveryUpdate | None:
        """把被清空的音频发布收口为不对用户报错的取消终态。

        Args:
            delivery_id: 被取消的语音交付编号。
            request_id: 关联的助手请求编号。
            generation_id: 关联的 generation 编号。

        Returns:
            找到活动交付时返回取消更新；未知交付返回空值，已收口交付返回未变化更新。
        """

        with self._lock:
            delivery = self._deliveries.get(delivery_id)
            if delivery is None:
                return None
            self._ensure_identity(delivery, request_id, generation_id)
            if delivery.status not in {"awaiting_start", "playing"}:
                return PlayoutDeliveryUpdate(False, delivery, None)
            updated = PlayoutDelivery(**{**delivery.__dict__, "status": "aborted"})
            self._store_delivery_locked(updated)
            return PlayoutDeliveryUpdate(True, updated, None)

    def fail(
        self,
        *,
        delivery_id: str,
        request_id: str,
        generation_id: str,
        failure_code: str,
        failure_message: str,
        occurred_at: float,
        client_instance_id: str | None = None,
    ) -> PlayoutDeliveryUpdate:
        """登记 TTS 或其它播放链路的安全失败事实。

        Args:
            delivery_id: 语音交付编号。
            request_id: 关联的助手请求编号。
            generation_id: 关联的 generation 编号。
            failure_code: 稳定产品错误码。
            failure_message: 用户可理解的失败说明。
            occurred_at: 失败发生时间。
            client_instance_id: 已知的 Desktop 实例编号。

        Returns:
            交付更新；第一次失败会产生公开失败事件。
        """

        with self._lock:
            delivery = self._deliveries.get(delivery_id)
            if delivery is None:
                delivery = PlayoutDelivery(
                    delivery_id=delivery_id,
                    request_id=request_id,
                    generation_id=generation_id,
                    status="awaiting_start",
                    occurred_at=occurred_at,
                )
            else:
                self._ensure_identity(delivery, request_id, generation_id)
                if delivery.status == "failed":
                    return PlayoutDeliveryUpdate(False, delivery, None)
                if delivery.status in {"delivered", "unconfirmed", "aborted", "interrupted"}:
                    # 同一身份的迟到失败只是重复事实，不能再次广播或改变终态。
                    return PlayoutDeliveryUpdate(False, delivery, None)
            updated = self._failed_delivery(
                delivery,
                client_instance_id=client_instance_id,
                failure_code=failure_code,
                failure_message=failure_message,
            )
            self._store_delivery_locked(updated)
            return PlayoutDeliveryUpdate(True, updated, self._failure_event(updated))

    def get(self, delivery_id: str) -> PlayoutDelivery | None:
        """读取一条交付快照，供 HTTP 回执和诊断使用。"""

        with self._lock:
            return self._deliveries.get(delivery_id)

    def _require_delivery(self, delivery_id: str) -> PlayoutDelivery:
        delivery = self._deliveries.get(delivery_id)
        if delivery is None:
            raise PlayoutDeliveryNotFoundError(delivery_id)
        return delivery

    def _store_delivery_locked(self, delivery: PlayoutDelivery) -> None:
        """保存交付并淘汰最早的多余终态记录。"""

        self._deliveries[delivery.delivery_id] = delivery
        terminal_ids = [
            delivery_id
            for delivery_id, stored in self._deliveries.items()
            if stored.status in _TERMINAL_PLAYOUT_STATUSES
        ]
        excess_count = len(terminal_ids) - self._max_terminal_records
        for delivery_id in terminal_ids[: max(excess_count, 0)]:
            self._deliveries.pop(delivery_id, None)

    @staticmethod
    def _ensure_identity(
        delivery: PlayoutDelivery,
        request_id: str,
        generation_id: str,
    ) -> None:
        if delivery.request_id != request_id or delivery.generation_id != generation_id:
            raise PlayoutDeliveryConflictError(
                f"交付 {delivery.delivery_id} 的 request/generation 身份不匹配"
            )

    @staticmethod
    def _failed_delivery(
        delivery: PlayoutDelivery,
        *,
        client_instance_id: str | None,
        failure_code: str,
        failure_message: str,
    ) -> PlayoutDelivery:
        return PlayoutDelivery(
            **{
                **delivery.__dict__,
                "status": "failed",
                "client_instance_id": client_instance_id,
                "failure_code": failure_code,
                "failure_message": failure_message,
            }
        )

    @staticmethod
    def _failure_event(delivery: PlayoutDelivery) -> dict[str, object]:
        return {
            "type": "assistant.playout_delivery_failed",
            "payload": {
                "delivery_id": delivery.delivery_id,
                "request_id": delivery.request_id,
                "generation_id": delivery.generation_id,
                "client_instance_id": delivery.client_instance_id,
                "code": delivery.failure_code,
                "message": delivery.failure_message,
            },
        }
