from __future__ import annotations

import asyncio
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Literal

from companion.observability.logging_config import get_backend_logger


logger = get_backend_logger(__name__)


@dataclass(frozen=True)
class PartialAsrSnapshot:
    """一次 partial ASR 请求快照。"""

    # 提交时刻的当前 turn 音频快照，后台任务只读取该不可变 bytes。
    pcm16: bytes
    # 当前用户 turn 标识，用于结果回到 audio worker 时做过期校验。
    turn_id: str | None
    # 当前输入音轨代际，用于切换 track 后丢弃旧 ASR 结果。
    input_track_generation: int
    # 提交时累计音频字节数，用于 latest-wins 和 partial 游标更新。
    byte_count: int
    # 提交时间，供后续指标或日志观测延迟。
    submitted_at: float


@dataclass(frozen=True)
class PartialAsrResult:
    """一次 partial ASR 完成结果。"""

    # ASR provider 返回的中间转写文本。
    text: str
    # 结果所属 turn，必须匹配当前输入 turn 才能应用。
    turn_id: str | None
    # 结果所属输入音轨代际，必须匹配当前 worker active_input_track_generation 才能应用。
    input_track_generation: int
    # 结果对应的提交音频长度，旧长度结果不能覆盖更新结果。
    byte_count: int
    # 原始请求提交时间，便于后续计算排队和推理耗时。
    submitted_at: float


@dataclass(frozen=True)
class FinalAsrSnapshot:
    """一次 final ASR 请求快照。"""

    # 当前 turn 的完整 PCM16 快照，包含用于判断结束的尾部静音。
    pcm16: bytes
    # final 所属 turn，用于迟到结果回到 worker 时做版本校验。
    turn_id: str | None
    # final 所属输入音轨代际，track 切换后旧结果必须丢弃。
    input_track_generation: int
    # final 请求提交时累计音频长度，便于观测和版本判断。
    byte_count: int
    # final 请求提交时间，便于后续统计 final ASR 延迟。
    submitted_at: float


@dataclass(frozen=True)
class FinalAsrResult:
    """一次 final ASR 完成结果。"""

    # ASR provider 返回的最终识别文本。
    text: str
    # 结果所属 turn，必须匹配 worker 当前 finalizing turn 才能应用。
    turn_id: str | None
    # 结果所属输入音轨代际。
    input_track_generation: int
    # final 请求对应的音频长度。
    byte_count: int
    # 原始请求提交时间。
    submitted_at: float


@dataclass(frozen=True)
class EndpointEouSnapshot:
    """一次 endpoint EOU 请求快照。"""

    # 当前 turn 的完整 PCM16 快照，后台先用它生成 final ASR 文本。
    pcm16: bytes
    # endpoint 所属 turn，用于迟到结果回到 worker 时做版本校验。
    turn_id: str | None
    # endpoint 所属输入音轨代际，track 切换后旧结果必须丢弃。
    input_track_generation: int
    # endpoint 请求提交时累计音频长度，用于确认结果仍匹配当前音频版本。
    byte_count: int
    # endpoint 请求提交时间，便于后续统计等待和推理耗时。
    submitted_at: float


@dataclass(frozen=True)
class EndpointEouResult:
    """一次 endpoint EOU 完成结果。"""

    # endpoint 阶段产出的 final ASR 文本，最终收束时可直接复用。
    text: str
    # 文本 EOU 模型返回的结束概率。
    eou_probability: float
    # 结果所属 turn，必须匹配当前输入 turn 才能应用。
    turn_id: str | None
    # 结果所属输入音轨代际。
    input_track_generation: int
    # 结果对应的提交音频长度。
    byte_count: int
    # 原始请求提交时间。
    submitted_at: float


@dataclass(frozen=True)
class AsrErrorResult:
    """一次 ASR 请求失败结果。"""

    # kind 标识失败来自 partial 还是 final，便于日志和 UI 诊断区分字幕与最终识别。
    kind: Literal["partial", "final", "endpoint_eou"]
    # turn_id 保留提交时的 turn 边界，便于 audio worker 判定错误是否仍属于当前输入。
    turn_id: str | None
    # input_track_generation 保留提交时的输入音轨代际，用于跨 track 切换诊断。
    input_track_generation: int
    # byte_count 保留提交时的音频长度，用于定位失败 snapshot。
    byte_count: int
    # submitted_at 是请求提交时间，便于后续计算失败延迟。
    submitted_at: float
    # message 是异常摘要，不包含敏感音频内容。
    message: str


