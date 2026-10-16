from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from threading import Condition, RLock

from companion.observability.logging_config import get_backend_logger


logger = get_backend_logger(__name__)


@dataclass(frozen=True, slots=True)
class CommitDrainStatus:
    """保存一次关闭排水检查看到的提交持有者。

    Args:
        pending_owner_ids: 仍在执行不可回滚提交的事务身份。
    """

    pending_owner_ids: tuple[str, ...]

    @property
    def drained(self) -> bool:
        """返回是否已经没有提交持有者。

        Returns:
            没有任何待完成提交时返回真。
        """

        return not self.pending_owner_ids


class BackgroundCommitDrainBarrier:
    """把 Background 提交与执行器破坏性关闭放在同一条生命周期边界。

    ``enter`` 成功后，持有者必须在 rollback 或 after-commit 完成时调用
    ``leave``。一旦 ``begin_shutdown`` 被调用，不再接受新的持有者；已经进入
    的提交会继续运行，执行器只能把最终清理注册到 ``add_drain_callback`` 之后。
    """

    def __init__(self) -> None:
        """创建一个尚未关闭的提交排水屏障。"""

        self._condition = Condition(RLock())
        self._owner_ids: set[str] = set()
        self._shutdown_requested = False
        self._drain_callbacks: list[Callable[[], None]] = []

    def enter(self, owner_id: str) -> bool:
        """登记一项不可回滚提交的生命周期持有。

        Args:
            owner_id: 提交事务的稳定身份。

        Returns:
            首次登记且关闭尚未开始时返回真；否则返回假。

        Raises:
            ValueError: 提交身份为空。
        """

        normalized_owner_id = owner_id.strip()
        if not normalized_owner_id:
            raise ValueError("commit barrier owner_id 不能为空")
        with self._condition:
            if self._shutdown_requested or normalized_owner_id in self._owner_ids:
                return False
            self._owner_ids.add(normalized_owner_id)
            return True

    def leave(self, owner_id: str) -> bool:
        """释放一项已经完成或回滚的提交持有。

        Args:
            owner_id: ``enter`` 成功时使用的稳定身份。

        Returns:
            本次确实移除持有者时返回真；重复释放返回假。
        """

        normalized_owner_id = owner_id.strip()
        callbacks: tuple[Callable[[], None], ...] = ()
        with self._condition:
            if normalized_owner_id not in self._owner_ids:
                return False
            self._owner_ids.remove(normalized_owner_id)
            self._condition.notify_all()
            if self._shutdown_requested and not self._owner_ids:
                callbacks = tuple(self._drain_callbacks)
                self._drain_callbacks.clear()
        for callback in callbacks:
            try:
                callback()
            except Exception:
                logger.exception("Background 提交排水后的清理回调失败")
        return True

    def begin_shutdown(self, timeout: float) -> CommitDrainStatus:
        """停止接纳新的提交，并在预算内等待已有提交排空。

        Args:
            timeout: 本次等待允许使用的最长秒数。

        Returns:
            记录等待结束时仍未完成的提交身份；超时不会取消这些提交。
        """

        self.request_shutdown()
        return self.wait_for_drain(timeout)

    def request_shutdown(self) -> bool:
        """在线性化点停止接纳新的提交持有者。

        Returns:
            本次首次切换到关闭请求态时返回真；重复调用返回假。
        """

        with self._condition:
            if self._shutdown_requested:
                return False
            self._shutdown_requested = True
            return True

    def wait_for_drain(self, timeout: float) -> CommitDrainStatus:
        """在不改变接纳状态的前提下等待已有提交排空。

        Args:
            timeout: 本次等待允许使用的最长秒数。

        Returns:
            记录等待结束时仍未完成的提交身份。
        """

        normalized_timeout = max(0.0, timeout)
        with self._condition:
            self._condition.wait_for(
                lambda: not self._owner_ids,
                timeout=normalized_timeout,
            )
            return CommitDrainStatus(tuple(sorted(self._owner_ids)))

    def add_drain_callback(self, callback: Callable[[], None]) -> None:
        """注册屏障排空后的终局清理动作。

        Args:
            callback: 只有全部提交持有者释放后才能执行的清理函数。

        Returns:
            无。

        Raises:
            TypeError: callback 不是可调用对象。
        """

        if not callable(callback):
            raise TypeError("commit barrier callback 必须可调用")
        run_now = False
        with self._condition:
            if self._owner_ids:
                self._drain_callbacks.append(callback)
            else:
                run_now = True
        if run_now:
            try:
                callback()
            except Exception:
                logger.exception("Background 提交排水清理回调执行失败")

    def pending_owner_ids(self) -> tuple[str, ...]:
        """读取当前仍在排水的提交身份。

        Returns:
            按稳定顺序排列的提交持有者身份。
        """

        with self._condition:
            return tuple(sorted(self._owner_ids))
