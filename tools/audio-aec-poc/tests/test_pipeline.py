from __future__ import annotations

import json
import wave
from pathlib import Path

import numpy as np

from audio_aec_poc.devices import DeviceInfo
from audio_aec_poc.pipeline import PipelineConfig, float32_to_pcm16, resample_pcm16_mono, run_capture_pipeline


class FakeCapture:
    """为采集管线提供可控音频帧的测试替身。

    功能：模拟 SoundcardCapture 的上下文协议、设备信息和双路读取接口，避免测试触碰真实音频设备。
    核心入参：sample_rate、channels、frame_samples 来自被测管线，speaker_frames 和 microphone_frames 是测试预置帧序列。
    预期输出：read_speaker_reference 与 read_microphone 按顺序返回 numpy 音频帧。
    边界异常：当测试读取次数超过预置帧数时，列表索引异常会暴露测试数据配置错误。
    """

    def __init__(
        self,
        *,
        sample_rate: int,
        channels: int,
        frame_samples: int,
        speaker_frames: list[np.ndarray],
        microphone_frames: list[np.ndarray],
    ) -> None:
        """保存测试帧和管线传入的采集参数。

        功能：记录初始化参数，便于断言管线按固定 10ms 帧长创建采集器。
        核心入参：sample_rate、channels、frame_samples 是采集配置，两组帧列表是双路输入数据。
        预期输出：生成可进入 with 块的假采集器实例。
        边界异常：本替身不校验帧列表长度，读取越界代表测试自身配置不完整。
        """
        # 采集参数用于测试确认管线把配置正确传给 capture_factory。
        self.sample_rate = sample_rate
        self.channels = channels
        self.frame_samples = frame_samples
        # 两路帧序列分别模拟扬声器 loopback 参考流和麦克风近端输入流。
        self._speaker_frames = speaker_frames
        self._microphone_frames = microphone_frames
        # 读取游标按帧推进，保证同一轮循环读取到同一时间片的两路音频。
        self._speaker_index = 0
        self._microphone_index = 0

    @property
    def device_info(self) -> DeviceInfo:
        """返回稳定的假设备信息。

        功能：模拟真实采集器暴露的 device_info 属性，用于 manifest 记录设备来源。
        核心入参：无显式入参。
        预期输出：返回包含固定麦克风和扬声器名称的 DeviceInfo。
        边界异常：本属性不访问真实系统设备，因此不会因设备缺失抛错。
        """
        # 固定名称让 manifest 断言稳定，不受运行机器默认设备影响。
        return DeviceInfo(microphone_name="Fake Microphone", speaker_name="Fake Speaker")

    def __enter__(self) -> "FakeCapture":
        """进入假采集上下文。

        功能：模拟真实采集器打开设备资源的生命周期入口。
        核心入参：无显式入参。
        预期输出：返回当前实例供管线读取音频帧。
        边界异常：本替身不模拟设备打开失败，失败路径不属于当前管线测试重点。
        """
        # 假采集器没有外部资源需要打开，直接返回自身即可。
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        """退出假采集上下文。

        功能：模拟真实采集器释放设备资源的生命周期出口。
        核心入参：exc_type、exc_value、traceback 是上下文协议传入的异常信息。
        预期输出：返回 None，保留 with 块内的原始异常传播行为。
        边界异常：本替身没有外部资源，退出阶段不会主动抛错。
        """
        # 返回 None 表示不吞掉管线内部异常。
        return None

    def read_speaker_reference(self) -> np.ndarray:
        """读取下一帧扬声器参考音频。

        功能：按预置顺序返回一帧 loopback 参考数据。
        核心入参：无显式入参。
        预期输出：返回 numpy.float32 兼容数组，形状可故意短于或长于 10ms。
        边界异常：读取超过预置帧数会抛出 IndexError，提示测试用例数据不足。
        """
        # 先取当前游标的帧，再推进游标，模拟逐帧采集。
        frame = self._speaker_frames[self._speaker_index]
        self._speaker_index += 1
        return frame

    def read_microphone(self) -> np.ndarray:
        """读取下一帧麦克风音频。

        功能：按预置顺序返回一帧近端输入数据。
        核心入参：无显式入参。
        预期输出：返回 numpy.float32 兼容数组，供管线规整后送入 AEC。
        边界异常：读取超过预置帧数会抛出 IndexError，提示测试用例数据不足。
        """
        # 麦克风游标独立推进，保持接口行为和真实双 recorder 读取一致。
        frame = self._microphone_frames[self._microphone_index]
        self._microphone_index += 1
        return frame

    def read_frame_pair(self) -> tuple[np.ndarray, np.ndarray]:
        """按生产采集器契约原子返回下一组双路测试帧。

        Returns:
            依次返回扬声器参考帧和麦克风帧。
        """

        reference_frame = self.read_speaker_reference()
        microphone_frame = self.read_microphone()
        return reference_frame, microphone_frame


