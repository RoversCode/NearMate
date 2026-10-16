from __future__ import annotations

from contextlib import ExitStack
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
from pathlib import Path

import numpy as np
import soxr

from audio_aec_poc.devices import SoundcardCapture
from audio_aec_poc.livekit_aec import LiveKitAecProcessor
from audio_aec_poc.wav_writer import Pcm16WavWriter


@dataclass(frozen=True)
class PipelineConfig:
    """描述一次 Windows AEC PoC 采集运行的核心配置。

    功能：集中保存采集时长、输出目录、内部采样率、clean 输出采样率、声道数和 AEC 预热时长，并提供业务级参数校验。
    核心入参：duration_seconds 是正式采集秒数，output_dir 是运行目录父目录，sample_rate 是内部 AEC 采样率，clean_output_sample_rate 是 mic_clean.wav 采样率，channels 是声道数，warmup_seconds 是正式落盘前的 AEC 收敛时间。
    预期输出：调用 validate 后配置满足 10ms 固定帧长和当前 PoC 的单声道约束。
    边界异常：正式时长小于等于 0、预热时长为负、内部采样率或 clean 输出采样率不能整除 100、声道数不是 1 时抛出 ValueError。
    """

    # 目标采集时长，管线会按 10ms 帧取整得到实际处理帧数。
    duration_seconds: float
    # 每次运行目录的父目录，管线会在其下创建带时间戳的唯一子目录。
    output_dir: Path
    # AEC 内部默认使用 48kHz，更贴近 Windows/WebRTC 常见音频管线，最后接 ASR 时再另行降采样。
    sample_rate: int = 48000
    # clean 输出默认保存为 16kHz，方便主观试听并贴近多数 ASR/VAD 输入习惯。
    clean_output_sample_rate: int = 16000
    # 当前 Windows AEC PoC 只验证单声道链路，避免多声道混音策略引入额外变量。
    channels: int = 1
    # 正式写入试听 WAV 前先让 AEC 持续接收 reference 和 mic，帮助自适应滤波器收敛到当前设备/房间。
    warmup_seconds: float = 2.0

    def validate(self) -> None:
        """校验采集管线配置是否满足当前 PoC 的固定约束。

        功能：在打开设备和创建输出文件前拒绝不合法配置，避免产生半成品运行目录。
        核心入参：无显式入参，读取当前 PipelineConfig 实例字段。
        预期输出：配置合法时返回 None。
        边界异常：duration_seconds 小于等于 0、warmup_seconds 小于 0、sample_rate 或 clean_output_sample_rate 不能整除 100、channels 不等于 1 时抛出 ValueError。
        """
        # 采集时长必须为正数，否则帧数计算和 manifest 语义都会失去业务意义。
        if self.duration_seconds <= 0:
            raise ValueError("duration_seconds must be greater than 0.")

        # 预热可以为 0 以便快速对照冷启动效果，但不能为负数。
        if self.warmup_seconds < 0:
            raise ValueError("warmup_seconds must be greater than or equal to 0.")

        # AEC 处理器按 10ms 帧工作，采样率必须能被 100 整除才可形成整数样本数。
        if self.sample_rate % 100 != 0:
            raise ValueError("sample_rate must be divisible by 100.")

        # clean 输出仍按 10ms 帧逐帧写入，采样率也必须能形成整数帧样本数。
        if self.clean_output_sample_rate % 100 != 0:
            raise ValueError("clean_output_sample_rate must be divisible by 100.")

        # 当前 PoC 明确只支持单声道，先收窄问题面，后续多声道策略可单独设计。
        if self.channels != 1:
            raise ValueError("channels must be 1 for the current AEC PoC.")


