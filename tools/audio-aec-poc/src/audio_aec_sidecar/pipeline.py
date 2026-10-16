from __future__ import annotations

from contextlib import ExitStack, nullcontext
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
import shutil
from typing import Any, Callable, Protocol

from audio_aec_poc.devices import SoundcardCapture
from audio_aec_poc.livekit_aec import LiveKitAecProcessor
from audio_aec_poc.pipeline import _FrameAdjustmentStats, _read_exact_pcm16_pair, resample_pcm16_mono
from audio_aec_poc.rdp_capture import RdpBufferedCapture
from audio_aec_poc.session_transport import CaptureProfile, detect_capture_profile
from audio_aec_sidecar.config import SidecarConfig
from audio_aec_sidecar.debug_recorder import SidecarDebugRecorder


class _Publisher(Protocol):
    """侧车 clean 麦克风发布器协议。

    功能：描述运行管线需要的最小发布能力，让测试替身和 CleanMicPublisher 都可被注入。
    核心入参：publish_pcm16 接收重采样后的 clean PCM16 字节。
    预期输出：发布完成后协程正常返回。
    边界异常：真实发布失败时允许异常透传给调用方，pipeline 不在此吞掉网络或 SDK 错误。
    """

    async def publish_pcm16(self, pcm16: bytes) -> None:
        """发布一帧 clean PCM16。

        功能：把侧车生成的 clean 麦克风音频推给下游实时链路。
        核心入参：pcm16 是已经符合 clean_output_sample_rate 的 PCM16 字节。
        预期输出：发布完成后返回 None。
        边界异常：实现方可抛出格式、网络或 LiveKit 运行时异常。
        """


@dataclass(frozen=True)
class SidecarPipelineResult:
    """侧车运行管线结果摘要。

    功能：记录本次有限帧运行实际发布了多少帧，以及循环停止的业务原因。
    核心入参：frames_published 是成功发布的 10ms clean 帧数，stop_reason 是结束原因。
    预期输出：调用方可据此判断是达到测试帧数、父进程退出还是其他上层控制结束。
    边界异常：本数据类不主动校验字段，字段值由 run_sidecar_pipeline_once 保证。
    """

    # 成功送入 publisher 的 clean 麦克风帧数，不包含 warmup 阶段。
    frames_published: int
    # 停止原因使用稳定字符串，方便 CLI 日志和测试断言。
    stop_reason: str


def capture_factory_for_profile(
    profile: CaptureProfile,
) -> type[SoundcardCapture] | type[RdpBufferedCapture]:
    """把 Windows 会话采集策略映射到具体采集器。

    Args:
        profile: WTS 会话判断得到的采集策略。

    Returns:
        本地策略返回 SoundcardCapture，RDP 策略返回 RdpBufferedCapture。
    """

    if profile is CaptureProfile.LOCAL_LOW_LATENCY:
        return SoundcardCapture
    return RdpBufferedCapture


def _make_debug_recorder_context(
    config: SidecarConfig,
    *,
    raw_sample_rate: int | None = None,
):
    """按配置创建调试录制上下文。

    功能：当 debug_output_dir 存在时创建 SidecarDebugRecorder，否则返回无副作用的空上下文。
    核心入参：config 是已校验的侧车运行配置，raw_sample_rate 是当前采集器实际输出采样率。
    预期输出：返回可被 ExitStack.enter_context 使用的上下文管理器。
    边界异常：调试目录不可写时由 SidecarDebugRecorder 在进入上下文时透传文件系统异常。
    """
    # 未配置调试输出时，pipeline 仍然要保持同一套控制流，nullcontext 让主循环无需分叉创建资源。
    if config.debug_output_dir is None:
        return nullcontext(None)

    # 每次 pipeline 运行都必须拥有独立 run-id 子目录，避免多次 sidecar 调试音频互相覆盖。
    debug_run_dir = _create_unique_debug_run_dir(Path(config.debug_output_dir))
    # 连续录音裁剪以“运行目录”为单位，而不是以单个 WAV 为单位，避免保留下来的最近一次启动变成残缺文件集合。
    _trim_old_debug_run_dirs(
        Path(config.debug_output_dir),
        current_run_dir=debug_run_dir,
        max_debug_runs=config.max_debug_runs,
    )

    # SidecarDebugRecorder 只负责写入传入的 run_dir，run-id 分配留在 pipeline 编排层完成。
    return SidecarDebugRecorder(
        debug_run_dir,
        sample_rate=config.sample_rate if raw_sample_rate is None else raw_sample_rate,
        clean_sample_rate=config.clean_output_sample_rate,
        channels=config.channels,
    )


