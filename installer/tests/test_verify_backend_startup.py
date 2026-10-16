from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import Mock

import pytest

from installer.verify_backend_startup import (
    _stop_backend_process,
    build_packaged_environment,
    load_runtime_contract,
    validate_backend_snapshot,
    validate_runtime_probe,
    validate_release_configuration,
    validate_livekit_probe,
)


@pytest.mark.skipif(os.name != "nt", reason="taskkill 使用 Windows OEM 编码")
def test_backend_cleanup_handles_native_diagnostics_in_utf8_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """原生命令诊断不能因 Python UTF-8 模式丢失，也不能让读线程异常冒充清理成功。

    Args:
        tmp_path: Pytest 提供的临时日志目录。
        monkeypatch: 把 taskkill 替换为只输出测试字节的自持 Python 程序。

    Returns:
        无；断言操作系统编码输出可读，损坏字节可替换，子进程清理结果仍可确认。
    """
    child = Mock(pid=12345)
    child.poll.return_value = None
    child.wait.return_value = 0
    # 这些字节在中文 OEM 936 下是“成功”，在其他 OEM 页下也必须按本机编码读出。
    # 不直接编码中文字符串，避免英文 Windows 的 OEM 437 无法表示中文。
    raw_diagnostic = bytes.fromhex("b3c9b9a6")
    real_run = subprocess.run
    captured_results = []

    def run_diagnostic_fixture(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        """运行字节输出 fixture；不执行 taskkill，也不操作任何实际系统进程。"""
        assert command == ["taskkill", "/PID", "12345", "/T", "/F"]
        result = real_run(
            [sys.executable, "-I", "-B", "-c",
             "import sys; sys.stdout.buffer.write(" + repr(raw_diagnostic) + "); "
             "sys.stderr.buffer.write(bytes([255]))"],
            **kwargs,
        )
        captured_results.append(result)
        return result

    monkeypatch.setattr(subprocess, "run", run_diagnostic_fixture)

    assert _stop_backend_process(child, log_path=tmp_path / "backend.log") is None
    assert captured_results[0].stdout == raw_diagnostic.decode("oem")
    assert captured_results[0].stderr == bytes([255]).decode("oem", errors="replace")
    child.wait.assert_called_once_with(timeout=15)


def _write_runtime(tmp_path: Path, *, schema_version: int = 3) -> Path:
    """写入安装 smoke 合同测试使用的最小 runtime。

    Args:
        tmp_path: Pytest 提供的临时目录。
        schema_version: 希望写入清单的 schema 版本。

    Returns:
        已写入 runtime-manifest.json 的运行时根目录。
    """

    runtime = tmp_path / "runtime"
    profile = "cuda"
    file_contents = {
        "python/Scripts/python.exe": b"python",
        "backend/config/env.yaml": b"livekit_url: ''\nlivekit_api_key: ''\nlivekit_api_secret: ''\n",
        "backend/config/realtime_audio.yaml": b"realtime_input_only_mode: false\n",
        "native/llama.dll": b"llama",
        "native/ggml-cuda.dll": b"cuda",
        "native/ggml.dll": b"ggml",
        "native/ggml-base.dll": b"ggml-base",
        "tools/claude/claude.exe": b"claude",
        "tools/cua/cua-driver.exe": b"cua",
        "tools/cua/cua-driver-uia.exe": b"uia",
        "tools/git/bin/bash.exe": b"bash",
        "tools/git/cmd/git.exe": b"git",
        "tools/ffmpeg/bin/ffmpeg.exe": b"ffmpeg",
        "tools/ffmpeg/bin/ffprobe.exe": b"ffprobe",
        "tools/ffmpeg/LICENSE": b"license",
        "tools/ffmpeg/README.txt": b"source and build configuration",
        "tools/livekit/livekit-server.exe": b"livekit",
        "tools/livekit/LICENSE": b"license",
        "shared/dev-ports.json": b'{"backend":{"host":"127.0.0.1","port":8765}}',
        "shared/default-settings.json": b"{}",
        "shared/control-events.json": b"{}",
        "console/dist/index.html": b"<html></html>",
        "model-catalog.json": b"{}",
        "backend/seed-character-cards/huohua/card.yaml": b"meta: {}",
        "assets/icon.png": b"png",
        "python/Lib/site-packages/companion/core/main.py": b"source",
        "python/Lib/site-packages/audio_aec_sidecar/main.py": b"source",
        "python/Lib/site-packages/audio_aec_poc/main.py": b"source",
    }
    for relative_path, content in file_contents.items():
        target = runtime / Path(relative_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)

    records = [
        {
            "path": relative_path,
            "size": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }
        for relative_path, content in file_contents.items()
    ]
    manifest = {
        "schema_version": schema_version,
        "product": "NearMate",
        "platform": "windows-x64",
        "python_version": "3.12.13",
        "profile": profile,
        "paths": {
            "python": "python/Scripts/python.exe",
            "backend": "backend",
            "native": "native",
            "shared": "shared",
            "console": "console/dist",
            "aec_package": "python/Lib/site-packages/audio_aec_sidecar",
            "model_catalog": "model-catalog.json",
            "seed_character_cards": "backend/seed-character-cards",
            "assets": "assets",
            "livekit_server": "tools/livekit/livekit-server.exe",
        },
        "livekit": {
            "owner": "desktop-managed-with-external-override",
            "healthcheck_required": True,
            "development_defaults_allowed": False,
        },
        "required_files": list(file_contents),
        "files": records,
        "tts_models": [
            {"model": "qwen3-tts-clone-0.6b-gguf"},
            {"model": "qwen3-tts-clone-1.7b-gguf"},
            {"model": "s2-pro-4b-gguf"},
            {"model": "voxcpm2-gguf"},
            {"model": "moss-tts-nano-onnx"},
        ],
    }
    (runtime / "runtime-manifest.json").write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )
    return runtime


