from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Callable

from companion.environment.models import FrozenEnvironmentSnapshot


class EnvironmentContextService:
    """维护有界的内存现场快照，并按 snapshot 或语音 turn 提供冻结读取。"""

    def __init__(
        self,
        *,
        retention_seconds: float = 120.0,
        max_pending_bytes: int = 32 * 1024 * 1024,
        max_pending_items: int = 256,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        """初始化内存快照仓储。

        Args:
            retention_seconds: 未冻结快照的最长保留秒数。
            max_pending_bytes: 仓储内截图字节总上限。
            max_pending_items: 仓储内待绑定快照条目上限。
            monotonic: 可注入的单调时间来源。
        """

        self._retention_seconds = retention_seconds
        self._max_pending_bytes = max_pending_bytes
        self._max_pending_items = max_pending_items
        self._monotonic = monotonic
        self._condition = threading.Condition(threading.RLock())
        self._snapshots: OrderedDict[str, tuple[float, FrozenEnvironmentSnapshot]] = OrderedDict()
        self._turn_snapshot_ids: dict[str, str] = {}
        self._latest_snapshot_id: str | None = None
        self._latest_payload: dict[str, object] | None = None
        self._pending_bytes = 0

    def update(self, snapshot: FrozenEnvironmentSnapshot) -> FrozenEnvironmentSnapshot:
        """接纳最新 Desktop 快照并唤醒等待对应 turn 的请求。

        Args:
            snapshot: 已通过接口校验的不可变现场快照。

        Returns:
            已写入待绑定仓储的同一快照。
        """

        with self._condition:
            self._remove_snapshot(snapshot.snapshot_id)
            self._snapshots[snapshot.snapshot_id] = (self._monotonic(), snapshot)
            self._pending_bytes += len(snapshot.screenshot_bytes or b"")
            self._latest_snapshot_id = snapshot.snapshot_id
            self._latest_payload = snapshot.to_payload()
            if snapshot.turn_id:
                self._turn_snapshot_ids[snapshot.turn_id] = snapshot.snapshot_id
            self._evict_expired_and_oversized()
            self._condition.notify_all()
        return snapshot

    def claim_snapshot_by_id(self, snapshot_id: str | None) -> FrozenEnvironmentSnapshot | None:
        """按明确编号认领快照；不存在时绝不回退到最新快照。

        Args:
            snapshot_id: 文本请求明确携带的快照编号。

        Returns:
            被当前请求独占的快照；编号无效、过期或已认领时返回 None。
        """

        if not snapshot_id:
            return None
        with self._condition:
            self._evict_expired_and_oversized()
            stored = self._snapshots.get(snapshot_id)
            if stored is None:
                return None
            snapshot = stored[1]
            self._remove_snapshot(snapshot_id)
            return snapshot

    def wait_and_claim_for_turn(
        self,
        turn_id: str | None,
        timeout_seconds: float,
    ) -> FrozenEnvironmentSnapshot | None:
        """等待并认领指定语音 turn 的快照。

        Args:
            turn_id: final ASR 所属语音 turn。
            timeout_seconds: 最长等待秒数。

        Returns:
            同 turn 快照；超时或 turn 无效时返回 None。
        """

        if not turn_id:
            return None
        # 语音链路只允许在极短时间窗口内等待同 turn 的现场图，
        # 目的是给 Desktop 上传留一点并发余量，同时避免助手请求被截图采集长时间阻塞。
        deadline = self._monotonic() + max(0.0, timeout_seconds)
        with self._condition:
            while True:
                self._evict_expired_and_oversized()
                # 语音请求不看“最新一张图”，只认与当前 turn_id 精确绑定的那一张；
                # 这样即使用户已经开始下一轮说话，也不会串用别的 turn 的现场。
                snapshot_id = self._turn_snapshot_ids.get(turn_id)
                stored = self._snapshots.get(snapshot_id) if snapshot_id else None
                if stored is not None:
                    snapshot = stored[1]
                    self._remove_snapshot(snapshot_id)
                    return snapshot
                remaining = deadline - self._monotonic()
                if remaining <= 0:
                    return None
                # 条件变量在新快照写入时会被唤醒；这里按剩余时间等待，
                # 超时后立刻返回，让上层决定是否降级为无图快照。
                self._condition.wait(remaining)

    def current_snapshot(self) -> FrozenEnvironmentSnapshot | None:
        """读取最新环境快照；只用于预览和非用户 turn 状态。

        Returns:
            尚未绑定的最新快照；仓储为空时返回 None。
        """

        with self._condition:
            self._evict_expired_and_oversized()
            stored = self._snapshots.get(self._latest_snapshot_id) if self._latest_snapshot_id else None
            return stored[1] if stored is not None else None

    def current_payload(self) -> dict[str, object] | None:
        """读取不含截图字节的最新环境元数据。

        Returns:
            可公开的最近现场元数据；从未采集时返回 None。
        """

        with self._condition:
            return dict(self._latest_payload) if self._latest_payload is not None else None

    def _evict_expired_and_oversized(self) -> None:
        """按过期时间和总字节上限淘汰最旧待绑定快照。"""

        now = self._monotonic()
        while self._snapshots:
            # OrderedDict 头部永远是最早进入仓储、也最该优先淘汰的快照。
            # 这里坚持“只删最旧的”，避免把刚上传、最可能被当前请求使用的快照误删掉。
            snapshot_id, (created_at, _) = next(iter(self._snapshots.items()))
            within_retention = now - created_at <= self._retention_seconds
            within_byte_limit = self._pending_bytes <= self._max_pending_bytes
            within_item_limit = len(self._snapshots) <= self._max_pending_items
            if within_retention and within_byte_limit and within_item_limit:
                break
            # 只要任一约束被突破，就持续剔除最旧项，直到仓储重新回到安全范围。
            self._remove_snapshot(snapshot_id)

    def _remove_snapshot(self, snapshot_id: str) -> None:
        """移除指定快照并同步维护 turn 与字节计数索引。"""

        stored = self._snapshots.pop(snapshot_id, None)
        if stored is None:
            return
        snapshot = stored[1]
        # 截图字节预算按“待绑定快照总量”计算；一旦移除，必须立刻归还这部分预算。
        self._pending_bytes -= len(snapshot.screenshot_bytes or b"")
        # turn 索引只能指向仍然存在的快照；删除时同步清理，避免语音链路命中悬空引用。
        if snapshot.turn_id and self._turn_snapshot_ids.get(snapshot.turn_id) == snapshot_id:
            self._turn_snapshot_ids.pop(snapshot.turn_id, None)
        if self._latest_snapshot_id == snapshot_id:
            # latest 只是“当前仓储里最后写入且仍然活着的快照”；
            # 若它被删掉，就回退到剩余快照中最新的一张；仓储空了则置空。
            self._latest_snapshot_id = next(reversed(self._snapshots), None)
