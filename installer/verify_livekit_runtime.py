"""用随包媒体程序和两个真实 RTC 参与者验证本机音频传输。"""

from __future__ import annotations

import argparse
from array import array
import asyncio
import base64
from contextlib import ExitStack
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request


def create_server_configuration(api_key: str, api_secret: str) -> dict[str, object]:
    """准备与 Desktop 相同的随机端口和仅本机信令配置。

    Args:
        api_key: 本次 smoke 的临时服务身份。
        api_secret: 本次 smoke 的临时签名密钥；不写入文件或命令行。

    Returns:
        可通过 LIVEKIT_CONFIG 传给服务的配置；媒体使用本机网卡候选。
    """
    # 同时保留探测 socket，防止操作系统把刚释放的同一 TCP 端口分配两次。
    with ExitStack() as stack:
        sockets = [stack.enter_context(socket.socket(socket.AF_INET, kind))
                   for kind in (socket.SOCK_STREAM, socket.SOCK_STREAM, socket.SOCK_DGRAM)]
        for probe in sockets:
            probe.bind(("127.0.0.1", 0))
        control_port, tcp_port, udp_port = [probe.getsockname()[1] for probe in sockets]
    return {
        "port": control_port,
        "bind_addresses": ["127.0.0.1"],
        "rtc": {"tcp_port": tcp_port, "udp_port": udp_port,
                "enable_loopback_candidate": True, "use_external_ip": False},
        "keys": {api_key: api_secret},
        "logging": {"level": "error"},
    }


def _create_token(api_key: str, api_secret: str, video_grant: dict[str, object],
                  identity: str = "") -> str:
    """签发只用于本轮媒体验证的短期令牌。

    Args:
        api_key: 本轮服务身份。
        api_secret: 只留在内存的本轮签名密钥。
        video_grant: 管理健康请求或单个测试房间所需权限。
        identity: 入房参与者身份；管理请求留空。

    Returns:
        两分钟内有效的 HS256 令牌；调用方不得记录它。
    """
    segments = [base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode())
                .decode().rstrip("=") for value in (
                    {"alg": "HS256", "typ": "JWT"},
                    {"iss": api_key, "sub": identity, "exp": int(time.time()) + 120,
                     "video": video_grant},
                )]
    payload = ".".join(segments)
    signature = hmac.new(api_secret.encode(), payload.encode(), hashlib.sha256).digest()
    return payload + "." + base64.urlsafe_b64encode(signature).decode().rstrip("=")


async def _verify_media(configuration: dict[str, object], api_key: str,
                        api_secret: str) -> dict[str, object]:
    """先认证服务，再用两个参与者发送并接收合成音频。

    Args:
        configuration: 与 Desktop 一致的本机服务配置。
        api_key: 本轮服务身份。
        api_secret: 本轮签名密钥。

    Returns:
        真实接收帧数、非零采样数与参与者数量，不包含音频正文或凭据。
    """
    from livekit import rtc

    http_url = f"http://127.0.0.1:{configuration['port']}"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(
        http_url + "/twirp/livekit.RoomService/ListRooms", data=b"{}",
        headers={"Authorization": "Bearer " + _create_token(api_key, api_secret, {"roomList": True}),
                 "Content-Type": "application/json"},
    )
    print("NEARMATE_LIVEKIT_STAGE=authenticated_readiness", flush=True)
    deadline = time.monotonic() + 10
    while True:
        try:
            with opener.open(request, timeout=1) as response:
                if response.status != 200:
                    raise RuntimeError("LiveKit 认证健康检查未通过")
            break
        except (urllib.error.URLError, TimeoutError):
            if time.monotonic() >= deadline:
                raise RuntimeError("LiveKit 认证健康检查超时") from None
            await asyncio.sleep(0.1)

    rooms: list[rtc.Room] = []
    streams: list[rtc.AudioStream] = []
    receive_tasks: list[asyncio.Task[None]] = []
    source: rtc.AudioSource | None = None
    received_frames = 0
    received_nonzero_samples = 0
    audio_received = asyncio.Event()
    try:
        for identity in ("smoke-receiver", "smoke-sender"):
            print("NEARMATE_LIVEKIT_STAGE=" + identity, flush=True)
            room = rtc.Room()
            rooms.append(room)
            token = _create_token(api_key, api_secret, {
                "room": "nearmate-build-smoke", "roomJoin": True,
                "canPublish": True, "canSubscribe": True,
            }, identity)
            await asyncio.wait_for(room.connect(http_url.replace("http:", "ws:"), token), 15)

        async def receive_audio(track: rtc.RemoteAudioTrack) -> None:
            nonlocal received_frames, received_nonzero_samples
            stream = rtc.AudioStream(track)
            streams.append(stream)
            async for event in stream:
                received_frames += 1
                received_nonzero_samples += sum(sample != 0 for sample in event.frame.data)
                if received_frames >= 3 and received_nonzero_samples > 0:
                    audio_received.set()
                    return

        @rooms[0].on("track_subscribed")
        def on_track_subscribed(track, publication, participant) -> None:
            if track.kind == rtc.TrackKind.KIND_AUDIO:
                receive_tasks.append(asyncio.create_task(receive_audio(track)))

        source = rtc.AudioSource(48000, 1)
        track = rtc.LocalAudioTrack.create_audio_track("smoke-audio", source)
        print("NEARMATE_LIVEKIT_STAGE=publish_audio", flush=True)
        await asyncio.wait_for(rooms[1].local_participant.publish_track(track), 10)
        # 合成非零信号，不打开麦克风；只有另一参与者收到真实解码音频才算通过。
        samples = array("h", (int(6000 * math.sin(2 * math.pi * 400 * index / 48000))
                              for index in range(960)))
        frame = rtc.AudioFrame(samples.tobytes(), 48000, 1, 960)
        print("NEARMATE_LIVEKIT_STAGE=receive_audio", flush=True)
        for _ in range(250):
            await asyncio.wait_for(source.capture_frame(frame), 2)
            if audio_received.is_set():
                break
            await asyncio.sleep(0.02)
        await asyncio.wait_for(audio_received.wait(), 5)
        return {"authenticated_readiness": True, "participants": len(rooms),
                "received_frames": received_frames,
                "received_nonzero_samples": received_nonzero_samples}
    finally:
        for task in receive_tasks:
            task.cancel()
        await asyncio.gather(*receive_tasks, return_exceptions=True)
        for resource in [*streams, *([source] if source is not None else [])]:
            await asyncio.wait_for(resource.aclose(), 3)
        for room in rooms:
            await asyncio.wait_for(room.disconnect(), 3)