def test_load_runtime_contract_accepts_v3_and_verifies_required_digests(
    tmp_path: Path,
) -> None:
    """合法 v3 清单应生成唯一路径快照并验证必需文件摘要。"""

    runtime = _write_runtime(tmp_path)

    contract = load_runtime_contract(runtime)

    assert contract.profile == "cuda"
    assert contract.python_path == runtime / "python" / "Scripts" / "python.exe"
    assert contract.native_root == runtime / "native"
    assert contract.livekit_server_path == runtime / "tools/livekit/livekit-server.exe"


@pytest.mark.parametrize("missing_path", [
    "tools/livekit/livekit-server.exe", "tools/livekit/LICENSE",
])
def test_release_contract_requires_bundled_livekit(tmp_path: Path, missing_path: str) -> None:
    """新发行必须随包带齐媒体程序和许可，不能继续发布依赖空白外部配置的包。"""
    runtime = _write_runtime(tmp_path)
    manifest_path = runtime / "runtime-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["required_files"].remove(missing_path)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(RuntimeError, match="必需运行入口"):
        load_runtime_contract(runtime)


def test_release_contract_rejects_external_only_livekit_ownership(tmp_path: Path) -> None:
    """构建入口必须拒绝只靠外部服务的过期清单，旧安装读取兼容由 Desktop 负责。"""
    runtime = _write_runtime(tmp_path)
    manifest_path = runtime / "runtime-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["livekit"]["owner"] = "external-runtime"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(RuntimeError, match="LiveKit"):
        load_runtime_contract(runtime)


@pytest.mark.parametrize("missing_path", [
    "tools/ffmpeg/bin/ffmpeg.exe", "tools/ffmpeg/bin/ffprobe.exe",
    "tools/ffmpeg/LICENSE", "tools/ffmpeg/README.txt",
])
def test_release_contract_requires_bundled_ffmpeg(tmp_path: Path, missing_path: str) -> None:
    """媒体能力的程序与许可必须进入安装校验，不能借用开发机已安装的工具。"""
    runtime = _write_runtime(tmp_path)
    manifest_path = runtime / "runtime-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["required_files"].remove(missing_path)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(RuntimeError, match="必需运行入口"):
        load_runtime_contract(runtime)


