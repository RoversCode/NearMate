from __future__ import annotations

import pytest

from audio_aec_poc.rdp_clock_recovery import RdpClockRecoveryController


def test_controller_starts_at_normal_speed_and_recovers_slow_input() -> None:
    """RDP 输入长期变慢时，控制器应把 WSOLA 调到低于 1，而不是改采样率。"""

    controller = RdpClockRecoveryController(
        nominal_input_rate=48_000,
        output_rate=16_000,
        calibration_seconds=0.5,
        adjustment_interval_seconds=0.5,
    )

    assert controller.speed == pytest.approx(1.0)
    speed = 1.0
    observed_at = 0.0
    for _ in range(440):
        observed_at += 480 / 42_000
        speed = controller.observe(
            sample_count=480,
            observed_at=observed_at,
            buffered_output_samples=32_000,
            target_buffer_samples=32_000,
        )

    assert speed == pytest.approx(42_000 / 48_000, abs=0.03)
    assert 0.85 <= speed <= 1.15
    assert controller.diagnostics.estimated_input_rate == pytest.approx(42_000, rel=0.02)


def test_controller_limits_speed_changes_without_resetting_state() -> None:
    """长期速率突变时，单次 WSOLA 调速幅度必须受限且状态仍属于同一轮。"""

    controller = RdpClockRecoveryController(
        nominal_input_rate=48_000,
        output_rate=16_000,
        calibration_seconds=0.5,
        adjustment_interval_seconds=0.5,
        max_speed_step=0.04,
    )
    observed_at = 0.0
    speeds: list[float] = []
    for _ in range(80):
        observed_at += 480 / 42_000
        speeds.append(
            controller.observe(
                sample_count=480,
                observed_at=observed_at,
                buffered_output_samples=32_000,
                target_buffer_samples=32_000,
            )
        )

    assert max(abs(current - previous) for previous, current in zip(speeds, speeds[1:])) <= 0.040001
    assert controller.diagnostics.adjustment_count >= 1


def test_controller_uses_buffer_level_in_the_expected_direction() -> None:
    """低水位应放慢 WSOLA，高水位应加快 WSOLA 消化积压。"""

    controller = RdpClockRecoveryController(
        nominal_input_rate=48_000,
        output_rate=16_000,
        calibration_seconds=0.5,
        adjustment_interval_seconds=0.1,
    )
    observed_at = 0.0
    for _ in range(20):
        observed_at += 0.05
        controller.observe(
            sample_count=2_400,
            observed_at=observed_at,
            buffered_output_samples=32_000,
            target_buffer_samples=32_000,
        )

    neutral_speed = controller.speed
    observed_at += 0.2
    low_speed = controller.observe(
        sample_count=9_600,
        observed_at=observed_at,
        buffered_output_samples=8_000,
        target_buffer_samples=32_000,
    )
    high_speed = neutral_speed
    for _ in range(3):
        observed_at += 0.2
        high_speed = controller.observe(
            sample_count=9_600,
            observed_at=observed_at,
            buffered_output_samples=56_000,
            target_buffer_samples=32_000,
        )

    assert low_speed < neutral_speed < high_speed


def test_controller_rejects_non_monotonic_observation_time() -> None:
    """到达时间倒退会污染速率估算，必须在控制器边界直接拒绝。"""

    controller = RdpClockRecoveryController(
        nominal_input_rate=48_000,
        output_rate=16_000,
    )
    controller.observe(sample_count=480, observed_at=1.0)

    with pytest.raises(ValueError, match="monotonic"):
        controller.observe(sample_count=480, observed_at=0.9)


def test_controller_never_exposes_variable_ratio_resampler_api() -> None:
    """时钟恢复控制器只输出速度，不应持有或调用可变比率 SoXR。"""

    assert not hasattr(RdpClockRecoveryController, "set_io_ratio")
