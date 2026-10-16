from __future__ import annotations

from pathlib import Path
from types import TracebackType
import wave


class Pcm16WavWriter:
    """将原始 PCM16 字节流写入 WAV 文件的上下文管理器。

    功能：负责创建父目录、打开 WAV 文件、写入 PCM16 帧，并在上下文退出时关闭文件。
    核心入参：path 是输出 WAV 路径，sample_rate 是采样率，channels 是声道数。
    预期输出：上下文结束后生成包含原始 PCM16 帧和标准 WAV 元数据的文件。
    边界异常：奇数字节会抛出 ValueError，未进入上下文写入会抛出 RuntimeError。
    """

    def __init__(self, path: Path, *, sample_rate: int, channels: int) -> None:
        """初始化 WAV 写入器配置，但暂不打开文件。

        功能：保存输出路径和 WAV 元数据，等待进入上下文时再创建文件。
        核心入参：path 是目标文件路径，sample_rate 是每秒采样数，channels 是声道数量。
        预期输出：生成可进入 with 上下文的写入器实例。
        边界异常：本方法不触发磁盘写入，路径不可写等异常会在进入上下文时暴露。
        """
        # 目标 WAV 路径仅在当前实例内使用，允许调用方传入 Path 的子类或临时路径。
        self._path = path
        # 采样率由调用方指定，用于写入 WAV 头中的 framerate 字段。
        self._sample_rate = sample_rate
        # 声道数由调用方指定，用于写入 WAV 头中的 nchannels 字段。
        self._channels = channels
        # 底层 wave 写入句柄只在上下文打开期间存在，None 表示当前不可写入。
        self._wav_file: wave.Wave_write | None = None

    def __enter__(self) -> "Pcm16WavWriter":
        """打开 WAV 文件并写入固定的 PCM16 元数据。

        功能：创建父目录，打开二进制 WAV 写入流，并配置声道数、样本宽度和采样率。
        核心入参：无显式入参，使用初始化阶段保存的路径与音频参数。
        预期输出：返回当前写入器，供调用方连续写入 PCM16 字节。
        边界异常：目录创建或文件打开失败时透传系统异常，便于调用方看到真实磁盘问题。
        """
        # 进入上下文时创建父目录，保证调用方可以直接传入尚不存在的嵌套输出路径。
        self._path.parent.mkdir(parents=True, exist_ok=True)

        # 打开标准 WAV 写入器后立即设置元数据，确保随后写入的帧按 PCM16 解释。
        self._wav_file = wave.open(str(self._path), "wb")
        self._wav_file.setnchannels(self._channels)
        self._wav_file.setsampwidth(2)
        self._wav_file.setframerate(self._sample_rate)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """关闭底层 WAV 文件，并保留上下文内发生的异常。

        功能：无论 with 块是否抛错，都尽力关闭已打开的 wave 文件。
        核心入参：exc_type、exc_value、traceback 是上下文协议传入的异常信息。
        预期输出：文件句柄被关闭，方法返回 None 以便原始异常继续向外传播。
        边界异常：关闭文件时若底层 wave 模块抛错，将按 Python 上下文协议向外传播。
        """
        # 如果文件从未成功打开，则没有句柄需要关闭，直接返回保留原始控制流。
        if self._wav_file is None:
            return None

        # 退出上下文时关闭 WAV 文件，确保头部帧数和数据长度被 wave 模块正确回写。
        self._wav_file.close()
        self._wav_file = None
        return None

    def write(self, pcm16: bytes) -> None:
        """追加写入一段 PCM16 字节。

        功能：校验输入字节数是否能组成完整 16 位样本，并将帧追加到 WAV 文件。
        核心入参：pcm16 是原始 PCM16 小端字节序数据。
        预期输出：数据被追加到当前打开的 WAV 文件中，不返回业务值。
        边界异常：未进入上下文写入会抛出 RuntimeError，奇数字节会抛出 ValueError。
        """
        # 未打开文件时拒绝写入，避免调用方在生命周期错误下静默丢失音频数据。
        if self._wav_file is None:
            raise RuntimeError("Pcm16WavWriter.write() must be called inside an open context.")

        # PCM16 每个样本占 2 字节，奇数字节意味着最后一个样本不完整，必须提前拒绝。
        if len(pcm16) % 2 != 0:
            raise ValueError("PCM16 data must contain an even number of bytes.")

        # wave.writeframes 会维护帧计数和数据块长度，因此这里直接传入原始 PCM16 字节。
        self._wav_file.writeframes(pcm16)
