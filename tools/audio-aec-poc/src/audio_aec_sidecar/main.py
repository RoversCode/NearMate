from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from livekit import rtc

from audio_aec_poc.session_transport import CaptureProfile, detect_capture_profile
from audio_aec_sidecar.config import SidecarConfig
from audio_aec_sidecar.livekit_publisher import CleanMicPublisher
from audio_aec_sidecar.parent_watchdog import ParentWatchdog
from audio_aec_sidecar.pipeline import (
    SidecarPipelineResult,
    capture_factory_for_profile,
    run_sidecar_pipeline_once,
)


def build_parser() -> argparse.ArgumentParser:
    """构建 LiveKit AEC sidecar 命令行解析器。

    功能：声明连接 LiveKit、绑定父进程、配置音频采样率、预热时间和调试输出目录所需的 CLI 参数。
    核心入参：无显式入参，参数值来自命令行。
    预期输出：返回配置完毕的 argparse.ArgumentParser，parse_args 后可组装 SidecarConfig。
    边界异常：参数类型错误由 argparse 在解析阶段处理；业务合法性由 SidecarConfig.validate 统一处理。
    """
    # CLI 名称使用模块名，方便 README 直接复制 python -m audio_aec_sidecar.main 运行。
    parser = argparse.ArgumentParser(
        prog="audio-aec-sidecar",
        description="连接 LiveKit 并发布经过 AEC 处理的 clean 麦克风音轨。",
    )
    # LiveKit URL、token 和房间名由宿主进程签发并传入，sidecar 不负责生成或打印敏感令牌。
    parser.add_argument("--livekit-url", required=True, help="LiveKit Server URL，例如 wss://livekit.example。")
    parser.add_argument("--token", required=True, help="侧车加入 LiveKit room 的 access token。")
    parser.add_argument("--room-name", required=True, help="目标 LiveKit room 名称，用于日志和配置完整性。")
    # identity 由后端签发并由 desktop main 原样传入，sidecar 不硬编码前缀，避免配置变更后无法启动。
    parser.add_argument("--identity", required=True, help="侧车 LiveKit identity，必须与后端签发的 desktopAec.identity 一致。")
    # parent-pid 是侧车退出边界，宿主进程结束后 sidecar 不应继续占用设备或发布音频。
    parser.add_argument("--parent-pid", required=True, type=int, help="宿主进程 pid，用于 watchdog 探活。")
    # AEC 内部采样率默认 48kHz，贴近系统音频和 WebRTC APM 常见处理格式。
    parser.add_argument("--sample-rate", type=int, default=48000, help="AEC 内部采样率，默认 48000。")
    # clean 输出默认 16kHz，匹配后端实时音频链路和常见 ASR/VAD 输入格式。
    parser.add_argument(
        "--clean-output-sample-rate",
        type=int,
        default=16000,
        help="发布 clean 麦克风采样率，默认 16000。",
    )
    # 预热让 AEC 先学习扬声器到麦克风的回声路径，不在此阶段发布音频。
    parser.add_argument("--warmup-seconds", type=float, default=2.0, help="AEC 预热秒数，默认 2.0。")
    # 调试目录可选，启用后保存三路 WAV，便于排查现场设备或 reference 问题。
    parser.add_argument(
        "--debug-output-dir",
        type=Path,
        default=None,
        help="可选调试输出目录，启用后写入 mic_raw.wav、speaker_ref.wav、mic_clean.wav。",
    )
    # 连续录音保留策略按 run 目录计数，避免单次正式运行被中途裁剪成残缺音频。
    parser.add_argument(
        "--max-debug-runs",
        type=int,
        default=None,
        help="可选调试运行目录保留上限，启用后只保留最近 N 次启动目录。",
    )
    parser.add_argument(
        "--audio-device-mode",
        choices=["system_default", "manual"],
        default="system_default",
        help="音频设备选择模式，默认使用系统默认并允许自动 fallback。",
    )
    parser.add_argument("--microphone-id", default=None, help="手动选择的麦克风设备 id。")
    parser.add_argument("--microphone-name", default=None, help="手动选择的麦克风设备名称。")
    parser.add_argument("--speaker-loopback-id", default=None, help="手动选择的扬声器 loopback 设备 id。")
    parser.add_argument("--speaker-loopback-name", default=None, help="手动选择的扬声器 loopback 设备名称。")
    return parser


def _config_from_args(args: argparse.Namespace) -> SidecarConfig:
    """把 argparse 结果转换为 SidecarConfig。

    功能：集中完成 CLI 命名空间到业务配置对象的字段映射，让 main 保持短小清晰。
    核心入参：args 是 build_parser().parse_args() 的返回值。
    预期输出：返回未显式校验的 SidecarConfig，后续 run_from_config 会调用 validate。
    边界异常：缺少字段代表 parser 定义被破坏，会由属性访问抛出 AttributeError。
    """
    # room_name 虽然当前 connect 只需要 token 和 url，但保存在配置中可用于上层日志和一致性校验。
    return SidecarConfig(
        livekit_url=args.livekit_url,
        token=args.token,
        room_name=args.room_name,
        identity=args.identity,
        parent_pid=args.parent_pid,
        sample_rate=args.sample_rate,
        clean_output_sample_rate=args.clean_output_sample_rate,
        channels=1,
        warmup_seconds=args.warmup_seconds,
        debug_output_dir=args.debug_output_dir,
        max_debug_runs=args.max_debug_runs,
        audio_device_mode=args.audio_device_mode,
        microphone_id=args.microphone_id,
        microphone_name=args.microphone_name,
        speaker_loopback_id=args.speaker_loopback_id,
        speaker_loopback_name=args.speaker_loopback_name,
    )


