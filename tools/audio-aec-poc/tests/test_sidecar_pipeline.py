from __future__ import annotations

import argparse
import asyncio
import json
import wave
from pathlib import Path

import numpy as np
import pytest

from audio_aec_sidecar import debug_recorder as debug_recorder_module
from audio_aec_sidecar import main as sidecar_main
from audio_aec_poc.session_transport import CaptureProfile
from audio_aec_sidecar.config import SidecarConfig
from audio_aec_sidecar.debug_recorder import SidecarDebugRecorder
from audio_aec_sidecar.main import build_parser
from audio_aec_sidecar.pipeline import SidecarPipelineResult, run_sidecar_pipeline_once


class FakeCapture:
    """为侧车运行管线提供可控双路音频帧的测试替身。

    功能：模拟真实 soundcard 采集器的上下文协议，并按顺序返回扬声器参考帧与麦克风帧。
    核心入参：sample_rate、channels、frame_samples 来自被测管线，speaker_frames 和 microphone_frames 是测试预置帧序列。
    预期输出：read_speaker_reference 与 read_microphone 在每次主循环中返回一组同步帧。
    边界异常：当测试读取次数超过预置帧数时，列表索引错误会暴露测试数据不足。
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
        """保存采集参数和测试音频帧。

        功能：记录管线传入的采集配置，并初始化双路读取游标。
        核心入参：sample_rate、channels、frame_samples 是采集契约，两组帧列表是待返回的音频数据。
        预期输出：创建可进入 with 块的 FakeCapture 实例。
        边界异常：本替身不校验帧列表，越界读取代表测试用例自身配置错误。
        """
        # 这些字段用于断言运行管线按 10ms 固定帧创建采集器。
        self.sample_rate = sample_rate
        self.channels = channels
        self.frame_samples = frame_samples
        # 两路帧分别代表系统播放参考和麦克风原始输入。
        self._speaker_frames = speaker_frames
        self._microphone_frames = microphone_frames
        # 双路游标独立推进，贴近真实采集器的两个读取方法。
        self._speaker_index = 0
        self._microphone_index = 0

    def __enter__(self) -> "FakeCapture":
        """进入假采集上下文。

        功能：模拟真实采集器打开音频设备的生命周期入口。
        核心入参：无。
        预期输出：返回当前实例供管线读取帧。
        边界异常：本替身不模拟设备打开失败。
        """
        # 测试替身没有外部资源，直接返回自身即可。
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        """退出假采集上下文。

        功能：模拟真实采集器释放设备资源的生命周期出口。
        核心入参：exc_type、exc_value、traceback 是上下文协议传入的异常信息。
        预期输出：返回 None，保留 with 块内原始异常传播。
        边界异常：本替身没有外部资源，退出阶段不主动抛错。
        """
        # 返回 None 表示不吞掉管线内部异常。
        return None

    def read_speaker_reference(self) -> np.ndarray:
        """读取下一帧扬声器参考音频。

        功能：按预置顺序返回一帧 loopback 参考数据。
        核心入参：无。
        预期输出：返回 numpy.float32 兼容数组。
        边界异常：读取超过预置帧数时抛出 IndexError。
        """
        # 先取当前帧再推进游标，模拟逐帧采集。
        frame = self._speaker_frames[self._speaker_index]
        self._speaker_index += 1
        return frame

    def read_microphone(self) -> np.ndarray:
        """读取下一帧麦克风原始音频。

        功能：按预置顺序返回一帧近端输入数据。
        核心入参：无。
        预期输出：返回 numpy.float32 兼容数组。
        边界异常：读取超过预置帧数时抛出 IndexError。
        """
        # 麦克风游标独立推进，保证测试能观察双路读取次数。
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
    """记录输入帧并返回固定 clean PCM16 的 AEC 测试替身。

    功能：验证管线传入精确 10ms PCM16 字节，并提供可识别输出供发布和 WAV 断言。
    核心入参：sample_rate 与 channels 来自被测管线。
    预期输出：process_frame 返回与输入帧同长度的 clean PCM16 字节。
    边界异常：任一路输入不是精确 10ms 时抛出 AssertionError。
    """

    def __init__(self, *, sample_rate: int, channels: int) -> None:
        """初始化帧长契约与调用记录。

        功能：计算单帧 PCM16 字节数，并准备记录所有 AEC 调用。
        核心入参：sample_rate 是 AEC 内部采样率，channels 是声道数。
        预期输出：创建可处理测试帧的 FakeAec。
        边界异常：非法采样率由 SidecarConfig.validate 负责提前拒绝。
        """
        # 10ms PCM16 字节数用于强制检查运行管线的切帧契约。
        self.frame_bytes = (sample_rate // 100) * channels * 2
        # 调用日志用于区分 warmup 和正式发布阶段是否都喂给 AEC。
        self.calls: list[tuple[bytes, bytes]] = []

    def process_frame(self, reference_pcm16: bytes, microphone_pcm16: bytes) -> bytes:
        """处理一组测试 PCM16 帧。

        功能：断言双路输入都是精确 10ms，记录调用，并返回固定 clean 音频。
        核心入参：reference_pcm16 是扬声器参考，microphone_pcm16 是麦克风原始输入。
        预期输出：返回固定样本值构成的 clean PCM16。
        边界异常：任一路字节数不等于单帧长度时抛出 AssertionError。
        """
        # AEC 输入必须在管线内完成规整，否则真实 LiveKit APM 无法稳定处理。
        assert len(reference_pcm16) == self.frame_bytes
        assert len(microphone_pcm16) == self.frame_bytes
        self.calls.append((reference_pcm16, microphone_pcm16))
        # 固定输出让发布内容和 debug mic_clean.wav 的断言稳定。
        return b"\x05\x00" * (self.frame_bytes // 2)


class FakePublisher:
    """记录侧车管线发布出的 clean PCM16。

    功能：模拟 CleanMicPublisher 的异步 publish_pcm16 方法，避免测试连接 LiveKit。
    核心入参：publish_pcm16 接收管线重采样后的 clean PCM16 字节。
    预期输出：published_frames 按顺序保存所有发布帧。
    边界异常：本替身不模拟网络失败，当前测试只关注运行时编排行为。
    """

    def __init__(self) -> None:
        """初始化空发布日志。

        功能：为每个测试提供独立的 clean 帧记录列表。
        核心入参：无。
        预期输出：published_frames 可用于断言发布次数和字节长度。
        边界异常：无业务异常。
        """
        # 发布日志是测试观察 pipeline 输出的唯一入口。
        self.published_frames: list[bytes] = []

    async def publish_pcm16(self, pcm16: bytes) -> None:
        """记录一帧 clean PCM16。

        功能：模拟异步发布，将传入数据转成不可变 bytes 后追加到日志。
        核心入参：pcm16 是待发布的 clean 麦克风音频。
        预期输出：published_frames 增加一条记录。
        边界异常：本替身不主动抛错。
        """
        # 转成 bytes，避免调用方传入可变缓冲区后影响断言。
        self.published_frames.append(bytes(pcm16))


def make_sidecar_config(**overrides) -> SidecarConfig:
    """构造默认合法的侧车配置，允许测试覆盖目标字段。

    功能：集中维护运行管线测试所需的 LiveKit、父进程和音频参数默认值。
    核心入参：overrides 是需要覆盖的 SidecarConfig 字段键值对。
    预期输出：返回 SidecarConfig 实例。
    边界异常：未知字段由 dataclass 构造器抛出 TypeError。
    """
    # 默认身份满足 desktop-aec- 前缀，避免每个测试重复填充连接样板。
    values = {
        "livekit_url": "wss://livekit.example",
        "token": "test-token",
        "room_name": "desktop-room",
        "identity": "desktop-aec-worker",
        "parent_pid": 1234,
        "sample_rate": 400,
        "clean_output_sample_rate": 400,
        "warmup_seconds": 0.0,
    }
    # 单个测试只覆盖自己关心的配置字段，失败原因更聚焦。
    values.update(overrides)
    return SidecarConfig(**values)


def read_wav(path: Path) -> tuple[bytes, int, int, int]:
    """读取 WAV 文件的 PCM 字节和核心元数据。

    功能：用标准库 wave 验证 debug recorder 生成的文件可解析且元数据正确。
    核心入参：path 是目标 WAV 路径。
    预期输出：返回原始帧字节、采样率、声道数和样本宽度。
    边界异常：文件不存在或不是合法 WAV 时由 wave 模块抛出异常。
    """
    # 标准库读取能同时验证 WAV 头和音频数据块。
    with wave.open(str(path), "rb") as wav_file:
        return (
            wav_file.readframes(wav_file.getnframes()),
            wav_file.getframerate(),
            wav_file.getnchannels(),
            wav_file.getsampwidth(),
        )


def only_child_directory(path: Path) -> Path:
    """返回目录下唯一的子运行目录。

    功能：验证 sidecar debug 输出基目录只包含一个 run-id 子目录，并把该子目录返回给调用方继续读取 WAV。
    核心入参：path 是配置传入的 debug_output_dir 基目录。
    预期输出：返回唯一子目录 Path。
    边界异常：子目录数量不是 1 时通过 AssertionError 让测试失败，暴露运行目录隔离策略错误。
    """
    # 只收集目录，避免未来测试或平台生成的非目录文件影响 run-id 结构判断。
    child_directories = [child for child in path.iterdir() if child.is_dir()]
    assert len(child_directories) == 1
    return child_directories[0]


def test_debug_recorder_rejects_write_before_open(tmp_path) -> None:
    """验证 debug recorder 未进入上下文时禁止写入三路 PCM。"""
    # 未打开时写入代表调用方生命周期错误，必须明确抛错而不是静默丢失调试音频。
    recorder = SidecarDebugRecorder(tmp_path, sample_rate=400, clean_sample_rate=400, channels=1)

    with pytest.raises(RuntimeError):
        recorder.write_frames(mic_raw=b"\x00\x00", speaker_ref=b"\x00\x00", mic_clean=b"\x00\x00")


def test_debug_recorder_closes_remaining_writers_when_one_close_fails(tmp_path, monkeypatch) -> None:
    """验证 debug recorder 关闭某一路失败时仍会继续关闭其余已打开写入器。"""
    # 关闭异常列表用于模拟 clean 文件关闭失败，同时观察其它两路 writer 是否仍被关闭。
    close_log: list[str] = []

    class FakeWavWriter:
        """模拟 Pcm16WavWriter 的上下文协议并记录关闭行为。

        功能：根据文件名决定关闭时是否抛错，帮助测试 recorder 的 best-effort 关闭策略。
        核心入参：path 是 SidecarDebugRecorder 传入的目标 WAV 路径，其余音频参数仅用于保持构造签名兼容。
        预期输出：__exit__ 被调用时把文件名写入 close_log。
        边界异常：关闭 mic_clean.wav 时抛出 RuntimeError，用于验证异常不会阻断后续 writer 关闭。
        """

        def __init__(self, path: Path, *, sample_rate: int, channels: int) -> None:
            """保存目标路径和音频参数。

            功能：记录当前 fake writer 对应的文件名，保持与真实 Pcm16WavWriter 相同的构造入口。
            核心入参：path 是目标 WAV 路径，sample_rate 与 channels 是 WAV 元数据。
            预期输出：生成可进入上下文的 fake writer。
            边界异常：构造阶段不抛错，当前测试只覆盖关闭阶段异常。
            """
            # 文件名是测试判断关闭顺序和关闭覆盖范围的稳定标识。
            self.name = path.name

        def __enter__(self) -> "FakeWavWriter":
            """进入 fake writer 上下文。

            功能：模拟真实 writer 打开文件并返回自身。
            核心入参：无。
            预期输出：返回当前 fake writer。
            边界异常：不模拟打开失败。
            """
            return self

        def __exit__(self, exc_type, exc_value, traceback) -> None:
            """记录关闭并在 clean writer 上抛出测试异常。

            功能：模拟某一路 WAV 关闭失败，同时让测试确认其它 writer 仍会收到关闭调用。
            核心入参：exc_type、exc_value、traceback 是上下文协议传入的异常信息。
            预期输出：close_log 增加当前文件名。
            边界异常：mic_clean.wav 关闭时抛出 RuntimeError。
            """
            # 每一路关闭尝试都必须记录，测试据此判断是否做到了 best-effort。
            close_log.append(self.name)
            if self.name == "mic_clean.wav":
                raise RuntimeError("clean close failed")
            return None

        def write(self, pcm16: bytes) -> None:
            """模拟写入接口。

            功能：保持 fake writer 与真实 writer 的最小协议兼容。
            核心入参：pcm16 是待写入音频字节。
            预期输出：不记录数据，当前测试只关注关闭。
            边界异常：不模拟写入失败。
            """
            # 当前测试不需要验证写入内容，保留方法即可满足 recorder 调用协议。
            _ = pcm16

    # 替换 recorder 模块持有的 writer 类，避免测试依赖真实 WAV 文件关闭行为。
    monkeypatch.setattr(debug_recorder_module, "Pcm16WavWriter", FakeWavWriter)

    recorder = SidecarDebugRecorder(tmp_path, sample_rate=400, clean_sample_rate=400, channels=1)
    recorder.__enter__()

    with pytest.raises(RuntimeError, match="clean close failed"):
        recorder.__exit__(None, None, None)

    # 即使 clean 关闭先失败，speaker_ref 与 mic_raw 也必须继续关闭，确保 WAV 头尽可能完整写回。
    assert close_log == ["mic_clean.wav", "speaker_ref.wav", "mic_raw.wav"]


def test_run_sidecar_pipeline_warms_aec_then_publishes_clean_frames(tmp_path) -> None:
    """验证侧车管线先预热 AEC，再发布并调试记录正式 clean 麦克风帧。"""
    # 三帧输入中第一帧用于 warmup，后两帧进入正式发布阶段。
    speaker_frames = [
        np.full((4, 1), 0.1, dtype=np.float32),
        np.full((4, 1), 0.2, dtype=np.float32),
        np.full((4, 1), 0.3, dtype=np.float32),
    ]
    microphone_frames = [
        np.full((4, 1), -0.1, dtype=np.float32),
        np.full((4, 1), -0.2, dtype=np.float32),
        np.full((4, 1), -0.3, dtype=np.float32),
    ]
    # 外层列表保存工厂创建的实例，运行后用于检查初始化参数和调用次数。
    captures: list[FakeCapture] = []
    aecs: list[FakeAec] = []

    def capture_factory(*, sample_rate: int, channels: int, frame_samples: int) -> FakeCapture:
        """创建固定三帧输入的采集替身。

        功能：记录管线传入的采集参数，并返回可控 FakeCapture。
        核心入参：sample_rate、channels、frame_samples 是侧车管线计算出的采集契约。
        预期输出：返回 FakeCapture 实例。
        边界异常：本工厂不模拟构造失败。
        """
        # 保存实例，便于测试确认 400Hz 下 10ms 帧长为 4 个样本。
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

        功能：让测试不依赖真实 LiveKit APM，同时能观察 warmup 与主循环调用总数。
        核心入参：sample_rate 和 channels 是管线传给 AEC 的配置。
        预期输出：返回 FakeAec 实例。
        边界异常：本工厂不模拟构造失败。
        """
        # 保存实例，运行后断言 AEC 收到 warmup 加正式帧共三次调用。
        aec = FakeAec(sample_rate=sample_rate, channels=channels)
        aecs.append(aec)
        return aec

    publisher = FakePublisher()
    config = make_sidecar_config(warmup_seconds=0.01, debug_output_dir=tmp_path)

    result = asyncio.run(
        run_sidecar_pipeline_once(
            config,
            publisher,
            frame_count=2,
            capture_factory=capture_factory,
            aec_factory=aec_factory,
            parent_alive=lambda: True,
        )
    )

    # 结果摘要应只统计正式发布帧，warmup 不计入发布量。
    assert result.frames_published == 2
    assert result.stop_reason == "frame_count_reached"
    assert captures[0].frame_samples == 4
    assert len(aecs[0].calls) == 3
    assert len(publisher.published_frames) == 2
    assert publisher.published_frames == [b"\x05\x00" * 4, b"\x05\x00" * 4]

    # debug recorder 只记录正式主循环三路音频，并且必须写入独立 run-id 子目录，避免多次运行互相覆盖。
    assert not (tmp_path / "mic_raw.wav").exists()
    assert not (tmp_path / "speaker_ref.wav").exists()
    assert not (tmp_path / "mic_clean.wav").exists()
    debug_run_dir = only_child_directory(tmp_path)
    mic_raw, mic_rate, mic_channels, mic_width = read_wav(debug_run_dir / "mic_raw.wav")
    speaker_ref, speaker_rate, speaker_channels, speaker_width = read_wav(debug_run_dir / "speaker_ref.wav")
    mic_clean, clean_rate, clean_channels, clean_width = read_wav(debug_run_dir / "mic_clean.wav")
    assert (mic_rate, mic_channels, mic_width) == (400, 1, 2)
    assert (speaker_rate, speaker_channels, speaker_width) == (400, 1, 2)
    assert (clean_rate, clean_channels, clean_width) == (400, 1, 2)
    assert mic_raw == b"".join(call[1] for call in aecs[0].calls[1:])
    assert speaker_ref == b"".join(call[0] for call in aecs[0].calls[1:])
    assert mic_clean == b"\x05\x00" * 8


