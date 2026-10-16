"""用同一个文件锁保护后台智能体配置的跨进程提交与读取。"""

from __future__ import annotations

import asyncio
import errno
import os
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import BinaryIO

if os.name == "nt":
    import msvcrt
else:
    import fcntl


class BackgroundAgentConfigCommitLease:
    """表示当前调用方已经独占后台智能体配置提交锁。

    Args:
        lock_file: 已经完成系统级加锁的文件句柄。
    """

    def __init__(self, lock_file: BinaryIO) -> None:
        """保存锁文件句柄，直到调用方明确释放。

        Args:
            lock_file: 已经完成系统级加锁的文件句柄。
        """

        self._lock_file: BinaryIO | None = lock_file

    def release(self) -> None:
        """释放系统级文件锁；重复调用不会产生副作用。

        Returns:
            无返回值。
        """

        lock_file = self._lock_file
        if lock_file is None:
            return
        self._lock_file = None
        try:
            _unlock_file(lock_file)
        finally:
            lock_file.close()


class BackgroundAgentConfigCommitLock:
    """让主进程写事务和助手子进程配置读取共用一把锁。

    Args:
        user_settings_path: 后台智能体非密钥设置所在的用户设置文件。
        retry_interval_seconds: 锁被占用后再次尝试的等待时间。
    """

    def __init__(
        self,
        user_settings_path: str | Path,
        *,
        retry_interval_seconds: float = 0.01,
    ) -> None:
        """根据用户设置路径确定稳定的同目录锁文件。

        Args:
            user_settings_path: 后台智能体非密钥设置所在的用户设置文件。
            retry_interval_seconds: 锁被占用后再次尝试的等待时间。

        Raises:
            ValueError: 重试间隔不是正数。
        """

        if retry_interval_seconds <= 0:
            raise ValueError("后台智能体配置锁重试间隔必须大于零")
        settings_path = Path(user_settings_path)
        self.lock_path = settings_path.with_name(
            f"{settings_path.name}.background-agent.lock"
        )
        self.retry_interval_seconds = retry_interval_seconds

    def try_acquire(self) -> BackgroundAgentConfigCommitLease | None:
        """尝试立即获取锁，不在锁被占用时阻塞当前线程。

        Returns:
            获取成功时返回锁租约；锁正被其它进程持有时返回空值。
        """

        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_file = self.lock_path.open("a+b", buffering=0)
        try:
            # Windows 的字节区间锁要求文件中存在可锁定字节。并发初始化即使
            # 多写入一个哨兵字节也没有影响，因为所有调用方只锁第一个字节。
            lock_file.seek(0, os.SEEK_END)
            if lock_file.tell() == 0:
                lock_file.write(b"\0")
            lock_file.seek(0)
            if _try_lock_file(lock_file):
                return BackgroundAgentConfigCommitLease(lock_file)
        except BaseException:
            lock_file.close()
            raise
        lock_file.close()
        return None

    def acquire(self) -> BackgroundAgentConfigCommitLease:
        """阻塞当前线程，直到成功获取配置提交锁。

        Returns:
            必须由调用方释放的锁租约。
        """

        while True:
            lease = self.try_acquire()
            if lease is not None:
                return lease
            time.sleep(self.retry_interval_seconds)

    @contextmanager
    def hold(self) -> Iterator[None]:
        """在同步代码块执行期间持续持有配置提交锁。

        Yields:
            锁已持有时把控制权交还调用方。
        """

        lease = self.acquire()
        try:
            yield
        finally:
            lease.release()

    @asynccontextmanager
    async def hold_async(self) -> AsyncIterator[None]:
        """在异步代码块执行期间持续持有配置提交锁。

        Yields:
            锁已持有时把控制权交还调用方。
        """

        # 每次系统调用都采用非阻塞尝试，锁被占用时把事件循环让给其它任务；
        # 这样请求取消不会遗留一个仍在工作线程里等待、随后无人释放的锁。
        lease = self.try_acquire()
        while lease is None:
            await asyncio.sleep(self.retry_interval_seconds)
            lease = self.try_acquire()
        try:
            yield
        finally:
            lease.release()


def _try_lock_file(lock_file: BinaryIO) -> bool:
    """尝试使用当前平台的标准库能力锁住文件第一个字节。

    Args:
        lock_file: 已打开并定位到第一个字节的锁文件。

    Returns:
        成功取得独占锁时返回真；锁已被占用时返回假。
    """

    try:
        if os.name == "nt":
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exception:
        if exception.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
            return False
        raise
    return True


def _unlock_file(lock_file: BinaryIO) -> None:
    """释放当前平台的文件锁。

    Args:
        lock_file: 当前调用方持有独占锁的文件句柄。

    Returns:
        无返回值。
    """

    lock_file.seek(0)
    if os.name == "nt":
        msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        return
    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