def _build_debug_run_diagnostics(
    *,
    capture: Any | None,
    started_at: str,
    frames_published: int,
    stop_reason: str,
    exception: BaseException | None,
) -> dict[str, Any]:
    """整理一轮 sidecar 的生命周期、采集水位和失败原因。

    Args:
        capture: 本轮采集器对象，可能在打开前就失败而为空。
        started_at: pipeline 开始时间的 UTC ISO 字符串。
        frames_published: 已经成功发布的 clean 帧数。
        stop_reason: 主循环记录的停止原因。
        exception: 上下文退出时收到的原始异常。

    Returns:
        可直接写入 diagnostics.json 的字典。
    """

    capture_diagnostics = None
    if capture is not None:
        capture_diagnostics = getattr(capture, "microphone_diagnostics", None)
        if is_dataclass(capture_diagnostics):
            capture_diagnostics = asdict(capture_diagnostics)
        elif capture_diagnostics is not None and hasattr(capture_diagnostics, "__dict__"):
            capture_diagnostics = dict(vars(capture_diagnostics))

    failure = None
    if exception is not None:
        failure = {
            "type": type(exception).__name__,
            "message": str(exception),
        }
    # 异常离开上下文时，默认的 frame_count_reached 不能再伪装成正常完成。
    reported_stop_reason = "failed" if exception is not None else stop_reason
    return {
        "started_at": started_at,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "frames_published": frames_published,
        "stop_reason": reported_stop_reason,
        "failure": failure,
        "capture_diagnostics": capture_diagnostics,
    }


def _create_unique_debug_run_dir(debug_output_dir: Path) -> Path:
    """在 debug 输出基目录下创建唯一运行子目录。

    功能：使用 UTC 时间戳生成 Windows 安全的 run-id 目录名，若同名目录已存在则追加递增后缀。
    核心入参：debug_output_dir 是配置传入的调试输出基目录。
    预期输出：返回已经创建成功的 debug run 目录路径。
    边界异常：目录不可写、父目录创建失败或连续碰撞过多时抛出文件系统异常或 RuntimeError。
    """
    # UTC 时间戳让跨时区日志排序稳定，格式避开 Windows 路径中不友好的冒号字符。
    run_name = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    # 先创建基目录，再用 exist_ok=False 创建子目录，避免覆盖已有调试运行。
    debug_output_dir.mkdir(parents=True, exist_ok=True)

    # 正常情况下微秒时间戳不会碰撞；后缀兜底可覆盖测试或极快连续启动场景。
    for suffix in range(1000):
        candidate = debug_output_dir / (run_name if suffix == 0 else f"{run_name}-{suffix}")
        try:
            candidate.mkdir()
            return candidate
        except FileExistsError:
            continue

    # 过多碰撞说明目录状态异常，显式失败比复用旧目录更安全。
    raise RuntimeError("Unable to create a unique sidecar debug run directory.")


