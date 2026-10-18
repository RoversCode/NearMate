"""资源准备的取消信号与限频进度。"""

from collections.abc import Callable
import time


ProgressCallback = Callable[[str], None]
CancellationCheck = Callable[[], bool]


class AssetPreparationCancelled(RuntimeError):
    """表示调用方已取消本次资源准备。"""


def check_cancelled(is_cancelled: CancellationCheck | None) -> None:
    """在继续读写资源前响应取消。

    Args:
        is_cancelled: 返回真时停止准备；为空时不检查。

    Returns:
        无；取消时抛出 AssetPreparationCancelled。
    """
    if is_cancelled is not None and is_cancelled():
        raise AssetPreparationCancelled("资源准备已取消")


class ProgressReporter:
    """限制连续进度每秒最多一次，阶段边界可以立即报告。

    Args:
        callback: 接收中文安全消息的回调；为空时保持安静。
    """

    def __init__(self, callback: ProgressCallback | None) -> None:
        self.callback = callback
        self.last_reported_at = float("-inf")

    def report(self, message: str, *, force: bool = False) -> None:
        """在阶段切换或距离上次报告一秒后交付消息。

        Args:
            message: 已由调用方收敛为清单相对文件名与进度的中文消息。
            force: 阶段开始、结束时立即交付。

        Returns:
            无；未到报告间隔时直接返回。
        """
        now = time.monotonic()
        if self.callback is None or (not force and now - self.last_reported_at < 1.0):
            return
        self.last_reported_at = now
        self.callback(message)