def test_run_sidecar_pipeline_stops_during_warmup_when_parent_exits() -> None:
    """验证 warmup 阶段发现父进程退出时立即停止，不继续采集或喂给 AEC。"""
    # warmup 有两帧输入，但 parent_alive 第一轮就返回 False，管线不应读取任何音频帧。
    speaker_frames = [np.zeros((4, 1), dtype=np.float32), np.zeros((4, 1), dtype=np.float32)]
    microphone_frames = [np.zeros((4, 1), dtype=np.float32), np.zeros((4, 1), dtype=np.float32)]
    aecs: list[FakeAec] = []

    def capture_factory(*, sample_rate: int, channels: int, frame_samples: int) -> FakeCapture:
        """创建理论上用于 warmup 的采集替身。"""
        return FakeCapture(
            sample_rate=sample_rate,
            channels=channels,
            frame_samples=frame_samples,
            speaker_frames=speaker_frames,
            microphone_frames=microphone_frames,
        )

    def aec_factory(*, sample_rate: int, channels: int) -> FakeAec:
        """创建 AEC 替身并保存实例，便于断言 warmup 未处理帧。"""
        aec = FakeAec(sample_rate=sample_rate, channels=channels)
        aecs.append(aec)
        return aec

    publisher = FakePublisher()
    config = make_sidecar_config(warmup_seconds=0.02)

    result = asyncio.run(
        run_sidecar_pipeline_once(
            config,
            publisher,
            frame_count=1,
            capture_factory=capture_factory,
            aec_factory=aec_factory,
            parent_alive=lambda: False,
        )
    )

    # 父进程退出应优先于 warmup 采集，避免宿主刚退出时 sidecar 继续占用设备和 room。
    assert result.frames_published == 0
    assert result.stop_reason == "parent_exited"
    assert aecs[0].calls == []
    assert publisher.published_frames == []