def verify_livekit_runtime(executable: Path, log_path: Path) -> dict[str, object]:
    """启动固定媒体程序、验证音频并等待本次拥有的进程退出。

    Args:
        executable: 已通过发行摘要校验的 LiveKit Server 程序。
        log_path: 本次服务输出日志，失败时用于定位启动阶段。

    Returns:
        不含凭据的验收证据；任何阶段失败都不返回成功结果。
    """
    api_key, api_secret = "nm_" + secrets.token_hex(16), secrets.token_hex(32)
    configuration = create_server_configuration(api_key, api_secret)
    # 构建机的代理和 LiveKit 配置不得影响本机检查；服务配置只传入它自己的环境。
    for name in list(os.environ):
        if name.upper().endswith("_PROXY") or name.upper().startswith("LIVEKIT_"):
            os.environ.pop(name)
    os.environ["NO_PROXY"] = "127.0.0.1,localhost,::1"
    environment = {**os.environ, "LIVEKIT_CONFIG": json.dumps(configuration)}
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("wb") as log_stream:
        process = subprocess.Popen(
            [str(executable.resolve())], env=environment,
            stdout=log_stream, stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            # RTC 的原生 DLL 在独立子进程执行；即使 native 崩溃，父进程仍能收口媒体服务。
            probe = subprocess.run(
                [sys.executable, "-I", "-B", "-X", "utf8", str(Path(__file__).resolve()), "--participants"],
                env=environment, capture_output=True, text=True, encoding="utf-8", timeout=65,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if probe.returncode != 0:
                stages = [line.removeprefix("NEARMATE_LIVEKIT_STAGE=")
                          for line in probe.stdout.splitlines()
                          if line.startswith("NEARMATE_LIVEKIT_STAGE=")]
                stage = stages[-1] if stages else "native_startup"
                raise RuntimeError(f"LiveKit RTC 参与者检查失败（阶段 {stage}，退出码 {probe.returncode}）")
            probe_line = next((line.removeprefix("NEARMATE_LIVEKIT_MEDIA=") for line in probe.stdout.splitlines()
                               if line.startswith("NEARMATE_LIVEKIT_MEDIA=")), None)
            if probe_line is None:
                raise RuntimeError("LiveKit RTC 参与者没有返回实际音频证据")
            result = json.loads(probe_line)
        finally:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
    result["owned_process_stopped"] = True
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("executable", type=Path, nargs="?")
    parser.add_argument("log", type=Path, nargs="?")
    parser.add_argument("--participants", action="store_true")
    arguments = parser.parse_args()
    if arguments.participants:
        configuration = json.loads(os.environ.pop("LIVEKIT_CONFIG"))
        api_key, api_secret = next(iter(configuration["keys"].items()))
        print("NEARMATE_LIVEKIT_MEDIA=" + json.dumps(asyncio.run(asyncio.wait_for(
            _verify_media(configuration, api_key, api_secret), 60))))
    else:
        if arguments.executable is None or arguments.log is None:
            parser.error("必须提供媒体程序路径和日志路径")
        print("NEARMATE_LIVEKIT_PROBE=" + json.dumps(
            verify_livekit_runtime(arguments.executable, arguments.log), ensure_ascii=False))