def test_packaged_environment_includes_private_ffmpeg_directory(tmp_path: Path) -> None:
    """真实启动 smoke 与 Desktop 必须使用同一私有 PATH，系统无 FFmpeg 也能运行媒体 Skill。"""
    contract = load_runtime_contract(_write_runtime(tmp_path))
    work = tmp_path / "work"
    environment = build_packaged_environment(
        contract, work=work, config_path=work / "env.yaml",
        control_url="http://127.0.0.1:9123", instance_id="smoke-instance",
        host_environment={"SystemRoot": r"C:\Windows", "PATH": r"C:\host-ffmpeg"},
    )

    assert str(contract.runtime / "tools" / "ffmpeg" / "bin") in environment["PATH"]
    assert "host-ffmpeg" not in environment["PATH"]


@pytest.mark.parametrize("host_environment, expected_system_drive", [
    ({"SystemRoot": r"D:\Windows", "SystemDrive": "D:", "ANTHROPIC_API_KEY": "fixture"}, "D:"),
    ({"sYsTeMrOoT": r"E:\Windows", "sYsTeMdRiVe": "E:"}, "E:"),
    ({"SystemRoot": r"F:\Windows"}, "F:"),
])
def test_packaged_environment_includes_system_drive(
    tmp_path: Path, host_environment: dict[str, str], expected_system_drive: str,
) -> None:
    """smoke 必须和 Desktop 一样保留系统盘，遗漏时从系统目录补齐。

    Args:
        tmp_path: Pytest 提供的隔离运行资源目录。
        host_environment: 不同大小写或缺少系统盘字段的测试环境。
        expected_system_drive: Windows 原生组件应收到的盘符。

    Returns:
        无；断言系统盘完整且测试密钥没有被继承。
    """
    contract = load_runtime_contract(_write_runtime(tmp_path))
    work = tmp_path / "work"
    environment = build_packaged_environment(
        contract, work=work, config_path=work / "env.yaml",
        control_url="http://127.0.0.1:9123", instance_id="smoke-instance",
        host_environment=host_environment,
    )

    assert environment["SystemDrive"] == expected_system_drive
    assert "ANTHROPIC_API_KEY" not in environment


@pytest.mark.skipif(os.name != "nt", reason="Windows 系统盘展开使用原生 API")
def test_packaged_environment_expands_native_system_drive_cache_path(tmp_path: Path) -> None:
    """用真实 Windows API 确认系统缓存路径不会落成工作目录下的字面量。

    Args:
        tmp_path: Pytest 提供的隔离运行资源目录。

    Returns:
        无；仅断言原生路径展开结果，不创建缓存或输出真实环境路径。
    """
    contract = load_runtime_contract(_write_runtime(tmp_path))
    work = tmp_path / "work"
    environment = build_packaged_environment(
        contract, work=work, config_path=work / "env.yaml",
        control_url="http://127.0.0.1:9123", instance_id="smoke-instance",
    )
    # 系统组件正是按这个模板展开缓存目录；这里只展开字符串，不加载 GPU 或写文件。
    probe = """
import ctypes
import json
from pathlib import Path

kernel = ctypes.WinDLL("kernel32", use_last_error=True)
kernel.ExpandEnvironmentStringsW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
kernel.ExpandEnvironmentStringsW.restype = ctypes.c_uint32
output = ctypes.create_unicode_buffer(32768)
size = kernel.ExpandEnvironmentStringsW(
    r"%SystemDrive%\\ProgramData\\Microsoft\\Windows\\Caches", output, len(output),
)
print(json.dumps({
    "expanded": bool(size) and "%SystemDrive%" not in output.value,
    "absolute_path": Path(output.value).is_absolute(),
}))
"""
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", probe],
        env=environment, capture_output=True, text=True, check=True, timeout=10,
    )

    assert json.loads(result.stdout) == {"expanded": True, "absolute_path": True}


