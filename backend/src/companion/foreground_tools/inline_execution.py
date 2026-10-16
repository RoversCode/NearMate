"""定义 Inline 工具在线程池排队、超时和取消期间共享的执行门禁。"""

from __future__ import annotations

from dataclasses import dataclass, field
from threading import Event


@dataclass(slots=True)
class InlineExecutionContext:
    """保存从线程池提交时就开始计算的截止时间和取消信号。

    Args:
        completion_deadline: Inline 结果必须完成的绝对单调时钟时间；空值表示无外层时限。
        generation_cancellation_event: 当前 generation 的外部协作式取消信号。
    """

    completion_deadline: float | None
    generation_cancellation_event: Event | None = None
    _local_cancellation_event: Event = field(
        default_factory=Event,
        init=False,
        repr=False,
    )

    def cancel(self) -> None:
        """让已经开始运行的 Inline 工具在下一道副作用门禁处停止。"""

        self._local_cancellation_event.set()

    def cancellation_requested(self) -> bool:
        """返回当前调用是否已被本地收口或 generation 取消。

        Returns:
            任一取消来源已经生效时返回真。
        """

        return self._local_cancellation_event.is_set() or (
            self.generation_cancellation_event is not None
            and self.generation_cancellation_event.is_set()
        )
