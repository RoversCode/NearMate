from __future__ import annotations

import asyncio

import pytest

from audio_aec_sidecar import livekit_publisher
from audio_aec_sidecar.livekit_publisher import CleanMicPublisher


class FakeAudioFrame:
    """记录 CleanMicPublisher 构造 LiveKit AudioFrame 时传入的参数。

    功能：模拟 livekit.rtc.AudioFrame 的最小构造接口，保留 clean PCM16 与音频元数据。
    核心入参：data 是待发布 PCM16，sample_rate、num_channels、samples_per_channel 描述帧格式。
    预期输出：生成可被 FakeAudioSource.capture_frame 接收的帧对象。
    边界异常：本替身不执行真实 LiveKit 参数校验，单元测试只关注发布器自身契约。
    """

    def __init__(
        self,
        data: bytes | bytearray | memoryview,
        sample_rate: int,
        num_channels: int,
        samples_per_channel: int,
    ) -> None:
        """保存构造参数，便于测试断言发布器封装逻辑。

        功能：将音频数据转成不可变 bytes，并记录所有 LiveKit 帧元数据。
        核心入参：data 是 clean PCM16 缓冲区，其余字段来自 CleanMicPublisher 配置。
        预期输出：当前实例持有 capture_frame 所需的数据属性。
        边界异常：不主动抛异常，异常输入由发布器在构造帧前处理。
        """
        # 数据转成 bytes，确保测试断言不受调用方传入缓冲区后续变化影响。
        self.data = bytes(data)
        # 以下字段直接对应 LiveKit AudioFrame 构造参数，是发布链路最关键的格式契约。
        self.sample_rate = sample_rate
        self.num_channels = num_channels
        self.samples_per_channel = samples_per_channel


class FakeAudioSource:
    """记录 CleanMicPublisher 发布出的音频帧。

    功能：模拟 LiveKit AudioSource 的异步 capture_frame 方法。
    核心入参：capture_frame 接收发布器创建的 FakeAudioFrame。
    预期输出：captured_frames 列表按调用顺序保存所有帧。
    边界异常：本替身不模拟网络或 LiveKit 运行时失败，当前任务只验证封装与调用。
    """

    def __init__(self) -> None:
        """初始化空的帧捕获日志。

        功能：为每个测试提供独立的捕获列表。
        核心入参：无。
        预期输出：captured_frames 可用于断言发布次数和帧内容。
        边界异常：无业务异常。
        """
        # 捕获日志是异步发布后的唯一观察点，避免测试依赖真实 LiveKit runtime。
        self.captured_frames: list[FakeAudioFrame] = []

    async def capture_frame(self, frame: FakeAudioFrame) -> None:
        """模拟异步发布单个音频帧。

        功能：把传入帧追加到捕获日志，代表 LiveKit AudioSource 已接收该帧。
        核心入参：frame 是 CleanMicPublisher 构造的音频帧对象。
        预期输出：captured_frames 增加一条记录。
        边界异常：本替身不抛异常，失败传播不属于 Task 3 的基础单元范围。
        """
        # 保留原始帧对象，方便测试检查构造参数而不需要真实 LiveKit 类型。
        self.captured_frames.append(frame)


@pytest.fixture
def fake_audio_frame(monkeypatch):
    """将发布器模块中的 LiveKit AudioFrame 替换为测试替身。

    功能：隔离真实 LiveKit runtime，让测试只验证 CleanMicPublisher 的封装契约。
    核心入参：monkeypatch 是 pytest 的动态替换工具。
    预期输出：livekit_publisher.rtc.AudioFrame 指向 FakeAudioFrame。
    边界异常：若模块导入路径改变，monkeypatch 会让测试及时失败。
    """
    # 直接替换被测模块持有的 rtc 成员，避免创建真实 LiveKit 帧时需要底层运行时支持。
    monkeypatch.setattr(livekit_publisher.rtc, "AudioFrame", FakeAudioFrame)


def test_clean_mic_publisher_skips_empty_pcm16(fake_audio_frame) -> None:
    """验证空 PCM16 块会被跳过，不发布空音频帧。"""
    # 空块常见于上游暂时没有 clean 音频输出，跳过可以避免给 LiveKit 注入无意义帧。
    audio_source = FakeAudioSource()
    publisher = CleanMicPublisher(audio_source)

    asyncio.run(publisher.publish_pcm16(b""))

    assert audio_source.captured_frames == []


def test_clean_mic_publisher_rejects_odd_length_pcm16(fake_audio_frame) -> None:
    """验证奇数字节 PCM16 被拒绝。"""
    # PCM16 每个样本固定 2 字节，奇数字节表示上游切片损坏或格式不匹配。
    audio_source = FakeAudioSource()
    publisher = CleanMicPublisher(audio_source)

    with pytest.raises(ValueError, match="PCM16"):
        asyncio.run(publisher.publish_pcm16(b"\x01"))

    # 错误输入必须在发布前被拦截，避免 LiveKit 收到不可解释的半个样本。
    assert audio_source.captured_frames == []


def test_clean_mic_publisher_rejects_zero_channels(fake_audio_frame) -> None:
    """验证发布器拒绝零声道配置。"""
    # 零声道会让 samples_per_channel 计算出现除零错误，必须在构造阶段给出业务异常。
    audio_source = FakeAudioSource()

    with pytest.raises(ValueError, match="channels"):
        CleanMicPublisher(audio_source, channels=0)


def test_clean_mic_publisher_rejects_stereo_channels(fake_audio_frame) -> None:
    """验证发布器拒绝当前侧车不支持的双声道配置。"""
    # Task 3 的 clean 麦克风发布链路只支持单声道，双声道不能静默按错误格式发布。
    audio_source = FakeAudioSource()

    with pytest.raises(ValueError, match="channels"):
        CleanMicPublisher(audio_source, channels=2)


def test_clean_mic_publisher_rejects_non_positive_sample_rate(fake_audio_frame) -> None:
    """验证发布器拒绝非正采样率。"""
    # 非正采样率无法描述真实音频时间轴，应在构造发布器时直接拒绝。
    audio_source = FakeAudioSource()

    with pytest.raises(ValueError, match="sample_rate"):
        CleanMicPublisher(audio_source, sample_rate=0)


def test_clean_mic_publisher_wraps_pcm16_in_audio_frame_and_captures_it(fake_audio_frame) -> None:
    """验证 clean PCM16 会被封装成 LiveKit AudioFrame 并异步发布。"""
    # 160 个单声道 PCM16 样本在 16kHz 下对应 10ms clean 麦克风音频。
    pcm16 = b"\x01\x00" * 160
    audio_source = FakeAudioSource()
    publisher = CleanMicPublisher(audio_source, sample_rate=16000, channels=1)

    asyncio.run(publisher.publish_pcm16(pcm16))

    # 发布器应只发布一帧，并完整保留 clean PCM16 字节。
    assert len(audio_source.captured_frames) == 1
    frame = audio_source.captured_frames[0]
    assert frame.data == pcm16
    assert frame.sample_rate == 16000
    assert frame.num_channels == 1
    assert frame.samples_per_channel == 160