@pytest.mark.parametrize("probe", [
    {}, {"authenticated_readiness": True},
    {"authenticated_readiness": True, "participants": 2, "received_frames": 0,
     "received_nonzero_samples": 0, "owned_process_stopped": True},
    {"authenticated_readiness": True, "participants": 2, "received_frames": 3,
     "received_nonzero_samples": 20, "owned_process_stopped": False},
])
def test_livekit_probe_rejects_http_only_or_unclosed_runtime(probe: object) -> None:
    """HTTP 正常不能代替真实音频收发，遗留媒体子进程也不能通过构建验收。"""
    with pytest.raises(RuntimeError, match="LiveKit"):
        validate_livekit_probe(probe)


def test_livekit_probe_accepts_real_media_and_owned_process_cleanup() -> None:
    """只有完整入房、非空音频接收和进程退出证据才满足媒体验收合同。"""
    validate_livekit_probe({
        "authenticated_readiness": True, "participants": 2, "received_frames": 3,
        "received_nonzero_samples": 20, "owned_process_stopped": True,
    })


def test_load_runtime_contract_rejects_old_schema(tmp_path: Path) -> None:
    """安装 smoke 不得再接受已删除的旧清单协议。"""

    runtime = _write_runtime(tmp_path, schema_version=2)

    with pytest.raises(RuntimeError, match="schema v3"):
        load_runtime_contract(runtime)


def test_load_runtime_contract_rejects_tampered_required_file(tmp_path: Path) -> None:
    """必需工具被篡改时必须在启动 Backend 前失败。"""

    runtime = _write_runtime(tmp_path)
    (runtime / "tools" / "claude" / "claude.exe").write_bytes(b"broken")

    with pytest.raises(RuntimeError, match="SHA256"):
        load_runtime_contract(runtime)


def test_release_configuration_rejects_development_livekit_credentials() -> None:
    """发行默认配置不得携带仓库内的 LiveKit 开发密钥。"""

    with pytest.raises(RuntimeError, match="LiveKit"):
        validate_release_configuration(
            {
                "livekit_url": "ws://127.0.0.1:7880",
                "livekit_api_key": "devkey",
                "livekit_api_secret": "devsecret",
            }
        )


def test_packaged_environment_does_not_inherit_build_machine_livekit(
    tmp_path: Path,
) -> None:
    """smoke 应使用空白发行配置，不得借用构建机 LiveKit 凭据。"""

    contract = load_runtime_contract(_write_runtime(tmp_path))
    work = tmp_path / "work"
    environment = build_packaged_environment(
        contract,
        work=work,
        config_path=work / "env.yaml",
        control_url="http://127.0.0.1:9123",
        instance_id="smoke-instance",
        host_environment={
            "SystemRoot": r"C:\Windows",
            "LIVEKIT_URL": "wss://build-machine.invalid",
            "LIVEKIT_API_KEY": "build-key",
            "LIVEKIT_API_SECRET": "build-secret",
        },
    )

    assert "LIVEKIT_URL" not in environment
    assert "LIVEKIT_API_KEY" not in environment
    assert "LIVEKIT_API_SECRET" not in environment
    assert environment["NEARMATE_RUNTIME_INSTANCE"] == "smoke-instance"


def test_backend_snapshot_checks_isolated_bootstrap_contract(tmp_path: Path) -> None:
    """空白配置 smoke 只访问健康、Bootstrap 状态和 Console 入口。"""

    contract = load_runtime_contract(_write_runtime(tmp_path))

    validate_backend_snapshot(
        contract,
        expected_instance_id="smoke-instance",
        health={"status": "ok", "instance_id": "smoke-instance"},
        bootstrap_status={"is_initialized": False},
        console_body=b"<!doctype html><html></html>",
    )