def test_run_sidecar_pipeline_stops_before_reading_when_parent_exits() -> None:
    """验证父进程退出时主循环立即停止，不继续读取、处理或发布音频。"""
    # 只提供一帧数据即可，parent_alive 在主循环开始前返回 False，管线不应读取它。
    speaker_frames = [np.zeros((4, 1), dtype=np.float32)]
    microphone_frames = [np.zeros((4, 1), dtype=np.float32)]
    aecs: list[FakeAec] = []

    def capture_factory(*, sample_rate: int, channels: int, frame_samples: int) -> FakeCapture:
        """创建不会被主循环读取的采集替身。"""
        return FakeCapture(
            sample_rate=sample_rate,
            channels=channels,
            frame_samples=frame_samples,
            speaker_frames=speaker_frames,
            microphone_frames=microphone_frames,
        )

    def aec_factory(*, sample_rate: int, channels: int) -> FakeAec:
        """创建 AEC 替身并保存调用记录。"""
        aec = FakeAec(sample_rate=sample_rate, channels=channels)
        aecs.append(aec)
        return aec

    publisher = FakePublisher()
    config = make_sidecar_config(warmup_seconds=0)

    result = asyncio.run(
        run_sidecar_pipeline_once(
            config,
            publisher,
            frame_count=1,
            capture_factory=capture_factory,
            aec_factory=aec_factory,
            parent_alive=lambda: False,
        )
    )

    # 父进程停止是有业务意义的退出原因，上层 CLI 可据此判断不是设备错误。
    assert result.frames_published == 0
    assert result.stop_reason == "parent_exited"
    assert aecs[0].calls == []
    assert publisher.published_frames == []