@dataclass(frozen=True)
class PipelineResult:
    """描述一次采集管线运行完成后的结果摘要。

    功能：把输出运行目录和实际处理帧数返回给 CLI、测试或上层调度代码。
    核心入参：run_dir 是本次运行的唯一输出目录，frames_processed 是成功写入和处理的帧数。
    预期输出：调用方可据此定位 WAV 文件与 manifest，并展示处理进度结果。
    边界异常：本数据类不校验路径存在性，文件系统结果由 run_capture_pipeline 负责保证。
    """

    # 本次运行的唯一目录，内部包含三路 WAV 和 manifest.json。
    run_dir: Path
    # 主循环成功处理的 10ms 帧数量，用于和期望帧数以及 WAV 长度交叉验证。
    frames_processed: int


@dataclass
class _FrameAdjustmentStats:
    """记录采集帧被补零或裁剪的次数。

    功能：累计扬声器参考流和麦克风流在行数维度上的补齐、裁剪次数，写入 manifest 供诊断。
    核心入参：各字段由管线内部逐帧累加，调用方无需直接构造。
    预期输出：to_manifest 返回稳定 JSON 字段名，便于测试和人工排查短读、过读问题。
    边界异常：本统计类不参与音频处理决策，不会主动抛出业务异常。
    """

    # 扬声器参考帧短于 10ms 时被补零的帧次数。
    speaker_reference_padded_frames: int = 0
    # 扬声器参考帧长于 10ms 时被裁剪的帧次数。
    speaker_reference_trimmed_frames: int = 0
    # 麦克风帧短于 10ms 时被补零的帧次数。
    microphone_padded_frames: int = 0
    # 麦克风帧长于 10ms 时被裁剪的帧次数。
    microphone_trimmed_frames: int = 0

    def record(self, stream_name: str, *, padded: bool, trimmed: bool) -> None:
        """按流名称累加一次帧长度修正记录。

        功能：把帧规整阶段发现的补零或裁剪事件归类到参考流或麦克风流。
        核心入参：stream_name 是 speaker_reference 或 microphone，padded 和 trimmed 表示本帧是否发生对应修正。
        预期输出：当前统计对象中的计数字段被原地更新。
        边界异常：未知 stream_name 会抛出 ValueError，避免 manifest 统计字段悄悄丢失。
        """
        # 没有发生长度修正时直接返回，减少主循环中的无意义统计写入。
        if not padded and not trimmed:
            return

        # 参考流和麦克风流分开统计，便于判断问题来自 loopback 还是输入设备。
        if stream_name == "speaker_reference":
            if padded:
                self.speaker_reference_padded_frames += 1
            if trimmed:
                self.speaker_reference_trimmed_frames += 1
            return

        # 麦克风流短读或过读会影响近端输入，单独记录有助于后续设备兼容性分析。
        if stream_name == "microphone":
            if padded:
                self.microphone_padded_frames += 1
            if trimmed:
                self.microphone_trimmed_frames += 1
            return

        # 未知流名称说明调用方或后续维护者传错了业务标识，必须尽早暴露。
        raise ValueError(f"Unknown stream_name: {stream_name}")

    def to_manifest(self) -> dict[str, int]:
        """转换为 manifest 中稳定的帧修正统计结构。

        功能：提供 JSON 可序列化字典，字段名与测试和人工排查文档保持一致。
        核心入参：无显式入参，读取当前统计对象字段。
        预期输出：返回包含四个计数字段的 dict。
        边界异常：本方法只读取整数计数，不会主动抛出业务异常。
        """
        # 使用显式字段而非 asdict，避免未来内部新增字段时无意扩大 manifest 契约。
        return {
            "speaker_reference_padded_frames": self.speaker_reference_padded_frames,
            "speaker_reference_trimmed_frames": self.speaker_reference_trimmed_frames,
            "microphone_padded_frames": self.microphone_padded_frames,
            "microphone_trimmed_frames": self.microphone_trimmed_frames,
        }