def test_runtime_probe_requires_profile_provider_and_five_models(tmp_path: Path) -> None:
    """Python 运行时探测必须覆盖 profile、ONNX provider 和五个固定模型。"""

    contract = load_runtime_contract(_write_runtime(tmp_path))
    probe = {
        "profile": "cuda",
        "asr_onnx_provider": "cuda",
        "streamvox_onnx_provider": "cuda",
        "llama_backend": "cuda",
        "available_onnx_providers": ["CPUExecutionProvider", "CUDAExecutionProvider"],
        "model_ids": sorted({
            "qwen3-tts-clone-0.6b-gguf",
            "qwen3-tts-clone-1.7b-gguf",
            "s2-pro-4b-gguf",
            "voxcpm2-gguf",
            "moss-tts-nano-onnx",
        }),
        "declared_model_ids": sorted({
            "qwen3-tts-clone-0.6b-gguf",
            "qwen3-tts-clone-1.7b-gguf",
            "s2-pro-4b-gguf",
            "voxcpm2-gguf",
            "moss-tts-nano-onnx",
        }),
        "missing_realtime_assets": [],
        "application_catalog_prewarm_returned": True,
        "native_audio": {
            "wakeword_decoded_frames": 1,
            "speaker_embedding_size": 192,
            "speaker_embedding_finite": True,
            "asr": {
                "transcript": "真实录音转写",
                "llama_backend": "cuda",
                "frontend_providers": ["CUDAExecutionProvider", "CPUExecutionProvider"],
                "backend_providers": ["CUDAExecutionProvider", "CPUExecutionProvider"],
            },
        },
    }
    validate_runtime_probe(contract, probe)

    probe["application_catalog_prewarm_returned"] = False
    with pytest.raises(RuntimeError, match="应用目录"):
        validate_runtime_probe(contract, probe)
    probe["application_catalog_prewarm_returned"] = True

    for field, invalid_value in (
        ("transcript", ""), ("llama_backend", "cpu"),
        ("frontend_providers", ["CPUExecutionProvider"]), ("backend_providers", []),
    ):
        valid_value = probe["native_audio"]["asr"][field]
        probe["native_audio"]["asr"][field] = invalid_value
        with pytest.raises(RuntimeError, match="真实 ASR"):
            validate_runtime_probe(contract, probe)
        probe["native_audio"]["asr"][field] = valid_value

    probe["model_ids"] = probe["model_ids"][:-1]
    with pytest.raises(RuntimeError, match="TTS"):
        validate_runtime_probe(contract, probe)


@pytest.mark.parametrize("audio_probe", [None, {}, {
    "wakeword_decoded_frames": 0,
    "speaker_embedding_size": 192,
    "speaker_embedding_finite": True,
}, {
    "wakeword_decoded_frames": 1,
    "speaker_embedding_size": 192,
    "speaker_embedding_finite": False,
}])
def test_runtime_probe_rejects_missing_or_unexecuted_native_audio(tmp_path: Path, audio_probe: object) -> None:
    """导入成功不能代替实际音频模型执行，缺失或无效的执行证据必须阻断构建。"""
    contract = load_runtime_contract(_write_runtime(tmp_path))
    model_ids = sorted(item["model"] for item in contract.manifest["tts_models"])
    probe = {
        "profile": "cuda", "asr_onnx_provider": "cuda",
        "streamvox_onnx_provider": "cuda", "llama_backend": "cuda",
        "available_onnx_providers": ["CPUExecutionProvider", "CUDAExecutionProvider"],
        "model_ids": model_ids, "declared_model_ids": model_ids,
        "missing_realtime_assets": [], "native_audio": audio_probe,
    }
    with pytest.raises(RuntimeError, match="音频模型"):
        validate_runtime_probe(contract, probe)
