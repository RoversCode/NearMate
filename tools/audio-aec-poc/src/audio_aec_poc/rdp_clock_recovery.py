from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import math


_DEFAULT_CALIBRATION_SECONDS = 2.0
_DEFAULT_ESTIMATION_WINDOW_SECONDS = 6.0
_DEFAULT_ADJUSTMENT_INTERVAL_SECONDS = 0.5
_DEFAULT_MAX_RATE_DEVIATION_FRACTION = 0.15
_DEFAULT_MIN_WSOLA_SPEED = 0.85
_DEFAULT_MAX_WSOLA_SPEED = 1.15
_DEFAULT_MAX_SPEED_STEP = 0.04
_RATE_CORRECTION_DEADBAND_FRACTION = 0.005
_RATE_ESTIMATE_SMOOTHING_FACTOR = 0.25
_BUFFER_FEEDBACK_GAIN_PER_SECOND = 0.06
_MAX_BUFFER_FEEDBACK_FRACTION = 0.05


@dataclass(frozen=True)
class RdpClockRecoveryDiagnostics:
    """保存 RDP 到达速率和 WSOLA 调速控制的诊断快照。

    Args:
        input_sample_count: 已经从 MediaCapture WAV 读出的原始单声道样本数。
        estimated_input_rate: 根据样本到达墙钟估算的原始输入速率。
        wsola_speed: 当前传给 WSOLA 的速度，低于 1 表示延长音频，高于 1 表示缩短音频。
        clock_recovery_active: 当前是否正在修正输入时钟和发布时钟的长期偏差。
        buffered_output_samples: 最近一次观察时 ring buffer 中的目标格式样本数。
        adjustment_count: 已经更新 WSOLA 速度的次数。

    Returns:
        一个不可变的 RDP 时钟恢复诊断快照。
    """

    input_sample_count: int
    estimated_input_rate: float | None
    wsola_speed: float
    clock_recovery_active: bool
    buffered_output_samples: int | None
    adjustment_count: int