def float32_to_pcm16(frame: np.ndarray) -> bytes:
    """将 float32 音频帧转换为 PCM16 小端字节。

    功能：把浮点采样值裁剪到 [-1.0, 1.0]，按 int16 幅度缩放，并输出可写入 WAV 或送入 AEC 的字节流。
    核心入参：frame 是任意形状的 numpy 数组，元素表示归一化浮点音频采样。
    预期输出：返回连续的 little-endian PCM16 bytes，样本顺序遵循输入数组的内存展开顺序。
    边界异常：输入无法转换为数值数组时由 numpy 抛出异常；NaN 会按 numpy 转换规则落入 int16。
    """
    # 转为 float32 能统一上游设备和测试输入类型，并减少后续缩放时的隐式类型差异。
    float_frame = np.asarray(frame, dtype=np.float32)
    # 真实采集可能出现轻微越界，先裁剪可避免 int16 溢出造成削波方向错误。
    clipped = np.clip(float_frame, -1.0, 1.0)
    # 使用 32767 对称缩放可避免 +1.0 变成 int16 溢出的 -32768。
    pcm16 = (clipped * 32767.0).astype("<i2")
    return pcm16.tobytes()


def resample_pcm16_mono(pcm16: bytes, *, source_sample_rate: int, target_sample_rate: int) -> bytes:
    """将单声道 PCM16 从源采样率转换到目标采样率。

    功能：为试听和 ASR/VAD 验证提供独立 clean 输出采样率，不改变 AEC 内部处理采样率。
    核心入参：pcm16 是源采样率下的单声道小端 PCM16，source_sample_rate 是 AEC 内部采样率，target_sample_rate 是 mic_clean.wav 的保存采样率。
    预期输出：返回目标采样率下的单声道小端 PCM16 字节。
    边界异常：PCM16 字节数为奇数、采样率非正数时抛出 ValueError。
    """
    # PCM16 每个样本固定 2 字节，奇数字节代表上游 AEC 输出损坏或切帧错误。
    if len(pcm16) % 2 != 0:
        raise ValueError("PCM16 data must contain an even number of bytes.")

    # 采样率必须为正数，避免后续比例计算出现除零或反向时间。
    if source_sample_rate <= 0 or target_sample_rate <= 0:
        raise ValueError("source_sample_rate and target_sample_rate must be greater than 0.")

    # 采样率相同时直接返回原始字节，避免无意义的 numpy 转换和舍入误差。
    if source_sample_rate == target_sample_rate:
        return pcm16

    # 将 PCM16 转为 [-1, 1] float32，交给 soxr 做高质量重采样，避免手写插值引入额外 DSP 误差。
    source_samples = np.frombuffer(pcm16, dtype="<i2").astype(np.float32) / 32768.0
    if source_samples.size == 0:
        return b""

    # soxr 是专门的重采样库，比简单抽点或手写线性插值更适合主观试听和 ASR 前处理验证。
    target_samples = soxr.resample(
        source_samples,
        source_sample_rate,
        target_sample_rate,
        quality="HQ",
    )
    return np.clip(np.rint(target_samples * 32767.0), -32768, 32767).astype("<i2").tobytes()