async def run_from_config(config: SidecarConfig) -> SidecarPipelineResult:
    """从已组装配置启动真实 LiveKit sidecar runtime。

    功能：校验配置并选择一次采集策略，连接 LiveKit room，发布 clean-mic 本地音轨，再启动采集与 AEC 管线；本地路径保持发布后立即 ready，RDP 路径在预缓冲完成后 ready。
    核心入参：config 包含 LiveKit 连接、父进程、采样率、预热和调试目录配置。
    预期输出：返回 run_sidecar_pipeline_once 的结果摘要；真实运行通常由父进程退出触发停止。
    边界异常：LiveKit 连接、track 发布、设备采集、AEC 处理或音频发布失败会透传给调用方；finally 始终尝试 disconnect。
    """
    # 真实连接前再次校验配置，保证 CLI 或测试直接调用 run_from_config 都遵守同一套运行边界。
    config.validate()
    capture_profile = detect_capture_profile()
    selected_capture_factory = capture_factory_for_profile(capture_profile)

    # Room 生命周期只存在于 CLI runtime 层，pipeline 不直接依赖 LiveKit SDK，便于单元测试隔离网络。
    room = rtc.Room()
    # primary_exception 记录连接、发布或 pipeline 的原始失败，finally 中 disconnect 失败不得覆盖它。
    primary_exception: BaseException | None = None
    try:
        # token 已包含 room 和 identity 授权，connect 调用不打印敏感信息。
        await room.connect(config.livekit_url, config.token)

        # AudioSource 参数必须与 clean 输出格式一致，否则发布给后端的 AudioFrame 元数据会错位。
        audio_source = rtc.AudioSource(config.clean_output_sample_rate, config.channels)
        # 轨道名固定为 clean-mic，后端可在日志和 LiveKit 控制台中直观看到 sidecar 发布的音频。
        track = rtc.LocalAudioTrack.create_audio_track("clean-mic", audio_source)
        await room.local_participant.publish_track(track)
        if capture_profile is CaptureProfile.LOCAL_LOW_LATENCY:
            # 本地协议沿用原契约：音轨发布成功后立即放行，不让 RDP 预缓冲改变协议 0 的启动语义。
            emit_ready_event(identity=config.identity, track_name="clean-mic")

        # watchdog 只暴露布尔探活接口，pipeline 每帧调用它决定是否继续运行。
        parent_watchdog = ParentWatchdog(config.parent_pid)
        # CleanMicPublisher 负责把 PCM16 包装成 LiveKit AudioFrame 并交给 AudioSource。
        publisher = CleanMicPublisher(
            audio_source,
            sample_rate=config.clean_output_sample_rate,
            channels=config.channels,
        )
        return await run_sidecar_pipeline_once(
            config,
            publisher,
            frame_count=None,
            capture_factory=selected_capture_factory,
            parent_alive=parent_watchdog.is_parent_alive,
            use_aec=capture_profile is CaptureProfile.LOCAL_LOW_LATENCY,
            on_capture_ready=(
                lambda: emit_ready_event(
                    identity=config.identity,
                    track_name="clean-mic",
                )
                if capture_profile is CaptureProfile.RDP_BUFFERED
                else None
            ),
        )
    except BaseException as exc:
        # 先保存原始异常再重新抛出，finally 仍会尝试 disconnect，但不会让清理异常覆盖业务失败。
        primary_exception = exc
        raise
    finally:
        # 无论运行管线如何退出，都断开 LiveKit room，避免 sidecar 留下悬挂 participant。
        try:
            await room.disconnect()
        except BaseException:
            # 如果已经有原始异常正在传播，disconnect 失败只能作为清理噪声处理，不能掩盖真正故障。
            if primary_exception is not None:
                pass
            else:
                raise


def emit_ready_event(*, identity: str, track_name: str) -> None:
    """向 stdout 输出机器可解析的 sidecar ready 事件。

    功能：在当前采集策略达到启动边界后通知 desktop supervisor；本地路径以 clean-mic 发布为边界，RDP 路径还要求采集预缓冲完成。
    核心入参：identity 是后端签发并传给 sidecar 的显式身份，track_name 是已发布的本地音轨名。
    预期输出：标准输出写入单行 JSON，并立即 flush，便于 Node supervisor 按行解析。
    边界异常：不包含 token、命令行参数或设备路径，避免敏感信息进入主进程日志。
    """
    # ready_payload 是 supervisor 唯一需要解析的机器事件，字段保持小而稳定，避免普通日志影响启动判定。
    ready_payload = {
        "event": "aec-sidecar-ready",
        "identity": identity,
        "track": track_name,
    }

    # flush=True 确保 Node 子进程 stdout 不会因缓冲而延迟 ready，避免 main 进程无谓等待。
    print(json.dumps(ready_payload, ensure_ascii=True), flush=True)


def main() -> None:
    """解析 CLI 参数并运行 LiveKit AEC sidecar。

    功能：读取命令行参数，组装 SidecarConfig，进入 asyncio 事件循环执行 run_from_config，并打印简要退出摘要。
    核心入参：所有参数通过命令行传入。
    预期输出：标准输出显示已发布帧数和停止原因。
    边界异常：参数解析错误由 argparse 处理；运行时异常向外传播，让宿主进程或开发者看到真实失败原因。
    """
    # CLI 层保持薄，复杂的运行时生命周期全部收敛在 run_from_config 中。
    parser = build_parser()
    config = _config_from_args(parser.parse_args())
    result = asyncio.run(run_from_config(config))
    print(f"Frames published: {result.frames_published}")
    print(f"Stop reason: {result.stop_reason}")


# 支持 README 中的 python -m audio_aec_sidecar.main 运行方式。
if __name__ == "__main__":
    main()
