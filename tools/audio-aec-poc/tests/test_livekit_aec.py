from __future__ import annotations

import pytest

from audio_aec_poc import livekit_aec
from audio_aec_poc.livekit_aec import LiveKitAecProcessor


class FakeAudioFrame:
    """记录 AudioFrame 构造参数的测试替身。

    功能：模拟 LiveKit AudioFrame 的最小数据接口，保留构造时传入的音频参数。
    核心入参：data 是 PCM16 字节，sample_rate 是采样率，num_channels 是声道数，samples_per_channel 是单声道样本数。
    预期输出：生成带 data 属性的帧对象，供 APM 替身读取或修改。
    边界异常：本替身不模拟真实 LiveKit 的底层校验，参数合法性由被测处理器负责。
    """

    def __init__(
        self,
        data: bytes | bytearray | memoryview,
        sample_rate: int,
        num_channels: int,
        samples_per_channel: int,
    ) -> None:
        """保存音频帧构造参数，便于测试断言被测代码传参正确。

        功能：将输入数据规范为 bytearray，并记录采样率、声道数、单声道样本数。
        核心入参：data 是原始帧数据，其余参数是 LiveKit AudioFrame 需要的音频元数据。
        预期输出：当前实例可作为 process_stream 和 process_reverse_stream 的输入。
        边界异常：本方法不抛出业务异常，测试只关注处理器是否传入正确参数。
        """
        # 将数据保存为可变缓冲区，模拟真实处理链路可能原地改写麦克风帧的行为。
        self.data = bytearray(data)
        # 以下元数据用于断言处理器按 10ms PCM16 约定构造 LiveKit 帧。
        self.sample_rate = sample_rate
        self.num_channels = num_channels
        self.samples_per_channel = samples_per_channel


class FakeAudioProcessingModule:
    """记录 APM 配置和调用顺序的测试替身。

    功能：模拟 LiveKit AudioProcessingModule 的公开方法，并将调用写入共享日志。
    核心入参：配置开关必须与真实 APM 构造参数同名，便于 monkeypatch 后直接接收被测代码调用。
    预期输出：实例暴露 process_reverse_stream 与 process_stream，测试可检查调用顺序和配置。
    边界异常：本替身不执行真实声学处理，避免测试依赖 LiveKit APM 的运行时行为。
    """

    instances: list["FakeAudioProcessingModule"] = []

    def __init__(
        self,
        *,
        echo_cancellation: bool,
        noise_suppression: bool,
        high_pass_filter: bool,
        auto_gain_control: bool,
    ) -> None:
        """保存 APM 配置，供测试验证处理器启用了正确的音频处理能力。

        功能：记录构造参数并注册当前实例。
        核心入参：四个布尔开关对应 LiveKit APM 的回声消除、降噪、高通滤波和自动增益配置。
        预期输出：当前实例进入 instances 列表，随后可记录处理调用。
        边界异常：本方法不抛出业务异常，配置错误会通过测试断言暴露。
        """
        # 配置字典用于一次性比较四个 LiveKit 开关，减少测试中的重复断言噪声。
        self.config = {
            "echo_cancellation": echo_cancellation,
            "noise_suppression": noise_suppression,
            "high_pass_filter": high_pass_filter,
            "auto_gain_control": auto_gain_control,
        }
        # 调用日志按发生顺序保存，核心目标是验证 reverse stream 必须先进入 APM。
        self.calls: list[tuple[str, FakeAudioFrame]] = []
        FakeAudioProcessingModule.instances.append(self)

    def process_reverse_stream(self, data: FakeAudioFrame) -> None:
        """记录参考流处理调用。

        功能：模拟 APM 先消费扬声器参考帧，让后续麦克风帧拥有可用于消回声的上下文。
        核心入参：data 是参考 PCM16 帧。
        预期输出：调用被追加到 calls 日志中。
        边界异常：本替身不校验帧内容，错误帧长度由处理器在调用前拒绝。
        """
        # 参考流调用必须先发生，测试会通过日志顺序确认这个业务约束。
        self.calls.append(("reverse", data))

    def process_stream(self, data: FakeAudioFrame) -> None:
        """记录麦克风流处理调用，并模拟输出帧可被回读。

        功能：模拟 APM 对麦克风帧执行处理，测试中写入可识别字节以验证返回值来自帧数据。
        核心入参：data 是麦克风 PCM16 帧。
        预期输出：调用被追加到 calls 日志中，data.data 被替换为固定测试输出。
        边界异常：本替身不模拟真实处理失败，失败路径不属于当前包装器职责。
        """
        # 写入固定输出可以证明 process_frame 返回的是处理后的 microphone_frame.data。
        data.data[:] = b"\x11" * len(data.data)
        self.calls.append(("stream", data))


