"""用操作系统文件锁串行准备同一份资源，等待期间仍可取消。"""

from contextlib import contextmanager
import errno
import os
from pathlib import Path
import time
from typing import Iterator

from .control import CancellationCheck, check_cancelled
from .io import assert_no_reparse_points


@contextmanager
def asset_lock(path: Path, is_cancelled: CancellationCheck | None = None) -> Iterator[None]:
    """持有可跨进程释放的独占锁，避免重复下载和相互覆盖。

    Args:
        path: 专用于当前资源目录或缓存文件的锁文件。
        is_cancelled: 等待锁时也会检查的取消信号。

    Yields:
        无；上下文期间当前进程独占该锁。
    """
    check_cancelled(is_cancelled)
    assert_no_reparse_points(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as lock_file:
        # Windows 只锁首字节；锁文件保留在原位，避免删除后两个进程锁住不同文件。
        lock_file.seek(0, os.SEEK_END)
        if lock_file.tell() == 0:
            lock_file.write(b"\0")
            lock_file.flush()
        acquired = False
        try:
            while not acquired:
                check_cancelled(is_cancelled)
                lock_file.seek(0)
                try:
                    if os.name == "nt":
                        import msvcrt

                        msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                except OSError as error:
                    if error.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                        raise
                    time.sleep(0.05)
            check_cancelled(is_cancelled)
            yield
        finally:
            if acquired:
                lock_file.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