class AsrScheduler:
    """输入推理调度 lane。"""

    def __init__(self, *, asr_provider: Any, text_eou_provider: Any | None = None) -> None:
        """初始化输入推理 lane 的线程状态和请求队列。"""

        self.asr_provider = asr_provider
        self.text_eou_provider = text_eou_provider
        self._lock = threading.Lock()  # _lock 保护 pending、completed 和 active 状态，保证 audio worker 线程与 ASR 线程安全通信。
        self._work_available = threading.Condition(self._lock)  # _work_available 用于唤醒 ASR 线程处理新请求或停止信号，避免忙等。
        self._stop_requested = False  # _stop_requested 标记 lane 生命周期结束，ASR 线程会在当前 provider 调用完成后退出。
        self._thread: threading.Thread | None = None  # _thread 保存专属 ASR 线程引用，start/stop 保持幂等。
        self._active_kind: Literal["partial", "final", "endpoint_eou"] | None = None  # _active_kind 描述当前正在执行的请求类型；None 表示推理线程空闲。
        self._pending_snapshot: PartialAsrSnapshot | None = None  # _pending_snapshot 保存 latest-wins partial，请求运行中被新 partial 覆盖。
        self._pending_final_snapshot: FinalAsrSnapshot | None = None  # _pending_final_snapshot 保存待执行 final；final 优先于 pending partial。
        self._pending_endpoint_eou_snapshot: EndpointEouSnapshot | None = None  # endpoint_eou 保存待执行端点文本判断；优先于 partial。
        #  保存已完成 partial，由 audio worker drain。
        self._completed_results: deque[PartialAsrResult] = deque()
        #  保存已完成 final，由 audio worker drain。
        self._completed_final_results: deque[FinalAsrResult] = deque()
        # 保存已完成 endpoint EOU，由 audio worker drain 后更新端点等待状态。
        self._completed_endpoint_eou_results: deque[EndpointEouResult] = deque()
        #  保存 ASR provider 失败事实，由 audio worker drain 后转内部错误事件。
        self._error_results: deque[AsrErrorResult] = deque()
        #  保存最近一次真实异常对象，便于测试和诊断读取。
        self.last_error: BaseException | None = None
        self.now = time.monotonic

    @property
    def is_running(self) -> bool:
        """返回 ASR 专属线程是否存活。"""

        # 读取线程引用到局部变量，避免检查期间其他线程把 _thread 改成 None。
        thread = self._thread
        return thread is not None and thread.is_alive()

    @property
    def completed_count(self) -> int:
        """返回当前可 drain 的 partial 结果数量。"""

        # 只暴露长度，不暴露内部 deque，避免调用方绕过 drain 修改队列。
        with self._lock:
            return len(self._completed_results)

    @property
    def completed_final_count(self) -> int:
        """返回当前可 drain 的 final 结果数量。"""

        # 只暴露数量，保持完成队列只能通过 drain 方法消费。
        with self._lock:
            return len(self._completed_final_results)

    @property
    def completed_endpoint_eou_count(self) -> int:
        """返回当前可 drain 的 endpoint EOU 结果数量。"""

        with self._lock:
            return len(self._completed_endpoint_eou_results)

    def start(self) -> None:
        """启动 ASR 专属线程。"""

        with self._lock:
            # 已有存活线程时直接返回，避免重复创建多个 provider 消费者。
            if self.is_running:
                return

            # 启动新线程前清除旧停止标记，允许 stop 后再次 start。
            self._stop_requested = False
            # daemon 线程绑定固定名称，便于日志和测试确认 ASR 不在音频线程执行。
            self._thread = threading.Thread(
                target=self._run_loop,
                name="companion-asr-runtime",
                daemon=True,
            )
            self._thread.start()

    def stop(self, timeout: float | None = 2.0) -> None:
        """停止 ASR 专属线程。"""

        with self._lock:
            # 没有线程时也清理队列，保证 stop 幂等且不会留下旧状态。
            thread = self._thread
            self._stop_requested = True
            self._pending_snapshot = None
            self._pending_final_snapshot = None
            self._pending_endpoint_eou_snapshot = None
            self._completed_results.clear()
            self._completed_final_results.clear()
            self._completed_endpoint_eou_results.clear()
            self._error_results.clear()
            self._work_available.notify_all()

        # join 必须在锁外执行，避免 ASR 线程完成时无法回收 active 状态。
        if thread is not None:
            thread.join(timeout=timeout)

        with self._lock:
            # 线程已自然退出时释放引用；若 provider 仍在运行，保留引用供 is_running 反映真实状态。
            if self._thread is thread and (thread is None or not thread.is_alive()):
                self._thread = None
            # stop 后再次清理，覆盖 join 期间可能完成的旧结果。
            self._pending_snapshot = None
            self._pending_final_snapshot = None
            self._pending_endpoint_eou_snapshot = None
            self._completed_results.clear()
            self._completed_final_results.clear()
            self._completed_endpoint_eou_results.clear()
            self._error_results.clear()

    def submit_partial(
        self,
        *,
        pcm16: bytes,
        turn_id: str | None,
        input_track_generation: int,
        byte_count: int,
        submitted_at: float,
    ) -> bool:
        """尝试提交一次 partial ASR 快照。

        Args:
            pcm16: 本次识别要使用的 PCM16 音频快照。
            turn_id: 音频快照所属的输入 Turn。
            input_track_generation: 输入音轨代际。
            byte_count: 提交快照时累计的音频字节数。
            submitted_at: 快照提交时的单调时钟时间。

        Returns:
            bool: 快照已进入待执行或正在执行的 partial lane 时返回 `True`，否则返回 `False`。
        """

        with self._lock:
            # scheduler 未运行或已进入停止生命周期时不接受新请求，避免旧 room 的提交泄漏到下一次 start。
            thread = self._thread
            if self._stop_requested or thread is None or not thread.is_alive():
                return False

            # final 或 endpoint 已经进入关键路径时，当前 turn 的 partial 不应再抢占模型资源。
            if (
                self._pending_final_snapshot is not None
                or self._pending_endpoint_eou_snapshot is not None
                or self._active_kind in {"final", "endpoint_eou"}
            ):
                return False

            # bytes() 确保 snapshot 与 worker 后续缓冲拼接解耦，避免后台线程观察到可变状态。
            self._pending_snapshot = PartialAsrSnapshot(
                pcm16=bytes(pcm16),
                turn_id=turn_id,
                input_track_generation=input_track_generation,
                byte_count=byte_count,
                submitted_at=submitted_at,
            )
            self._work_available.notify()
            return True

    def submit_final(
        self,
        *,
        pcm16: bytes,
        turn_id: str | None,
        input_track_generation: int,
        byte_count: int,
        submitted_at: float,
    ) -> bool:
        """提交一次 final ASR 快照。"""

        with self._lock:
            # scheduler 未运行或已进入停止生命周期时拒绝 final，调用方可据此知道提交没有进入 lane。
            thread = self._thread
            if self._stop_requested or thread is None or not thread.is_alive():
                return False

            # 同一 lane 同一时刻只允许一个 final 等待或执行，避免重复 finalize 同一 turn。
            if self._pending_final_snapshot is not None or self._active_kind == "final":
                return False

            # final 确认当前 turn 已收束，尚未启动的 partial snapshot 不应再占用推理资源。
            self._pending_snapshot = None
            self._pending_endpoint_eou_snapshot = None
            self._pending_final_snapshot = FinalAsrSnapshot(
                pcm16=bytes(pcm16),
                turn_id=turn_id,
                input_track_generation=input_track_generation,
                byte_count=byte_count,
                submitted_at=submitted_at,
            )
            self._work_available.notify()
            return True

    def submit_endpoint_eou(
        self,
        *,
        pcm16: bytes,
        turn_id: str | None,
        input_track_generation: int,
        byte_count: int,
        submitted_at: float,
    ) -> bool:
        """提交一次 endpoint EOU 快照。"""

        with self._lock:
            thread = self._thread
            if self._stop_requested or thread is None or not thread.is_alive():
                return False
            if self.text_eou_provider is None:
                return False

            # final 是真正提交 LLM 的关键路径；final 等待或运行时不再接受 endpoint 判断。
            if self._pending_final_snapshot is not None or self._active_kind == "final":
                return False

            # endpoint 判断会执行 final ASR，尚未启动的 partial 不应继续占用识别资源。
            self._pending_snapshot = None
            self._pending_endpoint_eou_snapshot = EndpointEouSnapshot(
                pcm16=bytes(pcm16),
                turn_id=turn_id,
                input_track_generation=input_track_generation,
                byte_count=byte_count,
                submitted_at=submitted_at,
            )
            self._work_available.notify()
            return True

    def drain_completed(self) -> list[PartialAsrResult]:
        """取出所有已经完成的 partial ASR 结果。"""

        # 一次性 drain 让 audio worker 能在处理新音频前先应用所有完成事实。
        with self._lock:
            results = list(self._completed_results)
            self._completed_results.clear()
            return results

    def drain_completed_final(self) -> list[FinalAsrResult]:
        """取出所有已经完成的 final ASR 结果。"""

        # final 结果只允许 audio worker 消费，保证 RealtimeInputRuntime 仍是单写。
        with self._lock:
            results = list(self._completed_final_results)
            self._completed_final_results.clear()
            return results

    def drain_completed_endpoint_eou(self) -> list[EndpointEouResult]:
        """取出所有已经完成的 endpoint EOU 结果。"""

        with self._lock:
            results = list(self._completed_endpoint_eou_results)
            self._completed_endpoint_eou_results.clear()
            return results

    def drain_errors(self) -> list[AsrErrorResult]:
        """取出所有 ASR 失败结果。"""

        # 错误结果和转写结果一样通过 drain 交给 audio worker，保持运行时状态单写。
        with self._lock:
            results = list(self._error_results)
            self._error_results.clear()
            return results

    def reset(self) -> None:
        """清理尚未应用的 ASR 请求和结果。"""

        with self._lock:
            # pending 属于尚未启动的旧输入上下文，重置后必须直接丢弃。
            self._pending_snapshot = None
            # 待执行 final 也属于旧输入上下文，重置后不能再触发最终文本。
            self._pending_final_snapshot = None
            # 待执行 endpoint EOU 属于旧输入上下文，重置后不能再影响端点等待。
            self._pending_endpoint_eou_snapshot = None
            # 已完成但未应用的结果同样属于旧上下文，不能跨 reset 进入新 turn。
            self._completed_results.clear()
            # final 完成结果也不能跨 reset 应用到新输入上下文。
            self._completed_final_results.clear()
            # endpoint EOU 完成结果同样不能跨 reset 应用。
            self._completed_endpoint_eou_results.clear()
            # 错误结果属于旧输入上下文，重置后也不再上报给 worker。
            self._error_results.clear()

    def _run_loop(self) -> None:
        """ASR 专属线程主循环。"""

        while True:
            # _take_next_snapshot 会阻塞等待新请求或停止信号，避免空转消耗 CPU。
            item = self._take_next_snapshot()
            if item is None:
                return

            # kind 和 snapshot 在锁内被取走并标记为 active，锁外执行慢 provider。
            kind, snapshot = item
            try:
                if kind == "partial":
                    logger.info(
                        "跑一次partial ASR任务 turn_id=%s byte_count=%s queue_wait_ms=%.1f",
                        snapshot.turn_id,
                        snapshot.byte_count,
                        (self.now() - snapshot.submitted_at) * 1000,
                    )
                    result = self._run_partial_sync(snapshot)
                    logger.info(f"结果：{result}")
                    with self._lock:
                        # stop 已请求时丢弃迟到结果，避免 stop 清理后又出现旧完成项。
                        if not self._stop_requested:
                            self._completed_results.append(result)
                        self._active_kind = None
                        self._work_available.notify()
                    continue

                if kind == "endpoint_eou":
                    logger.info(
                        "跑一次endpoint EOU任务 turn_id=%s byte_count=%s queue_wait_ms=%.1f",
                        snapshot.turn_id,
                        snapshot.byte_count,
                        (self.now() - snapshot.submitted_at) * 1000,
                    )
                    start_at = self.now()
                    result = self._run_endpoint_eou_sync(snapshot)
                    logger.info(
                        "turn_id %s，endpoint EOU耗时 %.3f，概率 %.4f",
                        snapshot.turn_id,
                        self.now() - start_at,
                        result.eou_probability,
                    )
                    with self._lock:
                        # stop 已请求时丢弃迟到结果，避免 room 结束后旧 endpoint 回流。
                        if not self._stop_requested:
                            self._completed_endpoint_eou_results.append(result)
                        self._active_kind = None
                        self._work_available.notify()
                    continue

                logger.info(
                    "跑一次final ASR任务 turn_id=%s byte_count=%s queue_wait_ms=%.1f",
                    snapshot.turn_id,
                    snapshot.byte_count,
                    (self.now() - snapshot.submitted_at) * 1000,
                )
                start_at = self.now()
                result = self._run_final_sync(snapshot)
                logger.info(f"turn_id {snapshot.turn_id}，final asr耗时 {self.now() - start_at}，结果：{result}")
                with self._lock:
                    # stop 已请求时丢弃迟到结果，避免 room 结束后旧 final 回流。
                    if not self._stop_requested:
                        self._completed_final_results.append(result)
                    self._active_kind = None
                    self._work_available.notify()
            except Exception as exception:
                # ASR provider 失败必须显式记录，避免后台线程静默死亡。
                logger.exception("ASR recognition lane request failed")
                error_result = AsrErrorResult(
                    kind=kind,
                    turn_id=snapshot.turn_id,
                    input_track_generation=snapshot.input_track_generation,
                    byte_count=snapshot.byte_count,
                    submitted_at=snapshot.submitted_at,
                    message=str(exception),
                )
                with self._lock:
                    self.last_error = exception
                    if not self._stop_requested:
                        self._error_results.append(error_result)
                    self._active_kind = None
                    self._work_available.notify()

    def _take_next_snapshot(
        self,
    ) -> (
        tuple[Literal["partial"], PartialAsrSnapshot]
        | tuple[Literal["final"], FinalAsrSnapshot]
        | tuple[Literal["endpoint_eou"], EndpointEouSnapshot]
        | None
    ):
        """取出下一次要执行的 ASR snapshot。"""

        with self._lock:
            while not self._stop_requested:
                # final 是助手回复关键路径，必须优先于所有 pending 输入推理。
                if self._pending_final_snapshot is not None:
                    snapshot = self._pending_final_snapshot
                    self._pending_final_snapshot = None
                    self._pending_snapshot = None
                    self._pending_endpoint_eou_snapshot = None
                    self._active_kind = "final"
                    return "final", snapshot

                # endpoint EOU 会决定端点收束时机，优先于实时字幕 partial。
                if self._pending_endpoint_eou_snapshot is not None:
                    snapshot = self._pending_endpoint_eou_snapshot
                    self._pending_endpoint_eou_snapshot = None
                    self._pending_snapshot = None
                    self._active_kind = "endpoint_eou"
                    return "endpoint_eou", snapshot

                # partial 采用 latest-wins，队列中永远只有最新 snapshot。
                if self._pending_snapshot is not None:
                    snapshot = self._pending_snapshot
                    self._pending_snapshot = None
                    self._active_kind = "partial"
                    return "partial", snapshot

                # 没有工作时等待 submit 或 stop 唤醒，避免忙等。
                self._work_available.wait()

            # stop 请求到达后不再启动新请求，当前线程可以退出。
            self._active_kind = None
            return None

    def _run_partial_sync(self, snapshot: PartialAsrSnapshot) -> PartialAsrResult:
        """同步执行一次 partial provider 调用。"""

        # provider 协议是 async；在 ASR 专属线程中用独立事件循环运行，不共享 audio worker 事件循环。
        transcript = asyncio.run(self.asr_provider.transcribe_partial(snapshot.pcm16))
        return PartialAsrResult(
            text=transcript.text,
            turn_id=snapshot.turn_id,
            input_track_generation=snapshot.input_track_generation,
            byte_count=snapshot.byte_count,
            submitted_at=snapshot.submitted_at,
        )

    def _run_final_sync(self, snapshot: FinalAsrSnapshot) -> FinalAsrResult:
        """同步执行一次 final provider 调用。"""

        # final 同样通过 ASR 专属线程的独立事件循环运行，保证同一时间只有一个 provider 调用。
        text = asyncio.run(self.asr_provider.transcribe_final(snapshot.pcm16))
        return FinalAsrResult(
            text=text,
            turn_id=snapshot.turn_id,
            input_track_generation=snapshot.input_track_generation,
            byte_count=snapshot.byte_count,
            submitted_at=snapshot.submitted_at,
        )

    def _run_endpoint_eou_sync(self, snapshot: EndpointEouSnapshot) -> EndpointEouResult:
        """同步执行一次 endpoint EOU provider 调用。"""

        if self.text_eou_provider is None:
            raise RuntimeError("text_eou_provider 未配置，无法执行 endpoint EOU")

        # endpoint EOU 必须使用 final ASR 文本，因为 partial 可能只覆盖用户最后一小段表达。
        text = asyncio.run(self.asr_provider.transcribe_final(snapshot.pcm16))
        probability = float(self.text_eou_provider.predict_end_probability(text))
        return EndpointEouResult(
            text=text,
            eou_probability=probability,
            turn_id=snapshot.turn_id,
            input_track_generation=snapshot.input_track_generation,
            byte_count=snapshot.byte_count,
            submitted_at=snapshot.submitted_at,
        )