def _enforce_exact_frame_shape(
    frame: np.ndarray,
    *,
    frame_samples: int,
    channels: int,
    stream_name: str,
    stats: _FrameAdjustmentStats,
) -> np.ndarray:
    """把采集帧修正为精确的 10ms 二维 float32 形状。

    功能：在 PCM16 转换前统一帧的行数和声道列数，短帧补静音、长帧裁剪、多余声道丢弃、缺失声道补零。
    核心入参：frame 是采集器返回的原始帧，frame_samples 是 10ms 行数，channels 是目标声道数，stream_name 用于统计归类，stats 用于记录修正次数。
    预期输出：返回形状为 (frame_samples, channels) 的 numpy.float32 数组。
    边界异常：输入不是一维或二维数组时抛出 ValueError，避免静默解释错误维度的音频数据。
    """
    # 先统一 dtype，保证补零数组和裁剪结果在后续转换阶段行为一致。
    normalized = np.asarray(frame, dtype=np.float32)

    # 单声道采集有时返回一维数组，升维后统一按“行是样本、列是声道”的约定处理。
    if normalized.ndim == 1:
        normalized = normalized.reshape(-1, 1)

    # 三维及以上通常代表调用方传入了批次或交错结构，管线无法可靠猜测业务含义。
    if normalized.ndim != 2:
        raise ValueError("audio frame must be a one-dimensional or two-dimensional array.")

    # 声道数不足时补静音列，保证后续 PCM16 字节数严格匹配 AEC 初始化参数。
    if normalized.shape[1] < channels:
        missing_channels = channels - normalized.shape[1]
        channel_padding = np.zeros((normalized.shape[0], missing_channels), dtype=np.float32)
        normalized = np.concatenate((normalized, channel_padding), axis=1)

    # 声道数过多时只保留前 channels 列，当前 PoC 不做多声道混音，避免引入不可控 DSP 策略。
    if normalized.shape[1] > channels:
        normalized = normalized[:, :channels]

    # 行数短于 10ms 时在末尾补静音，保持时间轴前半段真实采样不被移动。
    padded = normalized.shape[0] < frame_samples
    if padded:
        row_padding = np.zeros((frame_samples - normalized.shape[0], channels), dtype=np.float32)
        normalized = np.concatenate((normalized, row_padding), axis=0)

    # 行数长于 10ms 时裁掉尾部样本，确保每次 AEC 调用只处理一个固定时间片。
    trimmed = normalized.shape[0] > frame_samples
    if trimmed:
        normalized = normalized[:frame_samples, :]

    # 将长度修正写入统计，manifest 可暴露真实设备是否存在短读或过读现象。
    stats.record(stream_name, padded=padded, trimmed=trimmed)
    return normalized


def _create_unique_run_dir(output_dir: Path) -> tuple[Path, str]:
    """在输出父目录下创建一个唯一运行目录。

    功能：使用 UTC 时间戳作为目录名主体，若同名目录已存在则追加递增后缀。
    核心入参：output_dir 是所有运行目录的父路径。
    预期输出：返回已创建的 run_dir，以及用于 manifest 的 ISO UTC 开始时间字符串。
    边界异常：父目录不可写或目录创建失败时透传文件系统异常。
    """
    # 同一个时间点既用于目录名也用于 manifest，方便人工把文件夹和运行记录对应起来。
    started_at = datetime.now(timezone.utc)
    run_started_at = started_at.isoformat()
    # 文件夹名避免冒号等在 Windows 路径中容易引起麻烦的字符。
    run_name = started_at.strftime("%Y%m%dT%H%M%S%fZ")

    # 先确保父目录存在，让后续 mkdir(exist_ok=False) 只负责唯一性判断。
    output_dir.mkdir(parents=True, exist_ok=True)

    # 时间戳极少碰撞，但自动后缀可让快速连续测试或人工运行保持稳定。
    for suffix in range(1000):
        candidate = output_dir / (run_name if suffix == 0 else f"{run_name}-{suffix}")
        try:
            candidate.mkdir()
            return candidate, run_started_at
        except FileExistsError:
            continue

    # 如果 1000 次都碰撞，说明输出目录状态异常，直接失败比覆盖旧产物安全。
    raise RuntimeError("Unable to create a unique capture run directory.")