def _trim_old_debug_run_dirs(
    debug_output_dir: Path,
    *,
    current_run_dir: Path,
    max_debug_runs: int | None,
) -> None:
    """按运行目录数量裁剪历史 sidecar 调试录音。

    功能：在新 run 目录创建成功后，只保留最近指定数量的 run 目录，并删除更老的整目录。
    核心入参：debug_output_dir 是固定输出根目录，current_run_dir 是本轮刚创建的运行目录，max_debug_runs 是保留上限。
    预期输出：当上限未设置或目录数量未超限时无副作用；超限时删除最旧的多余目录。
    边界异常：目录删除失败时透传文件系统异常，避免调用方误以为裁剪已生效。
    """
    # 未配置保留上限时不主动清理历史目录，保持调试目录行为完全由显式配置决定。
    if max_debug_runs is None:
        return

    # 这里按 run 目录名排序即可，因为目录名由 UTC 时间戳生成，字典序与创建先后保持一致。
    run_directories = sorted(
        (
            child
            for child in debug_output_dir.iterdir()
            if child.is_dir()
        ),
        key=lambda candidate: candidate.name,
    )

    # 当前新建目录必须始终被保留；如果目录总数未超过上限，就没有任何裁剪动作。
    if len(run_directories) <= max_debug_runs:
        return

    directories_to_delete = run_directories[:-max_debug_runs]

    for obsolete_directory in directories_to_delete:
        # 新建目录只可能出现在排序尾部，但仍显式保护，避免未来命名策略调整时误删当前运行目录。
        if obsolete_directory == current_run_dir:
            continue
        shutil.rmtree(obsolete_directory)