class FakeAec:
    """记录输入帧并返回可识别清洁音频的 AEC 测试替身。

    功能：验证管线传入精确 10ms PCM16 字节，并提供固定输出供 WAV 写入断言。
    核心入参：sample_rate 与 channels 来自管线配置。
    预期输出：process_frame 返回与麦克风帧同长度的测试字节。
    边界异常：输入字节数不是精确 10ms 时抛出 AssertionError，让测试直接暴露帧规整失败。
    """

    def __init__(self, *, sample_rate: int, channels: int) -> None:
        """保存 AEC 配置并准备调用记录。

        功能：计算单帧字节数，记录管线传入的 AEC 初始化参数。
        核心入参：sample_rate 是采样率，channels 是声道数。
        预期输出：生成可处理测试帧的假 AEC 实例。
        边界异常：本替身假定测试使用可整除 100 的采样率，非法配置由 PipelineConfig 负责拒绝。
        """
        # 单个 10ms PCM16 帧的字节数，用于强制检查管线输出契约。
        self.frame_bytes = (sample_rate // 100) * channels * 2
        # 调用记录保存每一帧参考流和麦克风流，便于测试检查补零和裁剪效果。
        self.calls: list[tuple[bytes, bytes]] = []

    def process_frame(self, reference_pcm16: bytes, microphone_pcm16: bytes) -> bytes:
        """处理一对测试 PCM16 帧。

        功能：断言两路输入都是精确 10ms，并返回固定的清洁麦克风输出。
        核心入参：reference_pcm16 是参考音频，microphone_pcm16 是原始麦克风音频。
        预期输出：返回长度相同、内容可识别的 PCM16 字节。
        边界异常：任一路长度错误时抛出 AssertionError，代表管线未履行帧长规整职责。
        """
        # AEC 的核心输入契约是两路 PCM16 都必须已经被管线修正为精确 10ms。
        assert len(reference_pcm16) == self.frame_bytes
        assert len(microphone_pcm16) == self.frame_bytes
        self.calls.append((reference_pcm16, microphone_pcm16))
        # 返回固定样本值，方便测试确认 mic_clean.wav 写的是 AEC 输出而不是原始麦克风。
        return b"\x03\x00" * (self.frame_bytes // 2)


def _read_wav(path: Path) -> tuple[bytes, int, int, int]:
    """读取 WAV 文件的原始帧和核心元数据。

    功能：打开管线生成的 WAV，返回帧数据、采样率、声道数和样本宽度。
    核心入参：path 是待检查的 WAV 文件路径。
    预期输出：返回包含 PCM 字节和三个元数据字段的元组。
    边界异常：文件不存在或不是 WAV 时由 wave 模块抛出异常，测试据此失败。
    """
    # 使用标准库 wave 读取可以同时验证文件头和数据块是否可解析。
    with wave.open(str(path), "rb") as wav_file:
        return (
            wav_file.readframes(wav_file.getnframes()),
            wav_file.getframerate(),
            wav_file.getnchannels(),
            wav_file.getsampwidth(),
        )


def test_float32_to_pcm16_clips_and_scales_to_little_endian() -> None:
    """验证 float32 音频转换会裁剪到合法范围，并输出 PCM16 小端字节。"""
    # 输入覆盖越界负值、边界值、零点、正半幅和越界正值，确保裁剪与缩放同时生效。
    frame = np.array([[-2.0], [-1.0], [-0.5], [0.0], [0.5], [1.0], [2.0]], dtype=np.float32)

    # 转换结果应为 int16 小端格式，供 WAV 写入器和 LiveKit AEC 直接消费。
    pcm16 = float32_to_pcm16(frame)

    # 从字节反解回小端 int16，避免测试只比较平台相关的原始字节表现。
    assert np.frombuffer(pcm16, dtype="<i2").tolist() == [-32767, -32767, -16383, 0, 16383, 32767, 32767]


def test_pipeline_config_defaults_to_48k_aec_and_2s_warmup(tmp_path) -> None:
    """验证产品默认策略使用 48k 内部 AEC，并在正式录制前预热 AEC。"""
    # 默认配置代表未来产品的通用策略，应优先适配系统音频常见的 48k 管线。
    config = PipelineConfig(duration_seconds=1.0, output_dir=tmp_path)

    assert config.sample_rate == 48000
    assert config.warmup_seconds == 2.0


def test_pipeline_config_rejects_negative_warmup_seconds(tmp_path) -> None:
    """验证 warmup 不能为负数，避免帧数计算出现反向时间。"""
    # 负数预热没有业务意义，应该在打开设备和创建输出目录前直接拒绝。
    config = PipelineConfig(duration_seconds=1.0, output_dir=tmp_path, warmup_seconds=-0.01)

    try:
        config.validate()
    except ValueError as exc:
        assert "warmup_seconds" in str(exc)
    else:
        raise AssertionError("negative warmup_seconds should fail validation")


def test_resample_pcm16_mono_downsamples_clean_audio_for_listening() -> None:
    """验证 clean 音频可以从 AEC 内部采样率降到单独指定的输出采样率。"""
    # 这组样本代表 48k 下 480 个单声道 PCM16 样本，降到 16k 后应得到 160 个样本。
    source_pcm16 = np.linspace(-12000, 12000, num=480, dtype=np.int16).astype("<i2").tobytes()

    resampled = resample_pcm16_mono(source_pcm16, source_sample_rate=48000, target_sample_rate=16000)

    assert len(np.frombuffer(resampled, dtype="<i2")) == 160


def test_pipeline_config_rejects_invalid_clean_output_sample_rate(tmp_path) -> None:
    """验证 clean 输出采样率必须能形成 10ms 整数帧。"""
    # clean 输出仍然按 10ms 帧逐帧写入，采样率不能整除 100 会破坏每帧样本数。
    config = PipelineConfig(
        duration_seconds=1.0,
        output_dir=tmp_path,
        clean_output_sample_rate=44101,
    )

    try:
        config.validate()
    except ValueError as exc:
        assert "clean_output_sample_rate" in str(exc)
    else:
        raise AssertionError("invalid clean_output_sample_rate should fail validation")


def test_run_capture_pipeline_writes_wavs_and_manifest(tmp_path) -> None:
    """验证完整采集管线会写出三路 WAV 和包含设备、AEC、帧统计的 manifest。"""
    # 两帧 10ms 单声道输入用于验证持续运行时帧计数、WAV 拼接和 manifest 写出。
    speaker_frames = [
        np.array([[0.1], [0.2], [0.3], [0.4]], dtype=np.float32),
        np.array([[0.5], [0.6], [0.7], [0.8]], dtype=np.float32),
    ]
    microphone_frames = [
        np.array([[-0.1], [-0.2], [-0.3], [-0.4]], dtype=np.float32),
        np.array([[-0.5], [-0.6], [-0.7], [-0.8]], dtype=np.float32),
    ]
    # 保存替身实例，便于运行后检查管线传入的采集参数和 AEC 调用次数。
    captures: list[FakeCapture] = []
    aecs: list[FakeAec] = []

    def capture_factory(*, sample_rate: int, channels: int, frame_samples: int) -> FakeCapture:
        """创建带固定测试帧的采集替身。

        功能：模拟管线注入的 capture_factory，并记录实例用于断言。
        核心入参：sample_rate、channels、frame_samples 是管线计算出的采集参数。
        预期输出：返回 FakeCapture 实例。
        边界异常：本工厂不模拟构造失败。
        """
        # 将实例保存到外层列表，测试结束后可检查初始化参数是否符合配置。
        capture = FakeCapture(
            sample_rate=sample_rate,
            channels=channels,
            frame_samples=frame_samples,
            speaker_frames=speaker_frames,
            microphone_frames=microphone_frames,
        )
        captures.append(capture)
        return capture

    def aec_factory(*, sample_rate: int, channels: int) -> FakeAec:
        """创建记录调用的 AEC 替身。

        功能：模拟 LiveKitAecProcessor 工厂，让测试不依赖真实 APM。
        核心入参：sample_rate 和 channels 是管线传给 AEC 的音频配置。
        预期输出：返回 FakeAec 实例。
        边界异常：本工厂不模拟构造失败。
        """
        # 将 AEC 实例保存下来，便于检查每帧输入是否来自管线转换后的 PCM16。
        aec = FakeAec(sample_rate=sample_rate, channels=channels)
        aecs.append(aec)
        return aec

    # 400Hz 采样率让 10ms 只有 4 个样本，测试数据短小但仍遵守管线的帧长规则。
    config = PipelineConfig(
        duration_seconds=0.02,
        output_dir=tmp_path,
        sample_rate=400,
        clean_output_sample_rate=400,
        channels=1,
        warmup_seconds=0,
    )

    # 执行两帧采集，业务目标是一次运行产生独立目录和完整落盘产物。
    result = run_capture_pipeline(config, capture_factory=capture_factory, aec_factory=aec_factory)

    # 返回结果应指出实际运行目录和处理帧数，供 CLI 或上层界面展示。
    assert result.frames_processed == 2
    assert result.run_dir.parent == tmp_path
    assert captures[0].frame_samples == 4
    assert len(aecs[0].calls) == 2

    # 三个 WAV 都必须存在且使用调用方配置的采样率、声道数和 PCM16 样本宽度。
    mic_raw, mic_rate, mic_channels, mic_width = _read_wav(result.run_dir / "mic_raw.wav")
    speaker_ref, speaker_rate, speaker_channels, speaker_width = _read_wav(result.run_dir / "speaker_ref.wav")
    mic_clean, clean_rate, clean_channels, clean_width = _read_wav(result.run_dir / "mic_clean.wav")
    assert (mic_rate, mic_channels, mic_width) == (400, 1, 2)
    assert (speaker_rate, speaker_channels, speaker_width) == (400, 1, 2)
    assert (clean_rate, clean_channels, clean_width) == (400, 1, 2)
    assert mic_raw == b"".join(call[1] for call in aecs[0].calls)
    assert speaker_ref == b"".join(call[0] for call in aecs[0].calls)
    assert mic_clean == b"\x03\x00" * 8

    # manifest 是后续人工诊断和自动验收的入口，必须包含运行参数、设备信息和处理统计。
    manifest = json.loads((result.run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["duration_seconds"] == 0.02
    assert manifest["sample_rate"] == 400
    assert manifest["channels"] == 1
    assert manifest["frame_duration_ms"] == 10
    assert manifest["frames_processed"] == 2
    assert manifest["device_info"] == {
        "microphone_name": "Fake Microphone",
        "speaker_name": "Fake Speaker",
    }
    assert manifest["aec"] == {"processor": "FakeAec"}
    assert manifest["frame_adjustments"] == {
        "speaker_reference_padded_frames": 0,
        "speaker_reference_trimmed_frames": 0,
        "microphone_padded_frames": 0,
        "microphone_trimmed_frames": 0,
    }


def test_run_capture_pipeline_writes_clean_wav_at_requested_output_sample_rate(tmp_path) -> None:
    """验证 mic_clean.wav 可以使用独立于 AEC 内部采样率的输出采样率。"""
    # AEC 内部使用 600Hz，每个 10ms 是 6 个样本；clean 输出指定 300Hz 后应写出 3 个样本。
    speaker_frames = [np.zeros((6, 1), dtype=np.float32)]
    microphone_frames = [np.zeros((6, 1), dtype=np.float32)]

    class DownsampleFakeAec(FakeAec):
        """返回递增样本，便于断言 clean WAV 是否被降采样。"""

        def process_frame(self, reference_pcm16: bytes, microphone_pcm16: bytes) -> bytes:
            super().process_frame(reference_pcm16, microphone_pcm16)
            return np.array([0, 1000, 2000, 3000, 4000, 5000], dtype="<i2").tobytes()

    def capture_factory(*, sample_rate: int, channels: int, frame_samples: int) -> FakeCapture:
        """创建单帧采集替身，避免触碰真实设备。"""
        return FakeCapture(
            sample_rate=sample_rate,
            channels=channels,
            frame_samples=frame_samples,
            speaker_frames=speaker_frames,
            microphone_frames=microphone_frames,
        )

    def aec_factory(*, sample_rate: int, channels: int) -> DownsampleFakeAec:
        """创建会输出可识别 clean 样本的 AEC 替身。"""
        return DownsampleFakeAec(sample_rate=sample_rate, channels=channels)

    config = PipelineConfig(
        duration_seconds=0.01,
        output_dir=tmp_path,
        sample_rate=600,
        clean_output_sample_rate=300,
        channels=1,
        warmup_seconds=0,
    )

    result = run_capture_pipeline(config, capture_factory=capture_factory, aec_factory=aec_factory)

    mic_clean, clean_rate, clean_channels, clean_width = _read_wav(result.run_dir / "mic_clean.wav")
    assert (clean_rate, clean_channels, clean_width) == (300, 1, 2)
    clean_samples = np.frombuffer(mic_clean, dtype="<i2")
    assert len(clean_samples) == 3
    assert clean_samples.max() > 0

    mic_raw, mic_rate, _, _ = _read_wav(result.run_dir / "mic_raw.wav")
    speaker_ref, speaker_rate, _, _ = _read_wav(result.run_dir / "speaker_ref.wav")
    assert mic_rate == 600
    assert speaker_rate == 600
    assert len(mic_raw) == 12
    assert len(speaker_ref) == 12

    manifest = json.loads((result.run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["sample_rate"] == 600
    assert manifest["clean_output_sample_rate"] == 300


def test_run_capture_pipeline_warms_aec_before_writing_recorded_wavs(tmp_path) -> None:
    """验证 warmup 帧只喂给 AEC，不写入正式试听 WAV。"""
    # 400Hz 下每个 10ms 帧是 4 行；第一帧用于 warmup，第二帧才进入正式输出。
    speaker_frames = [
        np.array([[0.1], [0.1], [0.1], [0.1]], dtype=np.float32),
        np.array([[0.5], [0.5], [0.5], [0.5]], dtype=np.float32),
    ]
    microphone_frames = [
        np.array([[-0.1], [-0.1], [-0.1], [-0.1]], dtype=np.float32),
        np.array([[-0.5], [-0.5], [-0.5], [-0.5]], dtype=np.float32),
    ]
    aecs: list[FakeAec] = []

    def capture_factory(*, sample_rate: int, channels: int, frame_samples: int) -> FakeCapture:
        """创建一帧预热、一帧正式录制的采集替身。"""
        return FakeCapture(
            sample_rate=sample_rate,
            channels=channels,
            frame_samples=frame_samples,
            speaker_frames=speaker_frames,
            microphone_frames=microphone_frames,
        )

    def aec_factory(*, sample_rate: int, channels: int) -> FakeAec:
        """创建可记录 warmup 与正式帧调用顺序的 AEC 替身。"""
        aec = FakeAec(sample_rate=sample_rate, channels=channels)
        aecs.append(aec)
        return aec

    # warmup_seconds 和 duration_seconds 各对应一帧，期望 AEC 被调用两次但 WAV 只写正式帧。
    config = PipelineConfig(
        duration_seconds=0.01,
        output_dir=tmp_path,
        sample_rate=400,
        clean_output_sample_rate=400,
        channels=1,
        warmup_seconds=0.01,
    )

    result = run_capture_pipeline(config, capture_factory=capture_factory, aec_factory=aec_factory)

    assert result.frames_processed == 1
    assert len(aecs[0].calls) == 2
    mic_raw, mic_rate, mic_channels, mic_width = _read_wav(result.run_dir / "mic_raw.wav")
    assert (mic_rate, mic_channels, mic_width) == (400, 1, 2)
    assert np.frombuffer(mic_raw, dtype="<i2").tolist() == [-16383, -16383, -16383, -16383]

    manifest = json.loads((result.run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["warmup_seconds"] == 0.01
    assert manifest["warmup_frames_processed"] == 1
    assert manifest["frames_processed"] == 1


def test_run_capture_pipeline_pads_short_frames_and_trims_long_frames(tmp_path) -> None:
    """验证管线会在 PCM16 转换前把短帧补零、长帧裁剪为精确 10ms。"""
    # 400Hz 下每个 10ms 帧是 4 行；参考帧故意短 2 行，麦克风帧故意多 1 行。
    short_speaker_frame = np.array([[0.25], [0.5]], dtype=np.float32)
    long_microphone_frame = np.array([[0.1], [0.2], [0.3], [0.4], [0.9]], dtype=np.float32)
    aecs: list[FakeAec] = []

    def capture_factory(*, sample_rate: int, channels: int, frame_samples: int) -> FakeCapture:
        """创建含短帧和长帧的采集替身。

        功能：为帧规整测试提供偏离 10ms 长度的输入。
        核心入参：sample_rate、channels、frame_samples 是管线传入的采集配置。
        预期输出：返回只包含一帧数据的 FakeCapture。
        边界异常：本工厂不模拟构造失败。
        """
        # 帧数据故意不满足长度要求，测试目标是确认管线在 AEC 前修正它们。
        return FakeCapture(
            sample_rate=sample_rate,
            channels=channels,
            frame_samples=frame_samples,
            speaker_frames=[short_speaker_frame],
            microphone_frames=[long_microphone_frame],
        )

    def aec_factory(*, sample_rate: int, channels: int) -> FakeAec:
        """创建用于断言精确帧长的 AEC 替身。

        功能：记录管线规整后的 PCM16 输入。
        核心入参：sample_rate 和 channels 是管线传给 AEC 的音频配置。
        预期输出：返回 FakeAec 实例。
        边界异常：本工厂不模拟构造失败。
        """
        # AEC 替身会在 process_frame 内断言输入长度已经是精确 10ms。
        aec = FakeAec(sample_rate=sample_rate, channels=channels)
        aecs.append(aec)
        return aec

    # 运行单帧即可覆盖补零和裁剪两个方向的帧长修正。
    config = PipelineConfig(
        duration_seconds=0.01,
        output_dir=tmp_path,
        sample_rate=400,
        clean_output_sample_rate=400,
        channels=1,
        warmup_seconds=0,
    )

    # 如果管线没有规整到精确 10ms，FakeAec 会通过断言让测试失败。
    result = run_capture_pipeline(config, capture_factory=capture_factory, aec_factory=aec_factory)

    # 参考帧末尾应补两个静音样本，麦克风帧应丢弃第 5 个样本。
    reference_pcm16, microphone_pcm16 = aecs[0].calls[0]
    assert np.frombuffer(reference_pcm16, dtype="<i2").tolist() == [8191, 16383, 0, 0]
    assert np.frombuffer(microphone_pcm16, dtype="<i2").tolist() == [3276, 6553, 9830, 13106]

    # manifest 中的修正统计用于诊断真实设备偶发短读或过读问题。
    manifest = json.loads((result.run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["frame_adjustments"]["speaker_reference_padded_frames"] == 1
    assert manifest["frame_adjustments"]["microphone_trimmed_frames"] == 1