def test_run_sidecar_pipeline_resamples_clean_before_publish() -> None:
    """验证侧车管线会在发布前把 clean PCM16 重采样到配置的输出采样率。"""
    # AEC 内部 600Hz 每帧 6 个样本，clean 输出 300Hz 后每帧应发布 3 个样本。
    speaker_frames = [np.zeros((6, 1), dtype=np.float32)]
    microphone_frames = [np.zeros((6, 1), dtype=np.float32)]

    class DownsampleFakeAec(FakeAec):
        """返回递增样本，便于观察重采样后的帧长度。

        功能：覆盖 FakeAec 默认固定输出，让测试能确认发布前确实经过降采样。
        核心入参：沿用 FakeAec 的 sample_rate 与 channels。
        预期输出：process_frame 返回 6 个 PCM16 样本。
        边界异常：输入帧长度错误仍由父类断言暴露。
        """

        def process_frame(self, reference_pcm16: bytes, microphone_pcm16: bytes) -> bytes:
            """返回一帧递增 clean PCM16。

            功能：先执行父类长度检查，再构造 6 个样本的测试输出。
            核心入参：reference_pcm16 与 microphone_pcm16 是精确 10ms 输入帧。
            预期输出：返回源采样率下的 clean PCM16。
            边界异常：任一路输入长度错误时由父类抛出 AssertionError。
            """
            super().process_frame(reference_pcm16, microphone_pcm16)
            # 递增样本避免全零重采样时无法区分是否真的经过转换。
            return np.array([0, 1000, 2000, 3000, 4000, 5000], dtype="<i2").tobytes()

    def capture_factory(*, sample_rate: int, channels: int, frame_samples: int) -> FakeCapture:
        """创建单帧采集替身。"""
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

    publisher = FakePublisher()
    config = make_sidecar_config(sample_rate=600, clean_output_sample_rate=300, warmup_seconds=0)

    result = asyncio.run(
        run_sidecar_pipeline_once(
            config,
            publisher,
            frame_count=1,
            capture_factory=capture_factory,
            aec_factory=aec_factory,
            parent_alive=lambda: True,
        )
    )

    # 发布帧数保持一帧，但字节长度应从源 12 字节降到目标 6 字节。
    assert result.frames_published == 1
    assert len(publisher.published_frames) == 1
    assert len(publisher.published_frames[0]) == 6


def test_pipeline_emits_capture_ready_after_enter_and_before_first_read() -> None:
    """真实管线必须在采集器成功进入后、第一次读取前触发 ready 回调。"""

    lifecycle_events: list[str] = []

    class RecordingCapture(FakeCapture):
        """记录采集上下文进入、读取和退出顺序。"""

        def __enter__(self) -> "RecordingCapture":
            """记录设备已经成功打开。

            Returns:
                当前测试采集器。
            """

            lifecycle_events.append("capture_enter")
            return self

        def read_frame_pair(self) -> tuple[np.ndarray, np.ndarray]:
            """记录第一次双路读取并返回测试帧。

            Returns:
                下一组扬声器参考帧和麦克风帧。
            """

            lifecycle_events.append("first_read")
            return super().read_frame_pair()

        def __exit__(self, exc_type, exc_value, traceback) -> None:
            """记录采集设备退出。

            Args:
                exc_type: 上下文异常类型。
                exc_value: 上下文异常对象。
                traceback: 上下文异常堆栈。

            Returns:
                无返回值。
            """

            _ = exc_type, exc_value, traceback
            lifecycle_events.append("capture_exit")
            return None

    def capture_factory(*, sample_rate: int, channels: int, frame_samples: int) -> RecordingCapture:
        """创建包含一组固定帧的顺序记录采集器。

        Args:
            sample_rate: 管线内部采样率。
            channels: 管线声道数。
            frame_samples: 单个 10ms 帧样本数。

        Returns:
            可完成一次读取的 RecordingCapture。
        """

        return RecordingCapture(
            sample_rate=sample_rate,
            channels=channels,
            frame_samples=frame_samples,
            speaker_frames=[np.zeros((4, 1), dtype=np.float32)],
            microphone_frames=[np.zeros((4, 1), dtype=np.float32)],
        )

    result = asyncio.run(
        run_sidecar_pipeline_once(
            make_sidecar_config(warmup_seconds=0),
            FakePublisher(),
            frame_count=1,
            capture_factory=capture_factory,
            aec_factory=lambda **kwargs: FakeAec(**kwargs),
            on_capture_ready=lambda: lifecycle_events.append("ready"),
        )
    )

    assert result.frames_published == 1
    assert lifecycle_events == ["capture_enter", "ready", "first_read", "capture_exit"]


def test_pipeline_does_not_emit_ready_when_capture_enter_fails() -> None:
    """采集器进入失败时真实管线不能触发 ready 回调。"""

    lifecycle_events: list[str] = []

    class FailingCapture:
        """模拟音频设备打开失败的采集器。"""

        def __init__(self, *, sample_rate: int, channels: int, frame_samples: int) -> None:
            """接收并忽略管线采集参数。

            Args:
                sample_rate: 管线内部采样率。
                channels: 管线声道数。
                frame_samples: 单个 10ms 帧样本数。

            Returns:
                无返回值。
            """

            _ = sample_rate, channels, frame_samples

        def __enter__(self) -> "FailingCapture":
            """模拟设备打开失败。"""

            raise RuntimeError("capture open failed")

        def __exit__(self, exc_type, exc_value, traceback) -> None:
            """保持上下文协议完整。

            Args:
                exc_type: 上下文异常类型。
                exc_value: 上下文异常对象。
                traceback: 上下文异常堆栈。

            Returns:
                无返回值。
            """

            _ = exc_type, exc_value, traceback
            return None

    with pytest.raises(RuntimeError, match="capture open failed"):
        asyncio.run(
            run_sidecar_pipeline_once(
                make_sidecar_config(warmup_seconds=0),
                FakePublisher(),
                frame_count=1,
                capture_factory=FailingCapture,
                aec_factory=lambda **kwargs: FakeAec(**kwargs),
                on_capture_ready=lambda: lifecycle_events.append("ready"),
            )
        )

    assert lifecycle_events == []


