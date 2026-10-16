"""维护进程内唯一的 CUA Capability Slot。"""

from __future__ import annotations

import threading
from dataclasses import dataclass


class CuaCapabilityBusyError(RuntimeError):
    """表示另一项 Activity 已经拿走了 CUA 能力槽位。"""


@dataclass(frozen=True, slots=True)
class CuaCapabilityAvailability:
    """保存不含所有者身份的 CUA 可用性快照。"""

    available: bool


class CuaCapabilitySlot:
    """只维护一个全局 CUA Grant，不启动 Driver 或管理 MCP 状态。

    两条 Background lane 会从不同线程申请同一个槽位。申请、精确释放和纠正任务
    的所有权转移都在同一把锁内完成，因此不会出现先释放后抢占的空档。
    """

    def __init__(self) -> None:
        """创建一个当前空闲的全局能力槽位。"""

        self._lock = threading.RLock()
        self._owner_activity_id: str | None = None

    def acquire(self, activity_id: str) -> None:
        """原子申请一项 Activity 的 CUA Grant。

        Args:
            activity_id: 申请能力的 Activity 身份。

        Raises:
            ValueError: Activity 身份为空。
            CuaCapabilityBusyError: 槽位已经属于另一项 Activity。
        """

        normalized_activity_id = activity_id.strip()
        if not normalized_activity_id:
            raise ValueError("CUA Grant 的 Activity ID 不能为空")
        with self._lock:
            if self._owner_activity_id in {None, normalized_activity_id}:
                self._owner_activity_id = normalized_activity_id
                return
            raise CuaCapabilityBusyError("cua_busy")

    def release(self, activity_id: str) -> bool:
        """只在身份精确匹配时释放 Grant。

        Args:
            activity_id: 需要释放能力的 Activity 身份。

        Returns:
            当前所有者匹配并成功释放时返回真。
        """

        normalized_activity_id = activity_id.strip()
        if not normalized_activity_id:
            return False
        with self._lock:
            if self._owner_activity_id != normalized_activity_id:
                return False
            self._owner_activity_id = None
            return True

    def transfer(self, parent_activity_id: str, replacement_activity_id: str) -> bool:
        """把父 Activity 的 Grant 原子转给纠正 replacement。

        Args:
            parent_activity_id: 当前 Grant 所属的父 Activity。
            replacement_activity_id: 即将启动的 replacement Activity。

        Returns:
            父身份匹配并完成转移时返回真；其它情况返回假。
        """

        normalized_parent_id = parent_activity_id.strip()
        normalized_replacement_id = replacement_activity_id.strip()
        if not normalized_parent_id or not normalized_replacement_id:
            return False
        if normalized_parent_id == normalized_replacement_id:
            return False
        with self._lock:
            if self._owner_activity_id != normalized_parent_id:
                return False
            self._owner_activity_id = normalized_replacement_id
            return True

    def snapshot(self) -> CuaCapabilityAvailability:
        """读取不暴露所有者身份的能力快照。

        Returns:
            槽位当前是否空闲。
        """

        with self._lock:
            return CuaCapabilityAvailability(available=self._owner_activity_id is None)
