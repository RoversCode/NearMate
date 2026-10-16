from __future__ import annotations


def read_pcm16_from_audio_event(audio_event: object, *, expected_sample_rate: int = 16000) -> bytes:
    """从 LiveKit AudioStream 事件中读取 16k 单声道 PCM16 字节。"""

    # frame 是 LiveKit AudioStream event 的真实载荷；这里保持鸭子类型，方便单测用轻量替身覆盖 SDK 边界。
    frame = getattr(audio_event, "frame")
    # sample_rate 和 num_channels 是语音运行时格式契约，提前校验能把问题隔离在媒体接入层。
    sample_rate = int(getattr(frame, "sample_rate"))
    num_channels = int(getattr(frame, "num_channels"))
    if sample_rate != expected_sample_rate or num_channels != 1:
        raise ValueError("expected 16000 Hz mono audio")

    # LiveKit frame.data 可能是 memoryview，也可能是带 tobytes 的 SDK 缓冲对象；优先走显式复制接口。
    data = getattr(frame, "data")
    tobytes = getattr(data, "tobytes", None)
    if callable(tobytes):
        return tobytes()

    # bytes() 兜底支持 bytes、bytearray 和实现 buffer protocol 的对象，保证进入音频队列的值不可变。
    return bytes(data)