def test_run_sidecar_pipeline_creates_unique_windows_safe_debug_run_dirs(tmp_path) -> None:
    """验证同一 debug_output_dir 下连续运行会创建唯一且 Windows 安全的 run-id 子目录。"""
    # debug_base 代表 CLI 传入的稳定调试基目录，连续运行不得把 WAV 直接写到这一层。
    debug_base = tmp_path / "sidecar-debug"
    # Windows 文件名非法字符集合来自平台路径限制；run-id 必须避开这些字符以便真实桌面环境可用。
    illegal_windows_characters = set(':/\\?*"<>|')

    def run_once() -> None:
        """执行一帧可控 sidecar pipeline。

        功能：每次调用都创建新的假采集器和 AEC，向同一个 debug_base 写一组调试 WAV。
        核心入参：无显式入参，闭包读取 debug_base。
        预期输出：debug_base 下新增一个唯一 run-id 子目录，内部包含三路 WAV。
        边界异常：被测 pipeline 的配置、采集、AEC、重采样或写入异常会直接让测试失败。
        """
        # 单帧静音输入足以触发正式主循环、debug recorder 和发布链路。
        speaker_frames = [np.zeros((4, 1), dtype=np.float32)]
        microphone_frames = [np.zeros((4, 1), dtype=np.float32)]

        def capture_factory(*, sample_rate: int, channels: int, frame_samples: int) -> FakeCapture:
            """创建本次运行专属采集替身。

            功能：避免两次 pipeline 运行共享读取游标，保证每次都有完整一帧输入。
            核心入参：sample_rate、channels、frame_samples 是 pipeline 传入的采集契约。
            预期输出：返回 FakeCapture 实例。
            边界异常：本工厂不模拟构造失败。
            """
            return FakeCapture(
                sample_rate=sample_rate,
                channels=channels,
                frame_samples=frame_samples,
                speaker_frames=speaker_frames,
                microphone_frames=microphone_frames,
            )

        def aec_factory(*, sample_rate: int, channels: int) -> FakeAec:
            """创建本次运行专属 AEC 替身。

            功能：让 pipeline 不依赖真实 LiveKit APM，同时输出稳定 clean PCM16。
            核心入参：sample_rate 和 channels 是 AEC 初始化格式。
            预期输出：返回 FakeAec 实例。
            边界异常：本工厂不模拟构造失败。
            """
            return FakeAec(sample_rate=sample_rate, channels=channels)

        # 两次运行复用同一个 debug_base，测试目标是确认 pipeline 会在其下隔离 run-id 子目录。
        config = make_sidecar_config(warmup_seconds=0, debug_output_dir=debug_base)
        result = asyncio.run(
            run_sidecar_pipeline_once(
                config,
                FakePublisher(),
                frame_count=1,
                capture_factory=capture_factory,
                aec_factory=aec_factory,
                parent_alive=lambda: True,
            )
        )

        # 单帧运行必须正常发布完成，否则后续目录断言没有业务意义。
        assert result.frames_published == 1

    run_once()
    run_once()

    # 根目录不应直接出现 WAV，避免多次运行互相覆盖同名调试文件。
    assert not (debug_base / "mic_raw.wav").exists()
    assert not (debug_base / "speaker_ref.wav").exists()
    assert not (debug_base / "mic_clean.wav").exists()

    # 两次运行应各自拥有一个子目录，目录名唯一且不包含 Windows 非法路径字符。
    debug_run_dirs = sorted(child for child in debug_base.iterdir() if child.is_dir())
    assert len(debug_run_dirs) == 2
    assert debug_run_dirs[0].name != debug_run_dirs[1].name
    for debug_run_dir in debug_run_dirs:
        # run-id 目录名必须能安全落在 Windows 文件系统中，尤其不能包含 UTC ISO 字符串常见的冒号。
        assert not (set(debug_run_dir.name) & illegal_windows_characters)
        # 每个运行目录都必须包含完整三路 WAV，证明文件被写在子目录而不是基目录。
        assert (debug_run_dir / "mic_raw.wav").exists()
        assert (debug_run_dir / "speaker_ref.wav").exists()
        assert (debug_run_dir / "mic_clean.wav").exists()


def test_run_sidecar_pipeline_trims_old_debug_run_dirs_by_max_runs(tmp_path) -> None:
    """验证 sidecar 连续多次运行时只保留最近指定数量的 run 目录。"""
    # debug_base 代表 sidecar 按配置写入的固定输出根目录，max_debug_runs 应在其下一层按 run 目录裁剪。
    debug_base = tmp_path / "sidecar-debug"
    created_run_names: list[str] = []

    def run_once() -> None:
        """执行一帧可控 sidecar pipeline，并记录本轮新建的 run 目录名。"""

        def capture_factory(*, sample_rate: int, channels: int, frame_samples: int) -> FakeCapture:
            """创建一帧稳定输入的采集替身。"""
            return FakeCapture(
                sample_rate=sample_rate,
                channels=channels,
                frame_samples=frame_samples,
                speaker_frames=[np.zeros((4, 1), dtype=np.float32)],
                microphone_frames=[np.zeros((4, 1), dtype=np.float32)],
            )

        def aec_factory(*, sample_rate: int, channels: int) -> FakeAec:
            """创建固定 clean 输出的 AEC 替身。"""
            return FakeAec(sample_rate=sample_rate, channels=channels)

        before_names = {
            child.name
            for child in debug_base.iterdir()
            if child.is_dir()
        } if debug_base.exists() else set()

        config = make_sidecar_config(
            warmup_seconds=0,
            debug_output_dir=debug_base,
            max_debug_runs=2,
        )
        result = asyncio.run(
            run_sidecar_pipeline_once(
                config,
                FakePublisher(),
                frame_count=1,
                capture_factory=capture_factory,
                aec_factory=aec_factory,
                parent_alive=lambda: True,
            )
        )

        assert result.frames_published == 1
        after_names = [
            child.name
            for child in debug_base.iterdir()
            if child.is_dir() and child.name not in before_names
        ]
        assert len(after_names) == 1
        created_run_names.append(after_names[0])

    run_once()
    run_once()
    run_once()

    remaining_run_names = sorted(
        child.name
        for child in debug_base.iterdir()
        if child.is_dir()
    )

    assert len(remaining_run_names) == 2
    assert created_run_names[0] not in remaining_run_names
    assert created_run_names[1] in remaining_run_names
    assert created_run_names[2] in remaining_run_names


