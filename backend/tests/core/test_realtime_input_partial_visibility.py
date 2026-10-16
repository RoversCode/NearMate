from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from typing import Any, cast

from companion.audio.providers.base import PartialTranscript
from companion.realtime.asr_scheduler import AsrScheduler, PartialAsrResult
from companion.realtime.realtime_input_runtime import RealtimeInputRuntime
from companion.realtime.realtime_input_worker import RealtimeInputWorker
from companion.realtime.runtime_session import InputTurnSession

_SCHEDULER_TEST_TIMEOUT_SECONDS = 5.0


def _partial_worker(*results: PartialAsrResult) -> tuple[Any, list[dict]]:
    """创建只覆盖 partial 结果消费路径的 Worker 测试替身。

    Args:
        *results: 本次 drain 要返回的 Partial ASR 完成结果。

    Returns:
        tuple[Any, list[dict]]: 可直接调用生产方法的 Worker 替身，以及已应用的正文记录。
    """

    events: list[dict] = []
    worker = cast(Any, object.__new__(RealtimeInputWorker))
    worker.asr_scheduler = SimpleNamespace(drain_completed=lambda: list(results))
    worker.active_input_track_generation = 7
    worker.realtime_input_runtime = SimpleNamespace(
        input_turn_session=SimpleNamespace(current_turn_id="turn-1"),
        apply_partial_transcript=lambda text: events.append({"text": text})
        or [{"type": "user.partial_transcript", "payload": {"text": text}}],
    )
    worker.now = lambda: 1.0
    worker._confirm_barge_in_if_actionable_transcript = lambda **_kwargs: None
    worker._enqueue_runtime_events = lambda *_args, **_kwargs: None
    worker._last_partial_applied_byte_count = 0
    return worker, events


def test_empty_partial_is_ignored_without_runtime_event_or_barge_in() -> None:
    """空 partial 不能制造空气泡，也不能确认插话。"""

    confirmations: list[str] = []
    worker, events = _partial_worker(
        PartialAsrResult(
            text="   ",
            turn_id="turn-1",
            input_track_generation=7,
            byte_count=3200,
            submitted_at=0.5,
        )
    )
    worker._confirm_barge_in_if_actionable_transcript = lambda **kwargs: confirmations.append(
        kwargs["text"]
    )

    RealtimeInputWorker._drain_partial_asr_results(worker)

    assert events == []
    assert confirmations == []
    assert worker._last_partial_applied_byte_count == 0


def test_partial_results_compare_with_last_applied_version() -> None:
    """慢 ASR 的连续结果应展示，已经展示后的旧版本仍应丢弃。"""

    first = PartialAsrResult("你刚才说", "turn-1", 7, 3200, 0.1)
    second = PartialAsrResult("你刚才说就是", "turn-1", 7, 6400, 0.2)
    worker, events = _partial_worker(first, second)
    # 模拟 ASR 推理期间又提交了更新快照；完成结果不能再和这个提交游标比较。
    worker._last_partial_submitted_byte_count = 9600

    RealtimeInputWorker._drain_partial_asr_results(worker)

    assert events == [{"text": "你刚才说"}, {"text": "你刚才说就是"}]
    assert worker._last_partial_applied_byte_count == 6400
    assert RealtimeInputWorker._is_current_partial_result(worker, first) is False


def test_slow_scheduler_keeps_running_and_latest_pending_partial_visible() -> None:
    """慢 ASR 运行期间提交更新快照时，两版有效结果都不能被提交游标吞掉。"""

    first_started = threading.Event()
    release_first = threading.Event()

    class SlowPartialProvider:
        """用显式事件稳定复现一个运行中 Partial 和一个 latest-wins pending Partial。"""

        def __init__(self) -> None:
            self.call_count = 0

        async def transcribe_partial(self, _pcm16: bytes) -> PartialTranscript:
            """阻塞第一版，确保第二版在它完成前进入 pending。

            Args:
                _pcm16: Scheduler 传入的音频快照，本测试只验证版本调度。

            Returns:
                PartialTranscript: 每次调用对应的非空实时转写。
            """

            self.call_count += 1
            if self.call_count == 1:
                first_started.set()
                if not release_first.wait(timeout=_SCHEDULER_TEST_TIMEOUT_SECONDS):
                    raise TimeoutError("测试未及时释放第一版 Partial ASR")
                return PartialTranscript(text="第一版")
            return PartialTranscript(text="第二版")

    scheduler = AsrScheduler(asr_provider=SlowPartialProvider())
    scheduler.start()
    try:
        assert scheduler.submit_partial(
            pcm16=b"a" * 3200,
            turn_id="turn-1",
            input_track_generation=7,
            byte_count=3200,
            submitted_at=0.1,
        ) is True
        assert first_started.wait(timeout=_SCHEDULER_TEST_TIMEOUT_SECONDS)
        assert scheduler.submit_partial(
            pcm16=b"b" * 6400,
            turn_id="turn-1",
            input_track_generation=7,
            byte_count=6400,
            submitted_at=0.2,
        ) is True
        release_first.set()

        deadline = time.monotonic() + _SCHEDULER_TEST_TIMEOUT_SECONDS
        while scheduler.completed_count < 2 and time.monotonic() < deadline:
            time.sleep(0.001)
        results = scheduler.drain_completed()
    finally:
        release_first.set()
        scheduler.stop()

    worker, events = _partial_worker(*results)
    worker._last_partial_submitted_byte_count = 6400
    RealtimeInputWorker._drain_partial_asr_results(worker)

    assert [result.byte_count for result in results] == [3200, 6400]
    assert events == [{"text": "第一版"}, {"text": "第二版"}]