class RdpClockRecoveryController:
    """根据 RDP 到达速度和缓冲水位控制 WSOLA 的播放速度。

    RDP 的 WAV 头可能写着 48kHz，但样本实际按约 42kHz 到 48kHz 的墙钟速度批量到达。
    这里不改变采样率转换比例，因为那会直接改变人声音高；这里只计算保音高时间伸缩器
    的速度。控制器在同一轮采集中持续工作，不会重建时间伸缩器。

    Args:
        nominal_input_rate: WAV 头声明的原始采样率。
        output_rate: 固定 SoXR 输出给 WSOLA 的采样率。
        calibration_seconds: 开始调整前需要观察的最短墙钟时长。
        estimation_window_seconds: 估算长期输入速率使用的滑动窗口时长。
        adjustment_interval_seconds: 两次调速判断之间的最短间隔。
        max_rate_deviation_fraction: 速率估算相对标称速率的最大允许偏差。
        min_wsola_speed: WSOLA 最慢速度。
        max_wsola_speed: WSOLA 最快速度。
        max_speed_step: 单次调速允许改变的最大速度幅度。

    Returns:
        一个只负责控制和诊断、不持有音频重采样器的时钟恢复控制器。
    """

    def __init__(
        self,
        *,
        nominal_input_rate: float,
        output_rate: float,
        calibration_seconds: float = _DEFAULT_CALIBRATION_SECONDS,
        estimation_window_seconds: float = _DEFAULT_ESTIMATION_WINDOW_SECONDS,
        adjustment_interval_seconds: float = _DEFAULT_ADJUSTMENT_INTERVAL_SECONDS,
        max_rate_deviation_fraction: float = _DEFAULT_MAX_RATE_DEVIATION_FRACTION,
        min_wsola_speed: float = _DEFAULT_MIN_WSOLA_SPEED,
        max_wsola_speed: float = _DEFAULT_MAX_WSOLA_SPEED,
        max_speed_step: float = _DEFAULT_MAX_SPEED_STEP,
    ) -> None:
        """校验控制参数并从正常速度开始。"""

        if not math.isfinite(nominal_input_rate) or nominal_input_rate <= 0:
            raise ValueError("nominal_input_rate must be finite and greater than 0.")
        if not math.isfinite(output_rate) or output_rate <= 0:
            raise ValueError("output_rate must be finite and greater than 0.")
        if calibration_seconds <= 0:
            raise ValueError("calibration_seconds must be greater than 0.")
        if estimation_window_seconds < calibration_seconds:
            raise ValueError("estimation_window_seconds must not be shorter than calibration_seconds.")
        if adjustment_interval_seconds <= 0:
            raise ValueError("adjustment_interval_seconds must be greater than 0.")
        if not 0 < max_rate_deviation_fraction < 1:
            raise ValueError("max_rate_deviation_fraction must be between 0 and 1.")
        if not 0 < min_wsola_speed <= max_wsola_speed:
            raise ValueError("WSOLA speed limits are invalid.")
        if max_speed_step <= 0:
            raise ValueError("max_speed_step must be greater than 0.")

        self._nominal_input_rate = float(nominal_input_rate)
        self._output_rate = float(output_rate)
        self._calibration_seconds = float(calibration_seconds)
        self._estimation_window_seconds = float(estimation_window_seconds)
        self._adjustment_interval_seconds = float(adjustment_interval_seconds)
        self._max_rate_deviation_fraction = float(max_rate_deviation_fraction)
        self._min_wsola_speed = float(min_wsola_speed)
        self._max_wsola_speed = float(max_wsola_speed)
        self._max_speed_step = float(max_speed_step)
        self._observations: deque[tuple[float, int]] = deque()
        self._input_sample_count = 0
        self._last_observed_at: float | None = None
        self._last_adjustment_at: float | None = None
        self._estimated_input_rate: float | None = None
        self._wsola_speed = 1.0
        self._clock_recovery_active = False
        self._buffered_output_samples: int | None = None
        self._adjustment_count = 0

    @property
    def speed(self) -> float:
        """返回当前 WSOLA 速度。"""

        return self._wsola_speed

    @property
    def diagnostics(self) -> RdpClockRecoveryDiagnostics:
        """返回当前输入速率、水位和调速状态。"""

        return RdpClockRecoveryDiagnostics(
            input_sample_count=self._input_sample_count,
            estimated_input_rate=self._estimated_input_rate,
            wsola_speed=self._wsola_speed,
            clock_recovery_active=self._clock_recovery_active,
            buffered_output_samples=self._buffered_output_samples,
            adjustment_count=self._adjustment_count,
        )

    def observe(
        self,
        *,
        sample_count: int,
        observed_at: float,
        buffered_output_samples: int | None = None,
        target_buffer_samples: int | None = None,
    ) -> float:
        """记录一块输入并在到达调整间隔时返回新的 WSOLA 速度。

        Args:
            sample_count: 本次从 WAV 读出的原始单声道样本数。
            observed_at: 本次看到这块样本时的单调墙钟秒数。
            buffered_output_samples: 当前 ring buffer 中的目标格式样本数。
            target_buffer_samples: ring buffer 的目标水位样本数。

        Returns:
            当前应该交给 WSOLA 的速度。

        Raises:
            ValueError: 参数不是正数、时间倒退或缓冲反馈参数不完整。
        """

        if not isinstance(sample_count, int) or isinstance(sample_count, bool) or sample_count <= 0:
            raise ValueError("sample_count must be a positive integer.")
        if not math.isfinite(observed_at):
            raise ValueError("RDP clock recovery observed_at must be finite and monotonic.")
        if self._last_observed_at is not None and observed_at < self._last_observed_at:
            raise ValueError("RDP clock recovery observed_at must be monotonic.")
        if (buffered_output_samples is None) != (target_buffer_samples is None):
            raise ValueError("RDP clock recovery buffer feedback requires both current and target samples.")
        if buffered_output_samples is not None and buffered_output_samples < 0:
            raise ValueError("buffered_output_samples must not be negative.")
        if target_buffer_samples is not None and target_buffer_samples <= 0:
            raise ValueError("target_buffer_samples must be greater than 0.")

        self._input_sample_count += sample_count
        self._last_observed_at = observed_at
        self._buffered_output_samples = buffered_output_samples
        if self._observations and observed_at == self._observations[-1][0]:
            self._observations[-1] = (observed_at, self._input_sample_count)
        else:
            self._observations.append((observed_at, self._input_sample_count))

        window_start = observed_at - self._estimation_window_seconds
        # 保留跨过窗口起点的旧点，避免批量刷新时速率回归丢失窗口边界。
        while len(self._observations) > 2 and self._observations[1][0] < window_start:
            self._observations.popleft()

        if len(self._observations) < 2:
            return self._wsola_speed
        observation_duration = self._observations[-1][0] - self._observations[0][0]
        if observation_duration < self._calibration_seconds:
            return self._wsola_speed
        if (
            self._last_adjustment_at is not None
            and observed_at - self._last_adjustment_at < self._adjustment_interval_seconds
        ):
            return self._wsola_speed

        measured_input_rate = self._estimate_input_rate()
        if measured_input_rate is None:
            return self._wsola_speed
        minimum_rate = self._nominal_input_rate * (1 - self._max_rate_deviation_fraction)
        maximum_rate = self._nominal_input_rate * (1 + self._max_rate_deviation_fraction)
        bounded_input_rate = min(max(measured_input_rate, minimum_rate), maximum_rate)
        if self._estimated_input_rate is None:
            estimated_input_rate = bounded_input_rate
        else:
            estimated_input_rate = (
                self._estimated_input_rate * (1 - _RATE_ESTIMATE_SMOOTHING_FACTOR)
                + bounded_input_rate * _RATE_ESTIMATE_SMOOTHING_FACTOR
            )
        self._estimated_input_rate = estimated_input_rate
        self._last_adjustment_at = observed_at

        relative_deviation = abs(estimated_input_rate - self._nominal_input_rate) / self._nominal_input_rate
        base_speed = 1.0 if relative_deviation < _RATE_CORRECTION_DEADBAND_FRACTION else (
            estimated_input_rate / self._nominal_input_rate
        )
        desired_speed = self._apply_buffer_feedback(
            base_speed,
            buffered_output_samples=buffered_output_samples,
            target_buffer_samples=target_buffer_samples,
        )
        desired_speed = min(max(desired_speed, self._min_wsola_speed), self._max_wsola_speed)
        next_speed = min(
            max(desired_speed, self._wsola_speed - self._max_speed_step),
            self._wsola_speed + self._max_speed_step,
        )
        self._clock_recovery_active = abs(next_speed - 1.0) >= _RATE_CORRECTION_DEADBAND_FRACTION
        if next_speed != self._wsola_speed:
            self._wsola_speed = next_speed
            self._adjustment_count += 1
        return self._wsola_speed

    def _apply_buffer_feedback(
        self,
        base_speed: float,
        *,
        buffered_output_samples: int | None,
        target_buffer_samples: int | None,
    ) -> float:
        """让缓冲水位只做小幅调速，避免短时抖动覆盖长期速率判断。"""

        if buffered_output_samples is None or target_buffer_samples is None:
            return base_speed
        buffer_error_seconds = (buffered_output_samples - target_buffer_samples) / self._output_rate
        feedback_fraction = min(
            max(buffer_error_seconds * _BUFFER_FEEDBACK_GAIN_PER_SECOND, -_MAX_BUFFER_FEEDBACK_FRACTION),
            _MAX_BUFFER_FEEDBACK_FRACTION,
        )
        # 高水位需要更快消费，低水位需要更慢输出；WSOLA 的速度方向正好如此。
        return base_speed * (1 + feedback_fraction)

    def _estimate_input_rate(self) -> float | None:
        """用窗口内累计样本对墙钟做线性回归，降低批量写入造成的瞬时抖动。"""

        if len(self._observations) < 2:
            return None
        first_time = self._observations[0][0]
        first_sample_count = self._observations[0][1]
        times = [observed_at - first_time for observed_at, _ in self._observations]
        sample_counts = [sample_count - first_sample_count for _, sample_count in self._observations]
        mean_time = sum(times) / len(times)
        mean_samples = sum(sample_counts) / len(sample_counts)
        denominator = sum((value - mean_time) ** 2 for value in times)
        if denominator <= 0:
            return None
        measured_input_rate = sum(
            (time_value - mean_time) * (sample_value - mean_samples)
            for time_value, sample_value in zip(times, sample_counts)
        ) / denominator
        if not math.isfinite(measured_input_rate) or measured_input_rate <= 0:
            return None
        return measured_input_rate
