"""RealtimeInputWorker 说话人确认 gate 的阈值和连续低分规则测试。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest

from companion.audio.providers.base import SpeakerVerificationResult
from companion.realtime.realtime_input_worker import RealtimeInputWorker


class _SpeakerVerificationStub:
    """按调用顺序返回预设相似度的说话人确认替身。"""

    def __init__(self, similarities: list[float]) -> None:
        """保存每次确认要返回的相似度。"""

        self.similarities = similarities
        self.verify_count = 0

    def verify(self, pcm16: bytes) -> SpeakerVerificationResult:
        """记录输入并返回下一条预设结果。"""

        assert pcm16
        index = min(self.verify_count, len(self.similarities) - 1)
        self.verify_count += 1
        return SpeakerVerificationResult(best_similarity=self.similarities[index])


def _build_worker(similarities: list[float]) -> tuple[Any, _SpeakerVerificationStub]:
    """构造只覆盖说话人 gate 判定所需字段的 Worker。"""

    provider = _SpeakerVerificationStub(similarities)
    worker = cast(Any, object.__new__(RealtimeInputWorker))
    worker.speaker_verification_provider = provider
    worker._sv_locked_pass = False
    worker._sv_locked_reject = False
    worker._sv_consecutive_low_count = 0
    worker._sv_check_count = 0
    worker._sv_last_checked_speech_pcm16_length = 0
    worker._sv_similarities = []
    worker._current_turn_speech_pcm16_length = 19_200  # 600ms 的 16kHz PCM16。
    worker._current_turn_speaker_pcm16 = b"\x01" * 19_200
    worker.min_final_asr_audio_length = 19_200
    worker.speaker_verify_high = 0.28
    worker.speaker_verify_low = 0.20
    worker.speaker_verify_mean = 0.28
    worker._speaker_gate_status = "waiting_audio"
    worker._barge_in_candidate = None
    worker.latency_recorder = None
    worker.now = lambda: 1.0
    worker.realtime_input_runtime = SimpleNamespace(
        input_turn_session=SimpleNamespace(current_turn_id="turn-1")
    )
    return worker, provider


def _append_verification_audio(worker: Any) -> None:
    """追加一个最短确认窗口，让下一次 partial 能真正发起确认。"""

    worker._current_turn_speech_pcm16_length += worker.min_final_asr_audio_length
    worker._current_turn_speaker_pcm16 += b"\x02" * worker.min_final_asr_audio_length


def test_first_low_similarity_waits_for_more_evidence() -> None:
    """第一次低于 low 阈值时只进入等待，不应立即拒绝当前 turn。"""

    worker, provider = _build_worker([0.10])

    status = worker._update_speaker_gate_state(is_final=False)

    assert status == "uncertain"
    assert provider.verify_count == 1
    assert worker._sv_consecutive_low_count == 1
    assert worker._sv_locked_reject is False


def test_two_consecutive_low_similarities_reject() -> None:
    """连续两次低于 low 阈值时才锁定拒绝。"""

    worker, provider = _build_worker([0.10, 0.15])

    assert worker._update_speaker_gate_state(is_final=False) == "uncertain"
    _append_verification_audio(worker)

    assert worker._update_speaker_gate_state(is_final=False) == "rejected"
    assert provider.verify_count == 2
    assert worker._sv_locked_reject is True


def test_non_low_similarity_breaks_low_streak() -> None:
    """低分之间出现一次非低分时，后面的低分不能凑成连续两次。"""

    worker, provider = _build_worker([0.10, 0.21, 0.10])

    assert worker._update_speaker_gate_state(is_final=False) == "uncertain"
    _append_verification_audio(worker)
    assert worker._update_speaker_gate_state(is_final=False) == "uncertain"
    assert worker._sv_consecutive_low_count == 0
    _append_verification_audio(worker)

    assert worker._update_speaker_gate_state(is_final=False) == "uncertain"
    assert provider.verify_count == 3
    assert worker._sv_locked_reject is False
    assert worker._sv_consecutive_low_count == 1


@pytest.mark.parametrize(
    ("similarity", "expected_status"),
    [(0.27, "rejected"), (0.28, "passed")],
)
def test_single_final_uses_final_threshold(
    similarity: float, expected_status: str
) -> None:
    """只有一次确认时，final 直接按 0.28 阈值判断，不套用多次均值。"""

    worker, provider = _build_worker([similarity])

    status = worker._update_speaker_gate_state(is_final=True)

    assert status == expected_status
    assert provider.verify_count == 1


def test_single_final_low_score_does_not_create_low_streak_lock() -> None:
    """短语音的单次 final 低分按 final 拒绝，但不伪造连续低分锁定。"""

    worker, _provider = _build_worker([0.10])

    assert worker._update_speaker_gate_state(is_final=True) == "rejected"
    assert worker._sv_consecutive_low_count == 1
    assert worker._sv_locked_reject is False


def test_multiple_results_use_mean_threshold_at_final() -> None:
    """多次确认在 final 阶段使用本轮均值收束。"""

    worker, provider = _build_worker([0.24, 0.24])

    assert worker._update_speaker_gate_state(is_final=False) == "uncertain"
    _append_verification_audio(worker)

    assert worker._update_speaker_gate_state(is_final=True) == "rejected"
    assert provider.verify_count == 2
    assert worker._sv_similarities == [0.24, 0.24]
