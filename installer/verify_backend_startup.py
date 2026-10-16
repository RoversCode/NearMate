"""用 Desktop 同构的 packaged 环境验证单个发行 profile 的 Backend 启动。"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import socket
import subprocess
import time
from typing import Mapping
import urllib.error
import urllib.request
import uuid

import yaml


_PATH_KEYS = (
    "python",
    "backend",
    "native",
    "shared",
    "console",
    "aec_package",
    "model_catalog",
    "seed_character_cards",
    "assets",
    "livekit_server",
)
_FIXED_RUNTIME_FILES = frozenset(
    {
        "tools/claude/claude.exe",
        "tools/cua/cua-driver.exe",
        "tools/cua/cua-driver-uia.exe",
        "tools/git/bin/bash.exe",
        "tools/git/cmd/git.exe",
        "tools/ffmpeg/bin/ffmpeg.exe",
        "tools/ffmpeg/bin/ffprobe.exe",
        "tools/ffmpeg/LICENSE",
        "tools/ffmpeg/README.txt",
    }
)
_EXPECTED_TTS_MODELS = frozenset(
    {
        "qwen3-tts-clone-0.6b-gguf",
        "qwen3-tts-clone-1.7b-gguf",
        "s2-pro-4b-gguf",
        "voxcpm2-gguf",
        "moss-tts-nano-onnx",
    }
)
_PROFILE_EXPECTATIONS = {
    "cuda": {
        "asr_onnx_provider": "cuda",
        "streamvox_onnx_provider": "cuda",
        "llama_backend": "cuda",
        "available_onnx_provider": "CUDAExecutionProvider",
    },
    "universal": {
        "asr_onnx_provider": "dml",
        "streamvox_onnx_provider": "directml",
        "llama_backend": "vulkan",
        "available_onnx_provider": "DmlExecutionProvider",
    },
}
_WINDOWS_RESERVED_NAME = re.compile(
    r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])$",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class RuntimeContract:
    """保存通过 schema v3 和必需文件校验的安装运行布局。

    Args:
        runtime: 运行时根目录。
        manifest: 已验证的原始清单。
        profile: 当前 CUDA 或 Universal 发行档位。
        python_path: 冻结 Python 解释器。
        backend_root: Backend 的工作目录。
        native_root: 原生推理库目录。
        shared_root: 共享契约目录。
        console_root: Console 静态资源目录。
        model_catalog_path: 固定 TTS 模型清单路径。
        livekit_server_path: 安装包自带的固定媒体服务程序。
    """

    runtime: Path
    manifest: dict[str, object]
    profile: str
    python_path: Path
    backend_root: Path
    native_root: Path
    shared_root: Path
    console_root: Path
    model_catalog_path: Path
    livekit_server_path: Path


def load_runtime_contract(runtime: Path) -> RuntimeContract:
    """读取 schema v3 清单并验证安装 smoke 依赖的资源。

    Args:
        runtime: 解包后的 runtime 目录。

    Returns:
        不再需要重复推导路径的已验证合同。

    Raises:
        RuntimeError: 清单字段、路径、必需文件或摘要不合法时抛出。
    """

    runtime = runtime.expanduser().resolve()
    manifest_path = runtime / "runtime-manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exception:
        raise RuntimeError(
            f"无法读取发行 runtime-manifest.json：{manifest_path}"
        ) from exception

    if not isinstance(manifest, dict):
        raise RuntimeError("发行 runtime-manifest.json 必须是 JSON 对象")
    profile = manifest.get("profile")
    if (
        manifest.get("schema_version") != 3
        or manifest.get("product") != "NearMate"
        or manifest.get("platform") != "windows-x64"
        or manifest.get("python_version") != "3.12.13"
        or profile not in _PROFILE_EXPECTATIONS
    ):
        raise RuntimeError("安装 smoke 只接受 NearMate windows-x64 schema v3 清单")

    raw_paths = manifest.get("paths")
    if not isinstance(raw_paths, dict):
        raise RuntimeError("发行清单缺少 paths 对象")
    livekit = manifest.get("livekit")
    if not isinstance(livekit, dict) or (
        livekit.get("owner") != "desktop-managed-with-external-override"
        or livekit.get("healthcheck_required") is not True
        or livekit.get("development_defaults_allowed") is not False
    ):
        raise RuntimeError("新发行清单必须声明 Desktop 管理本机 LiveKit 且禁止开发凭据")
    resolved_paths: dict[str, Path] = {}
    for key in _PATH_KEYS:
        value = raw_paths.get(key)
        if not isinstance(value, str):
            raise RuntimeError(f"发行清单缺少路径字段：{key}")
        resolved_paths[key] = _resolve_runtime_path(
            runtime,
            value,
            field=f"paths.{key}",
        )

    raw_records = manifest.get("files")
    if not isinstance(raw_records, list) or not raw_records:
        raise RuntimeError("发行清单缺少非空 files 摘要")
    records: dict[str, tuple[int, str]] = {}
    for record in raw_records:
        if not isinstance(record, dict):
            raise RuntimeError("发行清单 files 包含非对象记录")
        relative_path = record.get("path")
        size = record.get("size")
        sha256 = record.get("sha256")
        if (
            not isinstance(relative_path, str)
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or not isinstance(sha256, str)
            or re.fullmatch(r"[0-9a-fA-F]{64}", sha256) is None
        ):
            raise RuntimeError("发行清单 files 记录格式无效")
        _validate_relative_runtime_path(relative_path, field="files")
        canonical_path = relative_path.casefold()
        if canonical_path in records:
            raise RuntimeError(
                f"发行清单 files 存在重复路径：{relative_path}"
            )
        records[canonical_path] = (size, sha256.lower())

    raw_required_files = manifest.get("required_files")
    if not isinstance(raw_required_files, list) or not raw_required_files:
        raise RuntimeError("发行清单缺少非空 required_files")
    required_files: list[str] = []
    for relative_path in raw_required_files:
        if not isinstance(relative_path, str):
            raise RuntimeError(
                "发行清单 required_files 包含非字符串路径"
            )
        _validate_relative_runtime_path(relative_path, field="required_files")
        required_files.append(relative_path)

    path_prefixes = {
        key: PurePosixPath(str(raw_paths[key]))
        for key in _PATH_KEYS
    }
    python_path = path_prefixes["python"]
    if len(python_path.parts) < 3:
        raise RuntimeError("发行清单 paths.python 必须位于 Python/Scripts 之下")
    python_root = PurePosixPath(*python_path.parts[:-2])
    expected_required_files = {
        *_FIXED_RUNTIME_FILES,
        str(path_prefixes["backend"] / "config" / "env.yaml"),
        str(path_prefixes["backend"] / "config" / "realtime_audio.yaml"),
        str(path_prefixes["shared"] / "default-settings.json"),
        str(path_prefixes["shared"] / "control-events.json"),
        str(path_prefixes["shared"] / "dev-ports.json"),
        str(path_prefixes["console"] / "index.html"),
        str(path_prefixes["model_catalog"]),
        str(path_prefixes["python"]),
        str(path_prefixes["native"] / "llama.dll"),
        str(path_prefixes["native"] / "ggml.dll"),
        str(path_prefixes["native"] / "ggml-base.dll"),
        str(path_prefixes["native"] / ("ggml-cuda.dll" if profile == "cuda" else "ggml-vulkan.dll")),
        str(python_root / "Lib" / "site-packages" / "companion" / "core" / "main.py"),
        str(python_root / "Lib" / "site-packages" / "audio_aec_sidecar" / "main.py"),
        str(python_root / "Lib" / "site-packages" / "audio_aec_poc" / "main.py"),
        str(path_prefixes["seed_character_cards"] / "huohua" / "card.yaml"),
        str(path_prefixes["assets"] / "icon.png"),
        str(path_prefixes["livekit_server"]),
        str(path_prefixes["livekit_server"].parent / "LICENSE"),
    }
    missing_contract_files = sorted(expected_required_files - set(required_files))
    if missing_contract_files:
        raise RuntimeError(
            "发行清单未把必需运行入口列入 required_files："
            + ", ".join(missing_contract_files)
        )

    for relative_path in required_files:
        record = records.get(relative_path.casefold())
        if record is None:
            raise RuntimeError(
                f"必需文件未登记 SHA256 摘要：{relative_path}"
            )
        file_path = _resolve_runtime_path(
            runtime,
            relative_path,
            field="required_files",
        )
        if not file_path.is_file():
            raise RuntimeError(f"发行必需文件不存在：{file_path}")
        _assert_no_reparse_points(runtime, file_path)
        expected_size, expected_sha256 = record
        if file_path.stat().st_size != expected_size:
            raise RuntimeError(f"发行必需文件大小不匹配：{file_path}")
        if _sha256_file(file_path) != expected_sha256:
            raise RuntimeError(f"发行必需文件 SHA256 不匹配：{file_path}")

    tts_models = manifest.get("tts_models")
    if not isinstance(tts_models, list):
        raise RuntimeError("发行清单缺少 tts_models")
    manifest_model_ids = {
        item.get("model")
        for item in tts_models
        if isinstance(item, dict) and isinstance(item.get("model"), str)
    }
    if (
        manifest_model_ids != _EXPECTED_TTS_MODELS
        or len(tts_models) != len(_EXPECTED_TTS_MODELS)
    ):
        raise RuntimeError("发行清单的五个 TTS 模型与产品契约不一致")

    directory_keys = (
        "backend",
        "native",
        "shared",
        "console",
        "aec_package",
        "seed_character_cards",
        "assets",
    )
    for key in directory_keys:
        if not resolved_paths[key].is_dir():
            raise RuntimeError(f"发行布局目录不存在：{resolved_paths[key]}")
        _assert_no_reparse_points(runtime, resolved_paths[key])
    if not resolved_paths["model_catalog"].is_file():
        raise RuntimeError(
            f"发行模型清单不存在：{resolved_paths['model_catalog']}"
        )

    return RuntimeContract(
        runtime=runtime,
        manifest=manifest,
        profile=str(profile),
        python_path=resolved_paths["python"],
        backend_root=resolved_paths["backend"],
        native_root=resolved_paths["native"],
        shared_root=resolved_paths["shared"],
        console_root=resolved_paths["console"],
        model_catalog_path=resolved_paths["model_catalog"],
        livekit_server_path=resolved_paths["livekit_server"],
    )


def validate_release_configuration(configuration: object) -> None:
    """确认安装包默认配置不携带开发凭据或诊断开关。

    Args:
        configuration: 从发行 env.yaml 读取的对象。

    Raises:
        RuntimeError: 配置不是对象、携带 LiveKit 默认值或打开开发诊断时抛出。
    """

    if not isinstance(configuration, dict):
        raise RuntimeError("发行 env.yaml 必须是对象")
    livekit_values = (
        configuration.get("livekit_url"),
        configuration.get("livekit_api_key"),
        configuration.get("livekit_api_secret"),
    )
    if any(
        not isinstance(value, str) or value.strip()
        for value in livekit_values
    ):
        raise RuntimeError("发行 env.yaml 不得携带 LiveKit 开发地址或凭据")
    prompt_audit = configuration.get("prompt_audit", {})
    if isinstance(prompt_audit, dict) and prompt_audit.get("enabled") is True:
        raise RuntimeError("发行 env.yaml 不得默认启用 Prompt Audit")
    if configuration.get("background_agent_terminal_diagnostics_enabled") is True:
        raise RuntimeError(
            "发行 env.yaml 不得默认启用 Background 终端诊断"
        )


def build_packaged_environment(
    contract: RuntimeContract,
    *,
    work: Path,
    config_path: Path,
    control_url: str,
    instance_id: str,
    host_environment: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """构建不继承构建机密钥和 Python/CUDA 路径的 smoke 环境。

    Args:
        contract: 已验证的运行布局。
        work: 本轮 smoke 专用可写目录。
        config_path: 已生成的临时发行配置。
        control_url: 本轮随机控制端口根地址。
        instance_id: 本轮 Backend 唯一身份。
        host_environment: 只用于获取 Windows 系统目录和系统盘的宿主环境。

    Returns:
        可直接传给 Python 子进程的白名单环境。
    """

    host = host_environment or os.environ
    windows_host = {key.lower(): value for key, value in host.items()}
    system_root = Path(
        windows_host.get("systemroot", windows_host.get("windir", r"C:\Windows"))
    )
    # 与 Desktop 一样补齐系统盘，避免原生组件把 %SystemDrive% 缓存写进 staging。
    system_drive = windows_host.get("systemdrive") or PureWindowsPath(system_root).drive
    system_directory = system_root / "System32"
    user_data_root = work / "NearMate"
    path_entries = [
        contract.python_path.parent,
        contract.native_root,
        contract.runtime / "tools" / "claude",
        contract.runtime / "tools" / "cua",
        contract.runtime / "tools" / "git" / "bin",
        contract.runtime / "tools" / "ffmpeg" / "bin",
        system_directory,
        system_root,
        system_directory / "Wbem",
        system_directory / "WindowsPowerShell" / "v1.0",
    ]
    return {
        "SystemRoot": str(system_root),
        "SystemDrive": system_drive,
        "WINDIR": str(system_root),
        "USERPROFILE": str(work),
        "APPDATA": str(work / "Roaming"),
        "LOCALAPPDATA": str(work / "Local"),
        "TEMP": str(work / "Temp"),
        "TMP": str(work / "Temp"),
        "PATH": os.pathsep.join(str(path) for path in path_entries),
        "NEARMATE_PACKAGED": "1",
        "NEARMATE_RESOURCE_ROOT": str(contract.runtime.parent),
        "NEARMATE_RUNTIME_ROOT": str(contract.runtime),
        "NEARMATE_DATA_ROOT": str(user_data_root),
        "NEARMATE_RUNTIME_MANIFEST": str(contract.runtime / "runtime-manifest.json"),
        "NEARMATE_RUNTIME_PROFILE": contract.profile,
        "NEARMATE_RUNTIME_INSTANCE": instance_id,
        "NEARMATE_ENV_PATH": str(config_path),
        "STREAMVOX_HOME": str(user_data_root / "streamvox"),
        "COMPANION_CONTROL_HTTP_URL": control_url,
        "PYTHONUTF8": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "CLAUDE_CODE_GIT_BASH_PATH": str(
            contract.runtime / "tools" / "git" / "bin" / "bash.exe"
        ),
    }


def validate_backend_snapshot(
    contract: RuntimeContract,
    *,
    expected_instance_id: str,
    health: object,
    bootstrap_status: object,
    console_body: bytes,
) -> None:
    """校验空白首次初始化中的 Backend 身份、Bootstrap 和 Console。

    Args:
        contract: 已验证的发行合同。
        expected_instance_id: 本轮 smoke 注入的 Backend 身份。
        health: ``/healthz`` 响应对象。
        bootstrap_status: ``/api/bootstrap/status`` 响应对象。
        console_body: ``/console`` 返回的原始字节。

    Raises:
        RuntimeError: 任一对外运行契约与当前发行不一致时抛出。
    """

    if not isinstance(health, dict) or (
        health.get("status") != "ok"
        or health.get("instance_id") != expected_instance_id
    ):
        raise RuntimeError("安装态 Backend 健康或运行身份不符合预期")
    if (
        not isinstance(bootstrap_status, dict)
        or bootstrap_status.get("is_initialized") is not False
    ):
        raise RuntimeError("安装 smoke 没有使用隔离的空白 Bootstrap 数据")

    if b"<html" not in console_body.lower():
        raise RuntimeError("安装态 Console 没有返回 HTML")


def validate_runtime_probe(contract: RuntimeContract, probe: object) -> None:
    """校验随包 Python 的发行合同、输入模型执行和可选预热返回证据。

    Args:
        contract: 已验证的发行合同。
        probe: ``python -I`` 子进程输出的结构化探测结果。

    Returns:
        无；profile、真实输入模型和 ASR 执行证据全部匹配时正常返回。

    Raises:
        RuntimeError: 发行合同漂移，或没有完成唤醒与声纹模型推理时抛出。
    """

    expected_profile = _PROFILE_EXPECTATIONS[contract.profile]
    if not isinstance(probe, dict):
        raise RuntimeError("安装态 Python 运行时探测结果无效")
    if probe.get("profile") != contract.profile or any(
        probe.get(field) != expected_profile[field]
        for field in (
            "asr_onnx_provider",
            "streamvox_onnx_provider",
            "llama_backend",
        )
    ):
        raise RuntimeError("安装态 Python Runtime Profile 与发行清单不一致")
    available_providers = probe.get("available_onnx_providers")
    if (
        not isinstance(available_providers, list)
        or expected_profile["available_onnx_provider"] not in available_providers
    ):
        raise RuntimeError("安装态 Python ONNX Runtime 未提供当前 profile 所需 provider")
    model_ids = probe.get("model_ids")
    declared_model_ids = probe.get("declared_model_ids")
    if (
        not isinstance(model_ids, list)
        or not isinstance(declared_model_ids, list)
        or set(model_ids) != _EXPECTED_TTS_MODELS
        or set(declared_model_ids) != _EXPECTED_TTS_MODELS
        or model_ids != sorted(model_ids)
        or declared_model_ids != sorted(declared_model_ids)
    ):
        raise RuntimeError("安装态 Python TTS 模型常量或清单不是完整五模型契约")
    if probe.get("missing_realtime_assets") != []:
        raise RuntimeError(
            "安装态 Python 实时语音资产缺失："
            + json.dumps(probe.get("missing_realtime_assets"), ensure_ascii=False)
        )
    native_audio = probe.get("native_audio")
    if not isinstance(native_audio, dict) or any(
        type(native_audio.get(field)) is not int or native_audio[field] <= 0
        for field in ("wakeword_decoded_frames", "speaker_embedding_size")
    ) or native_audio.get("speaker_embedding_finite") is not True:
        raise RuntimeError("安装态 Python 缺少有效的输入音频模型执行证据")
    asr = native_audio.get("asr")
    if (
        not isinstance(asr, dict)
        or not isinstance(asr.get("transcript"), str) or not asr["transcript"].strip()
        or asr.get("llama_backend") != expected_profile["llama_backend"]
        or any(
            not isinstance(asr.get(field), list)
            or expected_profile["available_onnx_provider"] not in asr[field]
            for field in ("frontend_providers", "backend_providers")
        )
    ):
        raise RuntimeError("安装态 Python 缺少当前 profile 的真实 ASR 转写及后端证据")
    if probe.get("application_catalog_prewarm_returned") is not True:
        raise RuntimeError("安装态 Python 缺少应用目录预热返回证据")


def validate_livekit_probe(probe: object) -> None:
    """要求媒体 smoke 证明真实音频可传递，并已结束自己创建的服务。

    Args:
        probe: 独立 LiveKit smoke 输出的去凭据结果。

    Returns:
        无；完整证据校验通过时正常返回。

    Raises:
        RuntimeError: 只有 HTTP 健康、没有音频或遗留进程时抛出。
    """
    if not isinstance(probe, dict) or (
        probe.get("authenticated_readiness") is not True
        or probe.get("participants") != 2
        or probe.get("owned_process_stopped") is not True
        or any(
            type(probe.get(field)) is not int or probe[field] <= 0
            for field in ("received_frames", "received_nonzero_samples")
        )
    ):
        raise RuntimeError("安装态 LiveKit 缺少真实音频收发或进程退出证据")


def verify_startup(runtime: Path, work: Path) -> None:
    """验证随包 Backend、输入模型与真实本机媒体收发，并完整收口。

    Args:
        runtime: 解包后的 runtime 目录。
        work: 本次验证专用的可写用户目录和日志目录。

    Raises:
        RuntimeError: 清单、资产、进程、接口或退出状态不符合安装契约时抛出。
    """

    contract = load_runtime_contract(runtime)
    work = work.expanduser().resolve()
    writable_paths = (
        work,
        work / "Roaming",
        work / "Local",
        work / "Temp",
        work / "NearMate",
    )
    for path in writable_paths:
        path.mkdir(parents=True, exist_ok=True)

    ports_path = contract.shared_root / "dev-ports.json"
    ports = json.loads(ports_path.read_text(encoding="utf-8"))
    address = dict(ports["backend"])
    if address.get("host") not in {"127.0.0.1", "localhost"}:
        raise RuntimeError(
            f"安装 smoke 只允许环回控制地址：{address.get('host')}"
        )
    with socket.socket() as probe:
        probe.bind((address["host"], 0))
        address["port"] = probe.getsockname()[1]

    env_template_path = contract.backend_root / "config" / "env.yaml"
    configuration = yaml.safe_load(
        env_template_path.read_text(encoding="utf-8")
    ) or {}
    validate_release_configuration(configuration)
    configuration.update(
        control_host=address["host"],
        control_port=address["port"],
    )
    config_path = work / "env.yaml"
    config_path.write_text(
        yaml.safe_dump(configuration, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )

    instance_id = str(uuid.uuid4())
    url = f"http://{address['host']}:{address['port']}"
    environment = build_packaged_environment(
        contract,
        work=work,
        config_path=config_path,
        control_url=url,
        instance_id=instance_id,
    )
    runtime_probe = _verify_isolated_runtime(contract, environment)
    ffmpeg_probe = _verify_ffmpeg_runtime(contract, environment, work)
    livekit_probe = _verify_livekit_runtime(contract, environment, work)

    log_path = work / "backend.log"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    child: subprocess.Popen[bytes] | None = None
    startup_error: Exception | None = None
    cleanup_error: RuntimeError | None = None
    snapshot: dict[str, object] | None = None
    log_stream = log_path.open("wb")
    try:
        child = subprocess.Popen(
            [
                str(contract.python_path),
                "-I",
                "-B",
                "-X",
                "utf8",
                "-m",
                "companion.core.main",
            ],
            cwd=contract.backend_root,
            env=environment,
            stdout=log_stream,
            stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        snapshot = _await_backend_readiness(
            child,
            opener=opener,
            base_url=url,
            contract=contract,
            instance_id=instance_id,
            log_path=log_path,
        )
    except Exception as exception:
        startup_error = exception
    finally:
        cleanup_error = _stop_backend_process(child, log_path=log_path)
        log_stream.close()

    if startup_error is not None and cleanup_error is not None:
        raise ExceptionGroup(
            "安装态 Backend 启动和清理同时失败",
            [startup_error, cleanup_error],
        )
    if startup_error is not None:
        raise startup_error
    if cleanup_error is not None:
        raise cleanup_error
    if snapshot is None:
        raise RuntimeError("安装 smoke 未产生 Backend 验收快照")
    snapshot["native_audio"] = runtime_probe["native_audio"]
    snapshot["livekit"] = livekit_probe
    snapshot["ffmpeg"] = ffmpeg_probe
    snapshot["application_catalog_prewarm_returned"] = runtime_probe["application_catalog_prewarm_returned"]
    print(json.dumps(snapshot, ensure_ascii=False))


def _verify_ffmpeg_runtime(
    contract: RuntimeContract,
    environment: dict[str, str],
    work: Path,
) -> dict[str, object]:
    """通过发行 Python 和私有 PATH 检查媒体 Skill 及真实音频处理。

    Args:
        contract: 本次已经验证的发行资源路径。
        environment: 与 Desktop 相同的受限子进程环境。
        work: 本轮 smoke 独占的可写目录。

    Returns:
        包含三个媒体 Skill 就绪状态的去用户数据验收结果。

    Raises:
        RuntimeError: 工具未能执行、超时或没有返回完整检查证据。
    """
    result = subprocess.run(
        [str(contract.python_path), "-I", "-B", "-X", "utf8",
         str(Path(__file__).with_name("verify_ffmpeg_runtime.py")),
         str(contract.runtime), str(work)],
        cwd=contract.backend_root, env=environment, capture_output=True,
        text=True, encoding="utf-8", errors="replace", timeout=45,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode != 0:
        raise RuntimeError(f"安装态 FFmpeg 检查失败：{result.stderr[-2000:]}")
    probe_line = next((line.removeprefix("NEARMATE_FFMPEG_PROBE=") for line in result.stdout.splitlines()
                       if line.startswith("NEARMATE_FFMPEG_PROBE=")), None)
    if probe_line is None:
        raise RuntimeError("安装态 FFmpeg 未返回检查证据")
    probe = json.loads(probe_line)
    if probe.get("ready_skills") != ["bilibili-download", "ffmpeg-convert", "media-transcribe"]:
        raise RuntimeError("安装态 FFmpeg 没有让全部媒体 Skill 依赖就绪")
    return probe


def _verify_livekit_runtime(
    contract: RuntimeContract,
    environment: dict[str, str],
    work: Path,
) -> dict[str, object]:
    """用发行 Python 和媒体程序运行有时间上限的真实 RTC 检查。

    Args:
        contract: 已验证的发行资源路径。
        environment: 已剔除宿主凭据的安装环境。
        work: 本轮 smoke 的日志目录。

    Returns:
        不含凭据、音频内容或用户身份的媒体检查结果。

    Raises:
        RuntimeError: 子进程超时、异常退出或缺少实际收发证据时抛出。
    """
    log_path = work / "livekit-smoke.log"
    with log_path.open("wb") as log_stream:
        child = subprocess.Popen(
            [str(contract.python_path), "-I", "-B", "-X", "utf8",
             str(Path(__file__).with_name("verify_livekit_runtime.py")),
             str(contract.livekit_server_path), str(work / "livekit-server.log")],
            cwd=contract.backend_root, env=environment,
            stdout=log_stream, stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            child.wait(timeout=90)
        except subprocess.TimeoutExpired as exception:
            # Python 或原生 RTC 卡住时，也必须结束其拥有的媒体服务，不能留给下一轮构建。
            cleanup_error = _stop_backend_process(child, log_path=log_path)
            raise RuntimeError(f"安装态 LiveKit 检查超时，日志：{log_path}") from (cleanup_error or exception)
    if child.returncode != 0:
        raise RuntimeError(f"安装态 LiveKit 检查失败（退出码 {child.returncode}），日志：{log_path}")
    output = log_path.read_text(encoding="utf-8", errors="replace")
    line = next((line.removeprefix("NEARMATE_LIVEKIT_PROBE=") for line in output.splitlines()
                 if line.startswith("NEARMATE_LIVEKIT_PROBE=")), None)
    if line is None:
        raise RuntimeError(f"安装态 LiveKit 未返回媒体验收结果，日志：{log_path}")
    try:
        probe = json.loads(line)
    except json.JSONDecodeError as exception:
        raise RuntimeError("安装态 LiveKit 验收结果不是 JSON") from exception
    validate_livekit_probe(probe)
    return probe


def _verify_isolated_runtime(
    contract: RuntimeContract,
    environment: Mapping[str, str],
) -> dict[str, object]:
    """用独立发行 Python 执行模型与预热，原生崩溃和卡死都必须阻断打包。

    Args:
        contract: 已验证的发行合同。
        environment: 与 Desktop 同构的白名单环境。

    Raises:
        RuntimeError: 发行合同、真实音频模型执行失败或子进程超时时抛出。

    Returns:
        通过发行合同与实际模型执行检查的探测结果。
    """

    log_path = Path(environment["NEARMATE_DATA_ROOT"]).parent / "native-audio.log"
    # 使用独立进程和持久日志，C/C++ 崩溃无法被 Python try/except 捕获，也不能让构建误判成功。
    with log_path.open("wb") as log_stream:
        try:
            result = subprocess.run(
                [
                    str(contract.python_path), "-I", "-B", "-X", "utf8",
                    str(Path(__file__).with_name("verify_native_audio_runtime.py")),
                    str(contract.model_catalog_path),
                ],
                cwd=contract.backend_root,
                env=dict(environment),
                stdout=log_stream,
                stderr=subprocess.STDOUT,
                # 完整 ASR 还要读取 F16 权重并预热 GPU，给它有界的独立预算。
                timeout=180,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.TimeoutExpired as exception:
            raise RuntimeError(f"安装态语音与启动预热检查超时，日志：{log_path}") from exception
    output = log_path.read_text(encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise RuntimeError(
            "安装态语音与启动预热检查失败："
            f"退出码 {result.returncode} (0x{result.returncode & 0xFFFFFFFF:08X})，"
            f"日志：{log_path}\n{output[-2000:]}"
        )
    probe_line = next(
        (
            line.removeprefix("NEARMATE_RUNTIME_PROBE=")
            for line in output.splitlines()
            if line.startswith("NEARMATE_RUNTIME_PROBE=")
        ),
        None,
    )
    if probe_line is None:
        raise RuntimeError("安装态 Python -I 未输出 RuntimeLayout 探测结果")
    try:
        probe = json.loads(probe_line)
    except json.JSONDecodeError as exception:
        raise RuntimeError("安装态 Python RuntimeLayout 探测结果不是 JSON") from exception
    validate_runtime_probe(contract, probe)
    return probe


def _await_backend_readiness(
    child: subprocess.Popen[bytes],
    *,
    opener: urllib.request.OpenerDirector,
    base_url: str,
    contract: RuntimeContract,
    instance_id: str,
    log_path: Path,
) -> dict[str, object]:
    """等待本轮 Backend 返回完整安装契约快照。

    Args:
        child: 本轮 smoke 启动的 Backend 子进程。
        opener: 显式禁用宿主代理的 HTTP opener。
        base_url: Backend 控制面根地址。
        contract: 已验证的发行合同。
        instance_id: 本轮期望的 Backend 身份。
        log_path: 启动失败时提供给构建人员的日志路径。

    Returns:
        可直接输出的安装验收摘要。

    Raises:
        RuntimeError: 进程提前退出、超时或对外契约不匹配时抛出。
    """

    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if child.poll() is not None:
            raise RuntimeError(
                f"后端退出码 {child.returncode}，日志：{log_path}"
            )
        try:
            health = _read_json(opener, base_url + "/healthz")
            bootstrap_status = _read_json(
                opener,
                base_url + "/api/bootstrap/status",
            )
            with opener.open(base_url + "/console", timeout=2) as response:
                console_body = response.read()
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            time.sleep(0.5)
            continue

        validate_backend_snapshot(
            contract,
            expected_instance_id=instance_id,
            health=health,
            bootstrap_status=bootstrap_status,
            console_body=console_body,
        )
        return {
            "profile": contract.profile,
            "healthz": "ok",
            "bootstrap": "isolated",
            "runtime_profile": "ok",
            "tts_catalog": "ok",
            "console": "ok",
            "is_initialized": False,
            "log": str(log_path),
        }
    raise RuntimeError(f"后端启动超时，日志：{log_path}")


def _read_json(
    opener: urllib.request.OpenerDirector,
    url: str,
) -> object:
    """读取 Backend JSON 接口并在两秒内收口。

    Args:
        opener: 不使用宿主代理的 HTTP opener。
        url: 本机 Backend 接口地址。

    Returns:
        解析后的 JSON 值。
    """

    with opener.open(url, timeout=2) as response:
        return json.load(response)


def _stop_backend_process(
    child: subprocess.Popen[bytes] | None,
    *,
    log_path: Path,
) -> RuntimeError | None:
    """结束 smoke Backend 进程树并等待句柄真正退出。

    Args:
        child: 本轮 Backend 子进程；启动前失败时可为 None。
        log_path: 清理失败诊断中展示的日志路径。

    Returns:
        清理成功返回 None；进程树仍未退出时返回错误。
    """

    if child is None:
        return None
    terminate_result: subprocess.CompletedProcess[str] | None = None
    if child.poll() is None:
        # taskkill 的本地化诊断遵循 Windows OEM 编码，Python 的 UTF-8 模式不能替它决定编码。
        # 诊断中的异常字节只影响可读文本；不能让后台读线程丢异常后仍把清理误报为成功。
        terminate_result = subprocess.run(
            ["taskkill", "/PID", str(child.pid), "/T", "/F"],
            capture_output=True,
            text=True,
            encoding="oem",
            errors="replace",
            check=False,
        )
    try:
        child.wait(timeout=15)
        return None
    except subprocess.TimeoutExpired:
        taskkill_summary = ""
        if terminate_result is not None:
            taskkill_summary = f"；taskkill 退出码 {terminate_result.returncode}"
        return RuntimeError(
            f"smoke Backend 未能退出{taskkill_summary}：{log_path}"
        )


def _resolve_runtime_path(runtime: Path, relative_path: str, *, field: str) -> Path:
    """把已验证的清单相对路径定位到 runtime 内部。

    Args:
        runtime: 发行 runtime 根目录。
        relative_path: 清单中的正斜杠相对路径。
        field: 出错时显示的清单字段。

    Returns:
        不越过 runtime 根目录的本地路径。

    Raises:
        RuntimeError: 路径语法无效或解析后越界时抛出。
    """

    _validate_relative_runtime_path(relative_path, field=field)
    candidate = runtime.joinpath(*PurePosixPath(relative_path).parts)
    try:
        candidate.resolve().relative_to(runtime)
    except ValueError as exception:
        raise RuntimeError(
            f"发行清单路径越过 runtime：{relative_path}"
        ) from exception
    return candidate


def _validate_relative_runtime_path(relative_path: str, *, field: str) -> None:
    """校验与 Desktop RuntimeLayout 一致的 Windows 相对路径语法。

    Args:
        relative_path: 待校验的正斜杠相对路径。
        field: 出错时显示的清单字段。

    Raises:
        RuntimeError: 路径为绝对路径、包含越界段或 Windows 非法名称时抛出。
    """

    segments = relative_path.split("/")
    has_control_character = any(
        ord(character) < 0x20 or ord(character) == 0x7F
        for character in relative_path
    )
    has_reserved_name = any(
        _WINDOWS_RESERVED_NAME.fullmatch(segment.split(".", 1)[0])
        is not None
        for segment in segments
    )
    if (
        not relative_path
        or relative_path.startswith("/")
        or re.match(r"^[A-Za-z]:", relative_path) is not None
        or "\\" in relative_path
        or has_control_character
        or any(segment in {"", ".", ".."} for segment in segments)
        or any(segment.endswith((" ", ".")) for segment in segments)
        or has_reserved_name
    ):
        raise RuntimeError(
            f"{field} 必须是可安全解析的正斜杠相对路径："
            f"{relative_path}"
        )


def _assert_no_reparse_points(runtime: Path, target: Path) -> None:
    """拒绝 runtime 到目标文件之间的符号链接或目录联接。

    Args:
        runtime: 发行资源根目录。
        target: 正在验证的必需文件或目录。

    Raises:
        RuntimeError: 任一已存在路径段是重解析点时抛出。
    """

    relative_parts = target.relative_to(runtime).parts
    current = runtime
    for part in (None, *relative_parts):
        if part is not None:
            current /= part
        if not current.exists() and not current.is_symlink():
            continue
        is_junction = bool(getattr(current, "is_junction", lambda: False)())
        attributes = getattr(os.lstat(current), "st_file_attributes", 0)
        if current.is_symlink() or is_junction or attributes & 0x400:
            raise RuntimeError(
                f"发行必需路径不能经过重解析点：{current}"
            )


def _sha256_file(path: Path) -> str:
    """流式计算文件 SHA256，避免把大文件一次性读入内存。

    Args:
        path: 待校验的普通文件。

    Returns:
        小写十六进制 SHA256 摘要。
    """

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("runtime", type=Path)
    parser.add_argument("work", type=Path)
    arguments = parser.parse_args()
    verify_startup(arguments.runtime, arguments.work)