def test_sidecar_cli_parser_maps_arguments_to_namespace(tmp_path) -> None:
    """验证 sidecar CLI 声明计划要求的 LiveKit、父进程和音频参数。"""
    # parser 只负责解析类型和名称，配置合法性由 SidecarConfig.validate 统一处理。
    parser = build_parser()

    args = parser.parse_args(
        [
            "--livekit-url",
            "wss://livekit.example",
            "--token",
            "token-1",
            "--room-name",
            "room-a",
            "--identity",
            "desktop-aec-worker",
            "--parent-pid",
            "1234",
            "--sample-rate",
            "48000",
            "--clean-output-sample-rate",
            "16000",
            "--warmup-seconds",
            "1.5",
            "--debug-output-dir",
            str(tmp_path),
            "--max-debug-runs",
            "7",
        ]
    )

    # argparse.Namespace 是 CLI 层的稳定输出，后续 main 会按这些字段组装 SidecarConfig。
    assert isinstance(args, argparse.Namespace)
    assert args.livekit_url == "wss://livekit.example"
    assert args.token == "token-1"
    assert args.room_name == "room-a"
    assert args.identity == "desktop-aec-worker"
    assert args.parent_pid == 1234
    assert args.sample_rate == 48000
    assert args.clean_output_sample_rate == 16000
    assert args.warmup_seconds == 1.5
    assert args.debug_output_dir == tmp_path
    assert args.max_debug_runs == 7


def test_run_from_config_disconnects_room_after_success(monkeypatch) -> None:
    """验证 run_from_config 正常结束时会断开 LiveKit room。"""
    # created_rooms 保存 fake room 实例，便于运行后检查 disconnect 是否被调用。
    created_rooms: list[FakeRoom] = []

    class FakeLocalParticipant:
        """模拟 LiveKit local_participant。

        功能：提供 publish_track 协程，记录已发布 track。
        核心入参：publish_track 接收 run_from_config 创建的本地音频轨。
        预期输出：published_tracks 保存发布记录。
        边界异常：当前成功路径不模拟发布失败。
        """

        def __init__(self) -> None:
            """初始化空发布列表。"""
            # 发布列表用于确认 run_from_config 经过了轨道发布阶段。
            self.published_tracks: list[object] = []

        async def publish_track(self, track: object) -> None:
            """记录发布的 track。

            功能：模拟 LiveKit SDK 异步发布本地音轨。
            核心入参：track 是待发布的 fake track。
            预期输出：published_tracks 增加一条记录。
            边界异常：不模拟失败。
            """
            self.published_tracks.append(track)

    class FakeRoom:
        """模拟 LiveKit Room 并记录连接和断开行为。

        功能：替代真实 rtc.Room，避免测试联网，同时暴露 disconnect 调用次数。
        核心入参：connect 接收 LiveKit url 和 token。
        预期输出：connect_calls 和 disconnect_calls 记录生命周期行为。
        边界异常：当前成功路径不模拟异常。
        """

        def __init__(self) -> None:
            """初始化 fake room 状态。"""
            # local_participant 是 run_from_config 发布 track 的入口。
            self.local_participant = FakeLocalParticipant()
            # 生命周期调用记录让测试确认 finally 清理执行。
            self.connect_calls: list[tuple[str, str]] = []
            self.disconnect_calls = 0
            created_rooms.append(self)

        async def connect(self, url: str, token: str) -> None:
            """记录连接参数。"""
            self.connect_calls.append((url, token))

        async def disconnect(self) -> None:
            """记录断开调用。"""
            self.disconnect_calls += 1

    class FakeAudioSource:
        """模拟 LiveKit AudioSource 构造对象。

        功能：保存采样率和声道数，供 CleanMicPublisher 构造时持有。
        核心入参：sample_rate 是 clean 输出采样率，channels 是声道数。
        预期输出：创建轻量 fake source。
        边界异常：不模拟 SDK 参数错误。
        """

        def __init__(self, sample_rate: int, channels: int) -> None:
            """保存音频格式参数。"""
            self.sample_rate = sample_rate
            self.channels = channels

    class FakeLocalAudioTrack:
        """模拟 LiveKit LocalAudioTrack 工厂。"""

        @staticmethod
        def create_audio_track(name: str, audio_source: FakeAudioSource) -> dict[str, object]:
            """创建 fake track。

            功能：保留轨道名和音频源，便于发布阶段传递。
            核心入参：name 是轨道名，audio_source 是 fake 音频源。
            预期输出：返回可发布的字典对象。
            边界异常：不模拟失败。
            """
            return {"name": name, "audio_source": audio_source}

    async def fake_pipeline(*args, **kwargs) -> SidecarPipelineResult:
        """模拟成功结束的 sidecar pipeline。

        功能：避免真实采集设备和 LiveKit AudioSource 发布，直接返回结果摘要。
        核心入参：args、kwargs 接收 run_from_config 传入的 pipeline 参数。
        预期输出：返回一帧已发布的结果。
        边界异常：不模拟失败。
        """
        _ = args, kwargs
        return SidecarPipelineResult(frames_published=1, stop_reason="frame_count_reached")

    monkeypatch.setattr(sidecar_main.rtc, "Room", FakeRoom)
    monkeypatch.setattr(sidecar_main.rtc, "AudioSource", FakeAudioSource)
    monkeypatch.setattr(sidecar_main.rtc, "LocalAudioTrack", FakeLocalAudioTrack)
    monkeypatch.setattr(sidecar_main, "run_sidecar_pipeline_once", fake_pipeline)

    result = asyncio.run(sidecar_main.run_from_config(make_sidecar_config()))

    assert result.frames_published == 1
    assert created_rooms[0].connect_calls == [("wss://livekit.example", "test-token")]
    assert created_rooms[0].disconnect_calls == 1
    assert len(created_rooms[0].local_participant.published_tracks) == 1