@pytest.fixture
def fake_livekit(monkeypatch):
    """替换 LiveKit 类为测试替身，并在每个测试前清理共享实例日志。

    功能：隔离真实 LiveKit APM，让单元测试只验证包装器契约。
    核心入参：monkeypatch 是 pytest 提供的动态替换工具。
    预期输出：livekit_aec.rtc 中的 AudioFrame 与 AudioProcessingModule 被替换为假对象。
    边界异常：若被测模块更改导入路径，monkeypatch 会让测试及时失败。
    """
    # 清空类级实例列表，避免不同测试之间共享历史调用记录造成误判。
    FakeAudioProcessingModule.instances.clear()
    # 直接替换被测模块持有的 rtc 成员，确保测试不依赖真实 LiveKit APM 行为。
    monkeypatch.setattr(livekit_aec.rtc, "AudioFrame", FakeAudioFrame)
    monkeypatch.setattr(livekit_aec.rtc, "AudioProcessingModule", FakeAudioProcessingModule)


def test_livekit_aec_processor_configures_apm_and_processes_reference_before_microphone(fake_livekit):
    """验证处理器配置 APM，并按先参考流后麦克风流的顺序处理 10ms 帧。"""
    # 16kHz 单声道下 10ms 正好是 160 个样本，每个 PCM16 样本 2 字节。
    processor = LiveKitAecProcessor(sample_rate=16000, channels=1)
    reference_pcm16 = b"\x01\x00" * 160
    microphone_pcm16 = b"\x02\x00" * 160

    # 执行一帧处理，业务要求参考帧先进入 reverse stream，再处理麦克风帧。
    processed = processor.process_frame(reference_pcm16, microphone_pcm16)

    # APM 必须按 PoC 配置开启回声消除和高通滤波，同时关闭降噪与自动增益。
    apm = FakeAudioProcessingModule.instances[0]
    assert apm.config == {
        "echo_cancellation": True,
        "noise_suppression": False,
        "high_pass_filter": True,
        "auto_gain_control": False,
    }
    # 调用顺序是 AEC 正确性的核心：参考流先建立远端播放上下文，麦克风流随后被消回声。
    assert [call_name for call_name, _ in apm.calls] == ["reverse", "stream"]
    # 两个帧都应使用同一组 10ms 音频元数据，避免 LiveKit 内部按错误帧长解释数据。
    assert apm.calls[0][1].sample_rate == 16000
    assert apm.calls[0][1].num_channels == 1
    assert apm.calls[0][1].samples_per_channel == 160
    assert apm.calls[1][1].sample_rate == 16000
    assert apm.calls[1][1].num_channels == 1
    assert apm.calls[1][1].samples_per_channel == 160
    # 返回值来自麦克风帧被 APM 处理后的 data，而不是原始输入字节。
    assert processed == b"\x11" * processor.frame_bytes


def test_livekit_aec_processor_frame_bytes_uses_10ms_pcm16_size(fake_livekit):
    """验证 frame_bytes 按 10ms、声道数和 PCM16 样本宽度计算。"""
    # 48kHz 双声道下，10ms 每声道 480 样本，总字节数是 480 * 2 声道 * 2 字节。
    processor = LiveKitAecProcessor(sample_rate=48000, channels=2)

    # frame_bytes 是调用方切帧的公开依据，必须精确反映 LiveKit AudioFrame 的 10ms 大小。
    assert processor.frame_bytes == 1920


def test_livekit_aec_processor_rejects_non_10ms_reference_frame(fake_livekit):
    """验证参考帧长度不是 10ms 时抛出包含 10ms 的 ValueError。"""
    # 构造少 1 字节的参考帧，模拟 loopback 捕获端切帧不完整。
    processor = LiveKitAecProcessor(sample_rate=16000, channels=1)
    invalid_reference_pcm16 = b"\x00" * (processor.frame_bytes - 1)
    microphone_pcm16 = b"\x00" * processor.frame_bytes

    # 错误消息必须包含 10ms，方便调用方直接定位到固定帧长约束。
    with pytest.raises(ValueError, match="10ms"):
        processor.process_frame(invalid_reference_pcm16, microphone_pcm16)


def test_livekit_aec_processor_rejects_non_10ms_microphone_frame(fake_livekit):
    """验证麦克风帧长度不是 10ms 时抛出包含 10ms 的 ValueError。"""
    # 构造多 1 字节的麦克风帧，模拟录音端批量读取后没有按 10ms 对齐。
    processor = LiveKitAecProcessor(sample_rate=16000, channels=1)
    reference_pcm16 = b"\x00" * processor.frame_bytes
    invalid_microphone_pcm16 = b"\x00" * (processor.frame_bytes + 1)

    # 包装器应在调用 APM 之前拒绝错误帧，避免真实 LiveKit 接收不符合约定的数据。
    with pytest.raises(ValueError, match="10ms"):
        processor.process_frame(reference_pcm16, invalid_microphone_pcm16)


def test_livekit_aec_processor_rejects_sample_rate_that_cannot_form_10ms_frames(fake_livekit):
    """验证采样率无法整除 100 时直接拒绝，避免产生非 10ms 的隐式帧长。"""
    # 44101Hz 无法用整数样本表达 10ms，继续处理会造成帧长和时间长度不一致。
    with pytest.raises(ValueError, match="10ms"):
        LiveKitAecProcessor(sample_rate=44101, channels=1)