def _read_exact_pcm16_pair(
    capture: object,
    *,
    frame_samples: int,
    channels: int,
    frame_adjustments: _FrameAdjustmentStats,
) -> tuple[bytes, bytes]:
    """读取一组扬声器参考帧和麦克风帧，并转换为精确 10ms PCM16。

    功能：统一执行双路读取、帧形状规整和 PCM16 转换，确保 warmup 阶段和正式录制阶段使用完全相同的输入处理逻辑。
    核心入参：capture 是实现 read_frame_pair 的采集器，frame_samples 是 10ms 样本数，channels 是目标声道数，frame_adjustments 记录补零和裁剪统计。
    预期输出：返回 reference_pcm16 和 microphone_pcm16，两者均为精确 10ms PCM16 字节。
    边界异常：采集器读取失败、帧维度非法或转换失败时透传对应异常，便于定位设备或数据问题。
    """
    # 采集器负责保证帧对的时间关系；本地路径仍是相邻读取，RDP 路径则从对齐缓冲原子取出。
    reference_frame, microphone_frame = capture.read_frame_pair()

    # AEC 要求输入是精确 10ms PCM16，因此在转换前统一补零或裁剪 float32 帧。
    exact_reference_frame = _enforce_exact_frame_shape(
        reference_frame,
        frame_samples=frame_samples,
        channels=channels,
        stream_name="speaker_reference",
        stats=frame_adjustments,
    )
    exact_microphone_frame = _enforce_exact_frame_shape(
        microphone_frame,
        frame_samples=frame_samples,
        channels=channels,
        stream_name="microphone",
        stats=frame_adjustments,
    )

    # 转换后输出给 AEC、WAV 写入器或预热流程复用。
    return float32_to_pcm16(exact_reference_frame), float32_to_pcm16(exact_microphone_frame)


