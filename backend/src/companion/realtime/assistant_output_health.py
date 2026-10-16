"""保存助手输出链路的进程级健康快照。"""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Callable
from threading import RLock
from time import monotonic
from typing import Literal


AssistantOutputHealthCode = Literal["output_unavailable", "request_identity_conflict"]


@dataclass(frozen=True, slots=True)
class AssistantOutputHealthSnapshot:
    """描述当前 Backend 是否还能接收新的助手输出请求。"""

    available: bool
    code: AssistantOutputHealthCode | None
    message: str | None
    changed_at: float

    def to_event(self) -> dict[str, object]:
        """转换成控制面可公开的健康事件。"""

        return {
            "type": "assistant.output_status_changed",
            "payload": {
                "available": self.available,
                "code": self.code,
                "message": self.message,
            },
        }


class AssistantOutputHealthLedger:
    """让 Bridge、Backend 准入和控制 WebSocket 共用一个健康事实。"""

    def __init__(self, *, clock: Callable[[], float] = monotonic) -> None:
        """初始化为输出不可用，直到活跃 Bridge 明确报告启动成功。

        Args:
            clock: 返回单调时间的函数，测试可注入固定时间序列。
        """

        self._clock = clock
        self._lock = RLock()
        # 健康状态必须带有当前 Bridge 所有者；旧会话迟到的停止通知不能覆盖新会话。
        self._owner_id: str | None = None
        self._snapshot = AssistantOutputHealthSnapshot(
            available=False,
            code="output_unavailable",
            message="助手输出暂时不可用，请重新连接后再试。",
            changed_at=clock(),
        )

    def register_owner(self, owner_id: str) -> AssistantOutputHealthSnapshot:
        """登记当前负责助手输出的 Bridge。

        新媒体会话创建 Bridge 时先登记所有者。登记后，旧 Bridge 即使仍在退出流程中，
        也没有资格再写入进程级健康账本。

        Args:
            owner_id: 新 Bridge 的唯一身份。

        Returns:
            当前健康快照；登记本身不改变可用状态。

        Raises:
            ValueError: Bridge 身份为空时抛出。
        """

        normalized_owner_id = owner_id.strip()
        if not normalized_owner_id:
            raise ValueError("健康状态所有者不能为空")
        with self._lock:
            self._owner_id = normalized_owner_id
            return self._snapshot

    def mark_available(
        self,
        *,
        owner_id: str | None = None,
    ) -> AssistantOutputHealthSnapshot | None:
        """记录助手输出恢复，并把恢复事实绑定到当前 Bridge。

        Args:
            owner_id: 产生状态的 Bridge 身份；省略时只刷新状态，不改变所有者。

        Returns:
            更新后的健康快照；不是当前所有者的迟到通知返回空值。
        """

        return self._set_snapshot(
            available=True,
            code=None,
            message=None,
            owner_id=owner_id,
            reject_stale_owner=True,
        )

    def mark_unavailable(
        self,
        code: AssistantOutputHealthCode = "output_unavailable",
        *,
        owner_id: str | None = None,
    ) -> AssistantOutputHealthSnapshot | None:
        """记录不可用原因，并拒绝已经退役 Bridge 的迟到通知。

        Args:
            code: 对外稳定的不可用错误码。
            owner_id: 产生状态的 Bridge 身份；省略时表示主进程主动刷新快照。

        Returns:
            更新后的健康快照；迟到旧所有者被忽略时返回空值。
        """

        return self._set_snapshot(
            available=False,
            code=code,
            message=(
                "助手输出身份发生冲突，已暂停继续发送。"
                if code == "request_identity_conflict"
                else "助手输出暂时不可用，请重新连接后再试。"
            ),
            owner_id=owner_id,
            reject_stale_owner=True,
        )

    def snapshot(self) -> AssistantOutputHealthSnapshot:
        """读取当前健康快照。"""

        with self._lock:
            return self._snapshot

    def snapshot_event(self) -> dict[str, object]:
        """读取可直接发送给控制 WebSocket 的快照事件。"""

        return self.snapshot().to_event()

    def _set_snapshot(
        self,
        *,
        available: bool,
        code: AssistantOutputHealthCode | None,
        message: str | None,
        owner_id: str | None,
        reject_stale_owner: bool,
    ) -> AssistantOutputHealthSnapshot | None:
        with self._lock:
            current = self._snapshot
            normalized_owner_id = owner_id.strip() if isinstance(owner_id, str) else None
            if normalized_owner_id == "":
                raise ValueError("健康状态所有者不能为空")
            if (
                reject_stale_owner
                and normalized_owner_id is not None
                and normalized_owner_id != self._owner_id
            ):
                return None
            if normalized_owner_id is not None:
                self._owner_id = normalized_owner_id
            if (
                current.available == available
                and current.code == code
                and current.message == message
            ):
                return current
            self._snapshot = AssistantOutputHealthSnapshot(
                available=available,
                code=code,
                message=message,
                changed_at=self._clock(),
            )
            return self._snapshot