def test_stale_partial_from_previous_turn_is_rejected() -> None:
    """输入 reset 后，旧 turn 的迟到 partial 不能污染新 turn。"""

    worker, events = _partial_worker(
        PartialAsrResult("旧轮", "turn-1", 7, 3200, 0.1)
    )
    worker.realtime_input_runtime.input_turn_session.current_turn_id = "turn-2"

    RealtimeInputWorker._drain_partial_asr_results(worker)

    assert events == []


def test_partial_without_active_turn_is_rejected() -> None:
    """没有活跃输入 Turn 时，partial 不能生成无归属的公共事件。"""

    worker, events = _partial_worker(
        PartialAsrResult("无归属", None, 7, 3200, 0.1)
    )
    worker.realtime_input_runtime.input_turn_session.current_turn_id = None

    RealtimeInputWorker._drain_partial_asr_results(worker)

    assert events == []


def test_scheduler_rejection_returns_false_without_pending_snapshot() -> None:
    """停止中的 Scheduler 必须明确拒绝提交，避免调用方推进节流游标。"""

    scheduler = AsrScheduler(asr_provider=object())

    assert scheduler.submit_partial(
        pcm16=b"audio",
        turn_id="turn-1",
        input_track_generation=7,
        byte_count=3200,
        submitted_at=0.1,
    ) is False
    assert scheduler._pending_snapshot is None


def test_worker_does_not_advance_throttle_cursor_when_scheduler_rejects() -> None:
    """Scheduler 拒绝快照时，Worker 必须保留本次音频的重试资格。"""

    worker = cast(Any, object.__new__(RealtimeInputWorker))
    worker._current_turn_pcm16 = b"audio"
    worker._last_partial_submitted_byte_count = 0
    worker._last_partial_at_ms = None
    worker.active_input_track_generation = 7
    worker.realtime_input_runtime = SimpleNamespace(
        input_turn_session=SimpleNamespace(current_turn_id="turn-1")
    )
    worker.asr_scheduler = SimpleNamespace(submit_partial=lambda **_kwargs: False)
    worker._update_speaker_gate_state = lambda **_kwargs: "disabled"
    worker._try_confirm_barge_in = lambda **_kwargs: None

    RealtimeInputWorker._submit_partial_asr_snapshot(
        worker,
        submitted_at=1.0,
        submitted_at_ms=1000.0,
    )

    assert worker._last_partial_submitted_byte_count == 0
    assert worker._last_partial_at_ms is None


def test_runtime_rejects_blank_partial_transcript() -> None:
    """Runtime 是最后一道门禁，绕过 Worker 也不能发布空 partial。"""

    runtime = cast(Any, object.__new__(RealtimeInputRuntime))
    runtime.current_audio_session_id = "session-a"
    runtime.input_runtime_id = "input-runtime-00000000-0000-4000-8000-000000000001"
    runtime.input_runtime_epoch = 1
    runtime.input_turn_session = InputTurnSession(
        current_turn_id="turn-1",
        current_turn_sequence=1,
    )

    assert runtime.apply_partial_transcript(" \t\n") == []
    assert runtime.apply_partial_transcript("  有效文字  ") == [
        {
            "type": "user.partial_transcript",
            "payload": {
                "input_runtime_id": "input-runtime-00000000-0000-4000-8000-000000000001",
                "input_runtime_epoch": 1,
                "session_id": "session-a",
                "turn_id": "turn-1",
                "turn_sequence": 1,
                "text": "有效文字",
            },
        }
    ]