@pytest.mark.parametrize(
    ("capture_profile", "expected_lifecycle_events"),
    [
        (
            CaptureProfile.LOCAL_LOW_LATENCY,
            ["connect", "publish_track", "ready", "pipeline_enter", "pipeline_complete", "disconnect"],
        ),
        (
            CaptureProfile.RDP_BUFFERED,
            ["connect", "publish_track", "pipeline_enter", "ready", "pipeline_complete", "disconnect"],
        ),
    ],
)
def test_run_from_config_emits_ready_at_profile_specific_boundary(
    monkeypatch,
    capsys,
    capture_profile: CaptureProfile,
    expected_lifecycle_events: list[str],
) -> None:
    """验证本地路径在发布后立即 ready，RDP 路径在预缓冲完成后才 ready。

    功能：覆盖 desktop supervisor 等待 sidecar 真正可用的 stdout 协议。
    核心入参：使用自定义 identity 的 SidecarConfig、假 LiveKit Room 与假 pipeline。
    预期输出：stdout 第一行是 JSON ready 事件，包含本次显式 identity 与 clean-mic 轨道名。
    边界异常：若 ready 早于 publish_track 输出，事件顺序断言会失败，暴露主进程可能过早返回 renderer session。
    """
    # lifecycle_events 保存 fake LiveKit 与 pipeline 的关键顺序，确保 ready 不是单纯进程启动后的空喊。
    lifecycle_events: list[str] = []

    class FakeLocalParticipant:
        """模拟 LiveKit local_participant，并记录 clean-mic 发布完成时刻。"""

        async def publish_track(self, track: object) -> None:
            """记录本地轨发布完成。

            功能：模拟 LiveKit SDK 已接受 clean-mic track。
            核心入参：track 是 run_from_config 创建出的本地音频轨。
            预期输出：lifecycle_events 增加 publish_track。
            边界异常：本测试不模拟发布失败，只关注 ready 输出顺序。
            """
            _ = track
            lifecycle_events.append("publish_track")

    class FakeRoom:
        """模拟 LiveKit Room，用于隔离网络并记录 connect/disconnect 顺序。"""

        def __init__(self) -> None:
            """初始化本地参与者。"""
            # local_participant 是 run_from_config 发布 clean-mic 的唯一入口。
            self.local_participant = FakeLocalParticipant()

        async def connect(self, url: str, token: str) -> None:
            """记录连接完成。

            功能：模拟 sidecar 已进入目标 LiveKit 房间。
            核心入参：url 和 token 来自 SidecarConfig。
            预期输出：lifecycle_events 增加 connect。
            边界异常：本测试不模拟连接失败。
            """
            _ = url, token
            lifecycle_events.append("connect")

        async def disconnect(self) -> None:
            """记录房间断开。"""
            lifecycle_events.append("disconnect")

    class FakeAudioSource:
        """模拟 LiveKit AudioSource。"""

        def __init__(self, sample_rate: int, channels: int) -> None:
            """保存音频格式参数，保持与真实 SDK 构造签名兼容。"""
            self.sample_rate = sample_rate
            self.channels = channels

    class FakeLocalAudioTrack:
        """模拟 LocalAudioTrack 工厂。"""

        @staticmethod
        def create_audio_track(name: str, audio_source: FakeAudioSource) -> object:
            """创建可发布的 fake clean-mic 轨道。"""
            return {"name": name, "audio_source": audio_source}

    async def fake_pipeline(*args, **kwargs) -> SidecarPipelineResult:
        """模拟 track 发布后的管线运行。

        功能：避免真实采集设备，同时让 run_from_config 进入 ready 后的主循环阶段。
        核心入参：args、kwargs 接收 run_from_config 传入参数。
        预期输出：返回一帧发布摘要。
        边界异常：不模拟 pipeline 失败。
        """
        _ = args
        lifecycle_events.append("pipeline_enter")
        on_capture_ready = kwargs["on_capture_ready"]
        if on_capture_ready is not None:
            on_capture_ready()
        lifecycle_events.append("pipeline_complete")
        return SidecarPipelineResult(frames_published=1, stop_reason="frame_count_reached")

    monkeypatch.setattr(sidecar_main.rtc, "Room", FakeRoom)
    monkeypatch.setattr(sidecar_main.rtc, "AudioSource", FakeAudioSource)
    monkeypatch.setattr(sidecar_main.rtc, "LocalAudioTrack", FakeLocalAudioTrack)
    monkeypatch.setattr(sidecar_main, "run_sidecar_pipeline_once", fake_pipeline)
    monkeypatch.setattr(sidecar_main, "detect_capture_profile", lambda: capture_profile)
    original_emit_ready_event = sidecar_main.emit_ready_event

    def recording_emit_ready_event(*, identity: str, track_name: str) -> None:
        """记录 ready 输出时刻并保留真实 stdout 行。

        功能：让测试同时验证 ready 事件内容和 ready 事件相对 publish/pipeline 的顺序。
        核心入参：identity 与 track_name 原样传给真实 emit_ready_event。
        预期输出：lifecycle_events 增加 ready，stdout 仍写入 JSON ready 行。
        边界异常：若生产代码在 publish_track 前调用 ready，本测试的生命周期顺序会直接失败。
        """
        # 先记录 ready 时刻，再调用真实输出函数，保证生命周期断言和 stdout 内容断言观察同一次事件。
        lifecycle_events.append("ready")
        original_emit_ready_event(identity=identity, track_name=track_name)

    monkeypatch.setattr(sidecar_main, "emit_ready_event", recording_emit_ready_event)

    result = asyncio.run(sidecar_main.run_from_config(make_sidecar_config(identity="mic-clean-worker")))
    ready_payload = json.loads(capsys.readouterr().out.splitlines()[0])

    assert result.frames_published == 1
    assert ready_payload == {
        "event": "aec-sidecar-ready",
        "identity": "mic-clean-worker",
        "track": "clean-mic",
    }
    assert lifecycle_events == expected_lifecycle_events


def test_run_from_config_preserves_pipeline_exception_when_disconnect_also_fails(monkeypatch, capsys) -> None:
    """验证 pipeline 异常与 disconnect 异常同时发生时优先保留原始 pipeline 异常。"""

    class FakeLocalParticipant:
        """模拟可成功发布 track 的 local_participant。"""

        async def publish_track(self, track: object) -> None:
            """忽略发布对象并正常返回。"""
            _ = track

    class DisconnectFailingRoom:
        """模拟断开失败的 LiveKit Room。

        功能：connect 和 publish 阶段正常，disconnect 抛出异常，用于验证异常优先级。
        核心入参：connect 接收 url 和 token。
        预期输出：disconnect 抛出 RuntimeError。
        边界异常：断开阶段固定失败。
        """

        def __init__(self) -> None:
            """初始化 local_participant。"""
            self.local_participant = FakeLocalParticipant()

        async def connect(self, url: str, token: str) -> None:
            """模拟成功连接。"""
            _ = url, token

        async def disconnect(self) -> None:
            """模拟断开失败。"""
            raise RuntimeError("disconnect failed")

    class FakeAudioSource:
        """模拟 LiveKit AudioSource。"""

        def __init__(self, sample_rate: int, channels: int) -> None:
            """保存但不使用音频格式参数。"""
            self.sample_rate = sample_rate
            self.channels = channels

    class FakeLocalAudioTrack:
        """模拟 LocalAudioTrack 工厂。"""

        @staticmethod
        def create_audio_track(name: str, audio_source: FakeAudioSource) -> object:
            """返回 fake track 对象。"""
            return {"name": name, "audio_source": audio_source}

    async def failing_pipeline(*args, **kwargs) -> SidecarPipelineResult:
        """模拟 pipeline 发布阶段失败。

        功能：抛出原始 runtime 异常，测试要求 disconnect 失败不得覆盖它。
        核心入参：args、kwargs 接收 run_from_config 传入参数。
        预期输出：无正常返回。
        边界异常：固定抛出 RuntimeError。
        """
        _ = args, kwargs
        raise RuntimeError("pipeline failed")

    monkeypatch.setattr(sidecar_main.rtc, "Room", DisconnectFailingRoom)
    monkeypatch.setattr(sidecar_main.rtc, "AudioSource", FakeAudioSource)
    monkeypatch.setattr(sidecar_main.rtc, "LocalAudioTrack", FakeLocalAudioTrack)
    monkeypatch.setattr(sidecar_main, "run_sidecar_pipeline_once", failing_pipeline)
    monkeypatch.setattr(
        sidecar_main,
        "detect_capture_profile",
        lambda: CaptureProfile.RDP_BUFFERED,
    )

    with pytest.raises(RuntimeError, match="pipeline failed"):
        asyncio.run(sidecar_main.run_from_config(make_sidecar_config()))

    # pipeline 尚未调用采集就绪回调就失败时，stdout 不能出现会误导 supervisor 的 ready 事件。
    assert capsys.readouterr().out == ""