async def run_sidecar_pipeline_once(
    config: SidecarConfig,
    publisher: _Publisher,
    frame_count: int | None,
    capture_factory=None,
    aec_factory=LiveKitAecProcessor,
    parent_alive: Callable[[], bool] = lambda: True,
    capture_profile_detector: Callable[[], CaptureProfile] = detect_capture_profile,
    on_capture_ready: Callable[[], None] | None = None,
    use_aec: bool | None = None,
) -> SidecarPipelineResult:
    """运行一次有限帧、可测试的 sidecar 管线。

    功能：校验配置，打开采集器；本地路径使用 AEC，RDP 路径直接发布连续原始麦克风，再完成重采样、调试落盘和 LiveKit 发布。
    核心入参：config 描述连接和音频参数，publisher 接收 clean PCM16，frame_count 限制主循环帧数，capture_factory/aec_factory/parent_alive 供测试注入，capture_profile_detector 决定真实会话路径，on_capture_ready 在设备可用后通知宿主，use_aec 显式控制是否启用 AEC。
    预期输出：返回 SidecarPipelineResult，包含发布帧数和停止原因。
    边界异常：配置非法、采集失败、AEC 失败、调试写入失败或发布失败时透传原始异常，调用方负责记录和退出策略。
    """
    # 在任何外部资源创建前校验配置，避免非法参数打开设备、连接文件或写出半成品调试目录。
    config.validate()

    # 只有真实运行未显式注入采集器时才查询 WTS；单元测试替身不会意外依赖当前桌面会话。
    selected_capture_factory = capture_factory
    if selected_capture_factory is None:
        selected_capture_profile = capture_profile_detector()
        selected_capture_factory = capture_factory_for_profile(selected_capture_profile)
    if use_aec is None:
        # RDP 采集器本身就是无 AEC 直通边界；其它注入采集器默认沿用本地 AEC 测试语义。
        use_aec = selected_capture_factory is not RdpBufferedCapture

    # RDP 让 MediaCapture worker 直接持续重采样到发布采样率，避免每个 10ms 帧独立重采样产生接缝。
    capture_sample_rate = config.sample_rate if use_aec else config.clean_output_sample_rate
    frame_samples = capture_sample_rate // 100
    # RDP 不创建 AEC，也不应为了无用的预热再延迟用户首次说话。
    warmup_frame_count = int(round(config.warmup_seconds * 100)) if use_aec else 0
    # 帧修正统计复用 PoC 管线工具，当前侧车不写 manifest，但仍需要同一个精确帧规整逻辑。
    frame_adjustments = _FrameAdjustmentStats()
    # 发布成功帧数只在 publisher 返回后递增，避免发布异常时夸大结果。
    frames_published = 0
    # 默认停止原因是达到调用方给定的有限帧数，父进程退出会覆盖它。
    stop_reason = "frame_count_reached"
    pipeline_started_at = datetime.now(timezone.utc).isoformat()
    capture = None
    debug_recorder = None

    # 使用 ExitStack 统一管理采集器和可选 debug recorder，任一资源打开失败都会释放已打开资源。
    with ExitStack() as stack:
        # 先打开 debug recorder，再打开采集器，保证采集器关闭后仍能写入最终诊断。
        debug_recorder = stack.enter_context(
            _make_debug_recorder_context(config, raw_sample_rate=capture_sample_rate)
        )
        capture_kwargs: dict[str, object] = {
            "sample_rate": capture_sample_rate,
            "channels": config.channels,
            "frame_samples": frame_samples,
        }
        # 两种真实采集器都需要麦克风选择参数，但只有本地 SoundcardCapture 还需要 loopback 选择。
        if selected_capture_factory is SoundcardCapture:
            capture_kwargs.update(
                {
                    "audio_device_mode": config.audio_device_mode,
                    "microphone_id": config.microphone_id,
                    "microphone_name": config.microphone_name,
                    "speaker_loopback_id": config.speaker_loopback_id,
                    "speaker_loopback_name": config.speaker_loopback_name,
                }
            )
        elif selected_capture_factory is RdpBufferedCapture:
            capture_kwargs.update(
                {
                    "audio_device_mode": config.audio_device_mode,
                    "microphone_id": config.microphone_id,
                    "microphone_name": config.microphone_name,
                }
            )
        if debug_recorder is not None:
            debug_recorder.set_diagnostics_provider(
                lambda exception: _build_debug_run_diagnostics(
                    capture=capture,
                    started_at=pipeline_started_at,
                    frames_published=frames_published,
                    stop_reason=stop_reason,
                    exception=exception,
                )
            )
        capture = stack.enter_context(selected_capture_factory(**capture_kwargs))
        # RDP 设备已经由 MediaCapture 直接提供连续麦克风；只有本地路径创建并预热 AEC。
        aec = aec_factory(sample_rate=config.sample_rate, channels=config.channels) if use_aec else None
        if on_capture_ready is not None:
            # 走到这里说明采集器已打开；RDP 路径还已经完成约 2 秒麦克风预缓冲，可以安全对外声明 ready。
            on_capture_ready()

        # warmup 也必须尊重父进程生命周期，避免宿主刚退出时 sidecar 继续占用设备或 LiveKit room。
        for _ in range(warmup_frame_count):
            # 每个 warmup 帧读取前先探活，父进程已退出时直接返回，且 warmup 不可能产生已发布帧。
            if not parent_alive():
                return SidecarPipelineResult(frames_published=frames_published, stop_reason="parent_exited")

            reference_pcm16, microphone_pcm16 = _read_exact_pcm16_pair(
                capture,
                frame_samples=frame_samples,
                channels=config.channels,
                frame_adjustments=frame_adjustments,
            )
            assert aec is not None
            aec.process_frame(reference_pcm16, microphone_pcm16)

        # frame_count 为 None 时用于真实 CLI 长运行；测试通常传入有限整数让管线可确定结束。
        while frame_count is None or frames_published < frame_count:
            # 每帧发布前都检查父进程，父进程退出代表宿主已不需要 sidecar，应立即停止采集和发布。
            if not parent_alive():
                stop_reason = "parent_exited"
                break

            reference_pcm16, microphone_pcm16 = _read_exact_pcm16_pair(
                capture,
                frame_samples=frame_samples,
                channels=config.channels,
                frame_adjustments=frame_adjustments,
            )
            # RDP 不做 AEC，clean 音频就是 MediaCapture 交付的连续麦克风；本地才经过 AEC。
            if aec is None:
                clean_output_pcm16 = microphone_pcm16
            else:
                clean_pcm16 = aec.process_frame(reference_pcm16, microphone_pcm16)
                clean_output_pcm16 = resample_pcm16_mono(
                    clean_pcm16,
                    source_sample_rate=config.sample_rate,
                    target_sample_rate=config.clean_output_sample_rate,
                )

            # 调试录制只保存正式主循环帧，避免 warmup 音频混入人工检查样本。
            if debug_recorder is not None:
                debug_recorder.write_frames(
                    mic_raw=microphone_pcm16,
                    speaker_ref=reference_pcm16,
                    mic_clean=clean_output_pcm16,
                )

            # 发布必须 await，让运行循环天然服从 LiveKit AudioSource 的背压节奏。
            await publisher.publish_pcm16(clean_output_pcm16)
            frames_published += 1

    return SidecarPipelineResult(frames_published=frames_published, stop_reason=stop_reason)
