from __future__ import annotations

import json
from pathlib import Path
from types import TracebackType
from typing import Any, Callable

from audio_aec_poc.wav_writer import Pcm16WavWriter


class SidecarDebugRecorder:
    """侧车调试音频录制器。

    功能：在指定运行目录中同时保存 mic_raw.wav、speaker_ref.wav 和 mic_clean.wav，便于离线检查侧车实时发布链路。
    核心入参：run_dir 是调试文件目录，sample_rate 是 AEC 内部输入采样率，clean_sample_rate 是 clean 输出采样率，channels 是声道数。
    预期输出：进入上下文后创建运行目录并打开三路 WAV，write_frames 会把每帧 PCM16 写入对应文件。
    边界异常：未进入上下文时写入会抛出 RuntimeError；PCM16 字节数非法或文件系统错误由底层 Pcm16WavWriter 透传。
    """

    def __init__(self, run_dir: Path, *, sample_rate: int, clean_sample_rate: int, channels: int) -> None:
        """初始化调试录制器配置，但暂不打开任何文件。

        功能：保存调试输出目录和三路 WAV 的音频元数据，等待进入上下文时统一创建写入器。
        核心入参：run_dir 是本次调试运行目录，sample_rate 用于 mic_raw 与 speaker_ref，clean_sample_rate 用于 mic_clean。
        预期输出：生成可作为上下文管理器使用的 SidecarDebugRecorder 实例。
        边界异常：初始化阶段不触碰磁盘，路径不可写等问题会在 __enter__ 阶段暴露。
        """
        # 调试运行目录只属于当前侧车 pipeline 运行，不负责再创建时间戳子目录。
        self._run_dir = run_dir
        # 原始麦克风与扬声器参考使用 AEC 内部采样率，方便与 AEC 输入逐帧对齐。
        self._sample_rate = sample_rate
        # clean 麦克风使用发布采样率，方便直接听到侧车真正送往 LiveKit 的内容。
        self._clean_sample_rate = clean_sample_rate
        # 当前侧车配置层限定单声道，这里保留参数以便三路 WAV 元数据一致。
        self._channels = channels
        # 三个写入器只在上下文打开期间存在，None 表示当前不能写入。
        self._mic_raw_writer: Pcm16WavWriter | None = None
        self._speaker_ref_writer: Pcm16WavWriter | None = None
        self._mic_clean_writer: Pcm16WavWriter | None = None
        self._diagnostics_provider: Callable[[BaseException | None], dict[str, Any]] | None = None

    def __enter__(self) -> "SidecarDebugRecorder":
        """创建调试目录并打开三路 WAV 文件。

        功能：确保 run_dir 存在，分别打开 mic_raw.wav、speaker_ref.wav 和 mic_clean.wav 的 PCM16 写入器。
        核心入参：无显式入参，使用初始化阶段保存的目录与音频格式。
        预期输出：返回当前录制器，调用方可连续写入每帧调试音频。
        边界异常：目录创建或 WAV 打开失败时透传底层异常；若部分文件已打开，会在异常路径上尽力关闭。
        """
        # 进入上下文时创建目录，满足任务要求并避免构造对象时产生空目录副作用。
        self._run_dir.mkdir(parents=True, exist_ok=True)
        try:
            # 原始麦克风保留 AEC 前近端输入，用于判断回声是否确实进入麦克风。
            self._mic_raw_writer = Pcm16WavWriter(
                self._run_dir / "mic_raw.wav",
                sample_rate=self._sample_rate,
                channels=self._channels,
            ).__enter__()
            # 扬声器参考保留远端播放内容，用于诊断 loopback reference 是否有效。
            self._speaker_ref_writer = Pcm16WavWriter(
                self._run_dir / "speaker_ref.wav",
                sample_rate=self._sample_rate,
                channels=self._channels,
            ).__enter__()
            # clean 麦克风保留发布前的最终音频，采样率必须与 LiveKit 发布链路一致。
            self._mic_clean_writer = Pcm16WavWriter(
                self._run_dir / "mic_clean.wav",
                sample_rate=self._clean_sample_rate,
                channels=self._channels,
            ).__enter__()
        except BaseException:
            # 任一路打开失败都说明调试录制不可用，必须关闭已打开文件后继续抛出原始异常。
            self.__exit__(None, None, None)
            raise

        return self

    @property
    def run_dir(self) -> Path:
        """返回当前 sidecar debug run 的独立目录。"""

        return self._run_dir

    def write_diagnostics(self, diagnostics: dict[str, Any]) -> None:
        """把当前 run 的生命周期和采集状态写入 JSON 诊断文件。

        Args:
            diagnostics: 已经整理为 JSON 可序列化值的本次运行诊断。

        Returns:
            无返回值；文件写入完成后代表本次 run 的诊断已经落盘。

        Raises:
            TypeError: 诊断载荷包含不能序列化的对象。
            OSError: 诊断文件无法写入。
        """

        diagnostics_path = self._run_dir / "diagnostics.json"
        diagnostics_path.write_text(
            json.dumps(diagnostics, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def set_diagnostics_provider(
        self,
        provider: Callable[[BaseException | None], dict[str, Any]],
    ) -> None:
        """登记一个在 run 关闭时生成诊断载荷的回调。

        Args:
            provider: 接收本次上下文异常并返回 JSON 诊断字典的回调。

        Returns:
            无返回值。
        """

        self._diagnostics_provider = provider

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """关闭三路 WAV 文件并保留原始异常传播。

        功能：上下文退出时释放已经打开的 WAV 写入器，并把内部引用恢复为空。
        核心入参：exc_type、exc_value、traceback 是上下文协议传入的异常信息。
        预期输出：文件句柄被关闭，方法返回 None 以保留 with 块中的异常语义。
        边界异常：底层关闭失败会按上下文协议向外传播。
        """
        # 关闭顺序与打开顺序相反；每一路都独立尝试关闭，避免某个 WAV 头回写失败阻断其它文件收口。
        close_error: BaseException | None = None
        for writer in (self._mic_clean_writer, self._speaker_ref_writer, self._mic_raw_writer):
            if writer is None:
                continue
            try:
                writer.__exit__(exc_type, exc_value, traceback)
            except BaseException as exc:
                # 保留第一个关闭错误作为确定性抛出结果，同时继续关闭剩余 writer。
                if close_error is None:
                    close_error = exc

        # 无论关闭是否异常，都清空引用，避免后续 write_frames 误以为仍可写入。
        self._mic_raw_writer = None
        self._speaker_ref_writer = None
        self._mic_clean_writer = None

        diagnostics_error: BaseException | None = None
        if self._diagnostics_provider is not None:
            try:
                self.write_diagnostics(self._diagnostics_provider(exc_value))
            except BaseException as exception:
                diagnostics_error = exception
        self._diagnostics_provider = None

        # 若关闭阶段产生错误，所有 writer 已完成 best-effort 关闭后再抛出第一个错误。
        if close_error is not None:
            raise close_error
        # 原始业务异常存在时，诊断落盘异常不能遮蔽真正的采集根因。
        if diagnostics_error is not None and exc_value is None:
            raise diagnostics_error

        return None

    def write_frames(self, *, mic_raw: bytes, speaker_ref: bytes, mic_clean: bytes) -> None:
        """写入一组三路对齐的调试 PCM16 帧。

        功能：把同一主循环帧的原始麦克风、扬声器参考和 clean 麦克风分别追加到三路 WAV。
        核心入参：mic_raw 是 AEC 前麦克风 PCM16，speaker_ref 是参考 PCM16，mic_clean 是重采样后的 clean PCM16。
        预期输出：三路 WAV 各追加一段音频，不返回业务值。
        边界异常：录制器未打开时抛出 RuntimeError；任一路 PCM16 字节数非法时由 Pcm16WavWriter 抛出 ValueError。
        """
        # 三个写入器必须同时打开，否则继续写入会制造不完整的调试文件组。
        if self._mic_raw_writer is None or self._speaker_ref_writer is None or self._mic_clean_writer is None:
            raise RuntimeError("SidecarDebugRecorder.write_frames() must be called inside an open context.")

        # 写入顺序保持和文件名语义一致，便于异常时定位是哪一路 PCM 数据出现问题。
        self._mic_raw_writer.write(mic_raw)
        self._speaker_ref_writer.write(speaker_ref)
        self._mic_clean_writer.write(mic_clean)