def test_run_from_config_preserves_publish_track_exception_when_disconnect_also_fails(monkeypatch) -> None:
    """验证发布本地轨失败且断开也失败时，run_from_config 保留原始发布异常并仍尝试断开。"""
    # created_rooms 保存 fake room，便于异常后确认 disconnect 已被 finally 尝试调用。
    created_rooms = []

    class PublishFailingLocalParticipant:
        """模拟 publish_track 失败的 LiveKit local_participant。

        功能：在发布 clean-mic 本地轨时抛出原始异常，覆盖 run_from_config 的发布失败清理路径。
        核心入参：publish_track 接收待发布 track。
        预期输出：无正常输出。
        边界异常：publish_track 固定抛出 RuntimeError。
        """

        async def publish_track(self, track: object) -> None:
            """模拟本地轨发布失败。

            功能：抛出 publish_track failed，测试要求该异常不被 disconnect failed 覆盖。
            核心入参：track 是 run_from_config 创建的 fake track。
            预期输出：无正常返回。
            边界异常：固定抛出 RuntimeError。
            """
            _ = track
            raise RuntimeError("publish_track failed")

    class PublishFailingRoom:
        """模拟发布失败且断开失败的 LiveKit Room。

        功能：connect 正常，local_participant.publish_track 抛错，disconnect 也抛错并记录调用次数。
        核心入参：connect 接收 url 和 token。
        预期输出：disconnect_calls 记录 finally 是否执行。
        边界异常：disconnect 固定抛出 RuntimeError。
        """

        def __init__(self) -> None:
            """初始化 fake room 生命周期记录。"""
            # local_participant 是发布异常的触发点。
            self.local_participant = PublishFailingLocalParticipant()
            # disconnect_calls 用于断言清理路径即使遇到发布异常也会执行。
            self.disconnect_calls = 0
            created_rooms.append(self)

        async def connect(self, url: str, token: str) -> None:
            """模拟成功连接。

            功能：避免真实 LiveKit 网络连接，只保留 run_from_config 的生命周期顺序。
            核心入参：url 是 LiveKit 地址，token 是入房令牌。
            预期输出：正常返回。
            边界异常：不模拟连接失败。
            """
            _ = url, token

        async def disconnect(self) -> None:
            """模拟断开失败并记录尝试次数。

            功能：验证清理异常不得覆盖 publish_track 原始异常。
            核心入参：无。
            预期输出：disconnect_calls 增加一次。
            边界异常：固定抛出 RuntimeError。
            """
            self.disconnect_calls += 1
            raise RuntimeError("disconnect failed")

    class FakeAudioSource:
        """模拟 LiveKit AudioSource。"""

        def __init__(self, sample_rate: int, channels: int) -> None:
            """保存音频格式参数。"""
            self.sample_rate = sample_rate
            self.channels = channels

    class FakeLocalAudioTrack:
        """模拟 LocalAudioTrack 工厂。"""

        @staticmethod
        def create_audio_track(name: str, audio_source: FakeAudioSource) -> object:
            """返回 fake track 对象。"""
            return {"name": name, "audio_source": audio_source}

    monkeypatch.setattr(sidecar_main.rtc, "Room", PublishFailingRoom)
    monkeypatch.setattr(sidecar_main.rtc, "AudioSource", FakeAudioSource)
    monkeypatch.setattr(sidecar_main.rtc, "LocalAudioTrack", FakeLocalAudioTrack)

    with pytest.raises(RuntimeError, match="publish_track failed"):
        asyncio.run(sidecar_main.run_from_config(make_sidecar_config()))

    assert created_rooms[0].disconnect_calls == 1


def test_run_from_config_surfaces_disconnect_failure_after_success(monkeypatch) -> None:
    """验证 pipeline 成功后若 disconnect 失败，则断开异常会正常向调用方暴露。"""

    class FakeLocalParticipant:
        """模拟可成功发布 track 的 local_participant。"""

        async def publish_track(self, track: object) -> None:
            """模拟本地轨发布成功。"""
            _ = track

    class DisconnectFailingRoom:
        """模拟成功运行后断开失败的 LiveKit Room。

        功能：connect 正常返回，disconnect 固定抛错，用于验证没有原始异常时清理异常不应被吞掉。
        核心入参：connect 接收 url 和 token。
        预期输出：disconnect 抛出 RuntimeError。
        边界异常：断开阶段固定失败。
        """

        def __init__(self) -> None:
            """初始化 local_participant。"""
            self.local_participant = FakeLocalParticipant()

        async def connect(self, url: str, token: str) -> None:
            """模拟连接成功。"""
            _ = url, token

        async def disconnect(self) -> None:
            """模拟断开失败。"""
            raise RuntimeError("disconnect failed")

    class FakeAudioSource:
        """模拟 LiveKit AudioSource。"""

        def __init__(self, sample_rate: int, channels: int) -> None:
            """保存但不使用音频格式参数。"""
            self.sample_rate = sample_rate
            self.channels = channels

    class FakeLocalAudioTrack:
        """模拟 LocalAudioTrack 工厂。"""

        @staticmethod
        def create_audio_track(name: str, audio_source: FakeAudioSource) -> object:
            """返回 fake track 对象。"""
            return {"name": name, "audio_source": audio_source}

    async def successful_pipeline(*args, **kwargs) -> SidecarPipelineResult:
        """模拟正常完成的 pipeline。

        功能：直接返回运行摘要，让测试只关注 disconnect 失败是否浮出。
        核心入参：args、kwargs 接收 run_from_config 传入参数。
        预期输出：返回成功结果。
        边界异常：不主动抛错。
        """
        _ = args, kwargs
        return SidecarPipelineResult(frames_published=1, stop_reason="frame_count_reached")

    monkeypatch.setattr(sidecar_main.rtc, "Room", DisconnectFailingRoom)
    monkeypatch.setattr(sidecar_main.rtc, "AudioSource", FakeAudioSource)
    monkeypatch.setattr(sidecar_main.rtc, "LocalAudioTrack", FakeLocalAudioTrack)
    monkeypatch.setattr(sidecar_main, "run_sidecar_pipeline_once", successful_pipeline)

    with pytest.raises(RuntimeError, match="disconnect failed"):
        asyncio.run(sidecar_main.run_from_config(make_sidecar_config()))