def _write_manifest(
    run_dir: Path,
    *,
    run_started_at: str,
    config: PipelineConfig,
    frames_processed: int,
    warmup_frames_processed: int,
    device_info: object,
    aec: object,
    frame_adjustments: _FrameAdjustmentStats,
) -> None:
    """写出一次采集运行的 manifest.json。

    功能：将运行参数、设备信息、AEC 配置和帧修正统计持久化，作为后续诊断入口。
    核心入参：run_dir 是运行目录，run_started_at 是开始时间，config 是采集配置，frames_processed 是处理帧数，device_info 和 aec 来自运行时对象。
    预期输出：run_dir/manifest.json 被写入 UTF-8 JSON 文本。
    边界异常：路径不可写时透传文件系统异常，device_info 不是 dataclass 时会退化为可读字符串结构。
    """
    # 设备信息通常是 DeviceInfo dataclass，使用 asdict 可保留明确字段名。
    if hasattr(device_info, "__dataclass_fields__"):
        manifest_device_info: object = asdict(device_info)
    else:
        manifest_device_info = {"description": str(device_info)}

    # AEC 配置记录当前处理器类名和音频参数，方便区分真实 LiveKit 与测试替身。
    aec_config = {
        "processor": type(aec).__name__,
    }

    # manifest 采用显式结构，避免调用方需要从 WAV 文件头反推基础运行参数。
    manifest = {
        "run_started_at": run_started_at,
        "duration_seconds": config.duration_seconds,
        "sample_rate": config.sample_rate,
        "clean_output_sample_rate": config.clean_output_sample_rate,
        "channels": config.channels,
        "warmup_seconds": config.warmup_seconds,
        "frame_duration_ms": 10,
        "warmup_frames_processed": warmup_frames_processed,
        "frames_processed": frames_processed,
        "device_info": manifest_device_info,
        "aec": aec_config,
        "frame_adjustments": frame_adjustments.to_manifest(),
    }

    # 使用缩进 JSON 方便人工直接打开查看；ensure_ascii=False 保留设备名称中的本地字符。
    (run_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def run_capture_pipeline(
    config: PipelineConfig,
    capture_factory=SoundcardCapture,
    aec_factory=LiveKitAecProcessor,
) -> PipelineResult:
    """执行一次固定 10ms 分帧的 AEC 采集管线。

    功能：校验配置、创建唯一输出目录、打开采集器与三路 WAV 写入器，逐帧读取参考流和麦克风流，送入 AEC 并写出 manifest。
    核心入参：config 是管线配置，capture_factory 用于注入真实或测试采集器，aec_factory 用于注入真实或测试 AEC 处理器。
    预期输出：返回包含运行目录和实际处理帧数的 PipelineResult。
    边界异常：配置非法时抛出 ValueError；设备、AEC、文件写入失败时保留底层异常，已创建的运行目录不会被自动删除。
    """
    # 在任何外部资源创建前先校验配置，避免非法参数留下空目录或半成品文件。
    config.validate()

    # LiveKit AEC 固定使用 10ms 帧，因此每帧样本数由采样率除以 100 得出。
    frame_samples = config.sample_rate // 100
    # warmup_seconds 映射到 10ms 帧数，只喂给 AEC，不写入正式试听 WAV。
    warmup_frame_count = int(round(config.warmup_seconds * 100))
    # duration_seconds 映射到 10ms 帧数，最短也处理一帧，方便极短 PoC 运行仍有输出。
    frame_count = max(1, int(round(config.duration_seconds * 100)))
    # 每次运行写入独立目录，防止覆盖历史 WAV 和 manifest。
    run_dir, run_started_at = _create_unique_run_dir(config.output_dir)
    # 帧修正统计跨整次运行累积，最终写入 manifest。
    frame_adjustments = _FrameAdjustmentStats()
    # AEC 处理器在采集开始前创建，保证每一帧都共享同一个连续处理上下文。
    aec = aec_factory(sample_rate=config.sample_rate, channels=config.channels)
    # 成功处理帧数只在三路 WAV 都写入后递增，避免异常时夸大结果。
    frames_processed = 0
    # 预热帧数只统计已喂给 AEC 的帧，不计入正式 WAV 时长。
    warmup_frames_processed = 0

    # 使用 ExitStack 同时管理采集器和三个 WAV 写入器，任何一步失败都会按已进入顺序释放资源。
    with ExitStack() as stack:
        capture = stack.enter_context(
            capture_factory(
                sample_rate=config.sample_rate,
                channels=config.channels,
                frame_samples=frame_samples,
            )
        )
        mic_raw_writer = stack.enter_context(
            Pcm16WavWriter(run_dir / "mic_raw.wav", sample_rate=config.sample_rate, channels=config.channels)
        )
        speaker_ref_writer = stack.enter_context(
            Pcm16WavWriter(run_dir / "speaker_ref.wav", sample_rate=config.sample_rate, channels=config.channels)
        )
        mic_clean_writer = stack.enter_context(
            Pcm16WavWriter(
                run_dir / "mic_clean.wav",
                sample_rate=config.clean_output_sample_rate,
                channels=config.channels,
            )
        )

        # 预热阶段只让 AEC 学习当前扬声器到麦克风的回声路径，不污染正式试听文件。
        for _ in range(warmup_frame_count):
            reference_pcm16, microphone_pcm16 = _read_exact_pcm16_pair(
                capture,
                frame_samples=frame_samples,
                channels=config.channels,
                frame_adjustments=frame_adjustments,
            )
            aec.process_frame(reference_pcm16, microphone_pcm16)
            warmup_frames_processed += 1

        # 主循环以固定帧数运行，保证输出时长由配置决定，而不是由设备偶发读取长度决定。
        for _ in range(frame_count):
            reference_pcm16, microphone_pcm16 = _read_exact_pcm16_pair(
                capture,
                frame_samples=frame_samples,
                channels=config.channels,
                frame_adjustments=frame_adjustments,
            )
            clean_pcm16 = aec.process_frame(reference_pcm16, microphone_pcm16)
            clean_output_pcm16 = resample_pcm16_mono(
                clean_pcm16,
                source_sample_rate=config.sample_rate,
                target_sample_rate=config.clean_output_sample_rate,
            )

            # 三路 WAV 分别保留原始麦克风、扬声器参考和清洁麦克风，便于离线对比。
            mic_raw_writer.write(microphone_pcm16)
            speaker_ref_writer.write(reference_pcm16)
            mic_clean_writer.write(clean_output_pcm16)
            frames_processed += 1

    # 采集和 WAV 文件关闭后再写 manifest，确保帧数和文件产物已经稳定落盘。
    _write_manifest(
        run_dir,
        run_started_at=run_started_at,
        config=config,
        frames_processed=frames_processed,
        warmup_frames_processed=warmup_frames_processed,
        device_info=capture.device_info,
        aec=aec,
        frame_adjustments=frame_adjustments,
    )
    return PipelineResult(run_dir=run_dir, frames_processed=frames_processed)
