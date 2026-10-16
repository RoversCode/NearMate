from __future__ import annotations

import asyncio
import threading
from typing import Protocol


class AsyncAudioWorker(Protocol):
    """异步音频 worker 协议。"""

    async def process_once(self) -> None:
        """推进一次音频 worker。"""
        ...

    def wake(self) -> None:
        """唤醒可能正在等待输入的 worker。"""
        ...


class AudioRuntimeThread:
    """音频运行时专用线程。"""

    def __init__(self, *, worker: AsyncAudioWorker, idle_sleep_sec: float = 0.01) -> None:
        """初始化运行时线程配置和生命周期状态。"""

        self.worker = worker
        #  控制空队列时的让步间隔，避免专用线程忙等占用 CPU。
        self.idle_sleep_sec = idle_sleep_sec
        #  是跨线程停止信号，使用 threading.Event 便于 LiveKit 事件循环同步调用 stop。
        self._stop_event = threading.Event()
        #  保存后台线程引用，用于幂等启动和 join 回收。
        self._thread: threading.Thread | None = None
        #  保护 start/stop 生命周期切换，避免重复启动创建多个音频消费线程。
        self._lock = threading.Lock()
        #  保存运行时线程最后一次真实异常，便于上层监督和测试读取。
        self.last_error: BaseException | None = None

    @property
    def is_running(self) -> bool:
        """返回运行时线程是否仍在运行。"""

        # 只读取线程存活状态，不推断 worker 内部是否有待处理音频。
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        """启动音频运行时线程。"""

        # 生命周期切换必须串行，防止两个调用者同时观察到未运行并各自启动线程。
        with self._lock:
            if self.is_running:
                return

            # 新一轮启动前清理旧停止信号和旧异常，确保状态只描述当前线程生命周期。
            self._stop_event.clear()
            self.last_error = None
            self._thread = threading.Thread(
                target=self._run_thread,
                name="companion-audio-runtime",
                daemon=True,
            )
            self._thread.start()

    def stop(self, *, timeout: float | None = 2.0) -> None:
        """停止音频运行时线程。"""

        # stop 可以在未 start、重复断连或测试清理中多次调用，必须保持幂等。
        with self._lock:
            thread = self._thread
            if thread is None:
                return

            self._stop_event.set()
            wake = getattr(self.worker, "wake", None)
            if callable(wake):
                wake()

        # join 放在锁外，避免后台线程退出回调或其他生命周期读取被长时间阻塞。
        thread.join(timeout=timeout)

        # join 后再次加锁清理已退出线程引用；如果超时仍 alive，保留引用供后续 stop 继续尝试。
        with self._lock:
            if self._thread is thread and not thread.is_alive():
                self._thread = None

    def _run_thread(self) -> None:
        """后台线程入口。"""

        try:
            asyncio.run(self._run_loop())
        except BaseException as exception:
            # 保存真实异常，让外层可以在状态页或测试中发现音频消费线程已经崩溃。
            self.last_error = exception

    async def _run_loop(self) -> None:
        """音频 worker 周期推进循环。"""

        # 线程停止信号来自同步 LiveKit 生命周期；循环每轮检查一次即可快速退出。
        while not self._stop_event.is_set():
            await self.worker.process_once()

