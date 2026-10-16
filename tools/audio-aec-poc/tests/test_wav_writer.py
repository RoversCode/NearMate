from __future__ import annotations

import wave

import pytest

from audio_aec_poc.wav_writer import Pcm16WavWriter


def test_pcm16_wav_writer_preserves_pcm_bytes(tmp_path):
    """验证写入器会原样保存 PCM16 字节，并写入调用方要求的 WAV 元数据。"""
    # 准备一段覆盖正负样本与静音样本的 PCM16 小端字节，确保测试验证的是真实二进制内容。
    pcm16 = b"\x00\x00\x01\x00\xff\x7f\x00\x80"
    # 使用临时目录中的嵌套路径，顺带验证写入器会自动创建父目录。
    wav_path = tmp_path / "nested" / "sample.wav"

    # 通过上下文管理器写入音频，业务上要求调用方无需手动处理 WAV 头和文件关闭。
    with Pcm16WavWriter(wav_path, sample_rate=16000, channels=1) as writer:
        writer.write(pcm16)

    # 重新打开 WAV 文件读取底层帧数据，确认写入器没有改写、补齐或丢弃原始 PCM 字节。
    with wave.open(str(wav_path), "rb") as wav_file:
        assert wav_file.readframes(wav_file.getnframes()) == pcm16


def test_pcm16_wav_writer_sets_expected_metadata(tmp_path):
    """验证写入器固定使用 PCM16 样本宽度，并保留采样率和声道数配置。"""
    # 关键路径变量用于隔离测试产物，避免测试之间共享同一个 WAV 文件。
    wav_path = tmp_path / "metadata.wav"

    # 写入一帧静音样本，确保 WAV 文件包含可被 wave 模块解析的帧数据。
    with Pcm16WavWriter(wav_path, sample_rate=16000, channels=1) as writer:
        writer.write(b"\x00\x00")

    # 读取标准 WAV 元数据，确认写入器按照 PCM16 单声道 16kHz 的 PoC 约定落盘。
    with wave.open(str(wav_path), "rb") as wav_file:
        assert wav_file.getframerate() == 16000
        assert wav_file.getnchannels() == 1
        assert wav_file.getsampwidth() == 2


def test_pcm16_wav_writer_rejects_odd_byte_count(tmp_path):
    """验证奇数字节会被拒绝，避免半个 PCM16 样本写入文件造成后续解析异常。"""
    # 奇数字节无法组成完整的 16 位样本，这是调用方输入错误而不是 WAV 写入错误。
    odd_pcm16 = b"\x00"
    wav_path = tmp_path / "odd.wav"

    # 错误消息必须明确提到 PCM16，帮助调用方快速定位到样本宽度约束。
    with Pcm16WavWriter(wav_path, sample_rate=16000, channels=1) as writer:
        with pytest.raises(ValueError, match="PCM16"):
            writer.write(odd_pcm16)


def test_pcm16_wav_writer_rejects_write_outside_context(tmp_path):
    """验证未进入上下文时禁止写入，避免调用方误以为数据已经落盘。"""
    # 构造但不进入上下文，模拟调用方忘记使用 with 的生命周期错误。
    writer = Pcm16WavWriter(tmp_path / "closed.wav", sample_rate=16000, channels=1)

    # 写入器没有打开底层 wave 文件时必须明确报错，而不是静默丢弃数据。
    with pytest.raises(RuntimeError):
        writer.write(b"\x00\x00")
