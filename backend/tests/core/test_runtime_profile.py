from __future__ import annotations

import pytest

from companion.config.realtime_audio import load_realtime_audio_config
from companion.core.runtime_profile import resolve_runtime_profile, runtime_profile_defaults


def test_universal_profile_selects_only_profile_gpu_components(monkeypatch: pytest.MonkeyPatch) -> None:
    """通用安装包只切换 ASR、StreamVox 和 llama 后端，Text EOU 固定 CPU。"""

    monkeypatch.setenv("NEARMATE_RUNTIME_PROFILE", "universal")
    config = load_realtime_audio_config()

    assert config.asr.engine.onnx_provider == "dml"
    assert config.asr.engine.llm_backend == "vulkan"
    assert config.text_eou.provider == "CPUExecutionProvider"
    assert config.wakeword.provider == "cpu"
    assert config.vad.provider == "CPUExecutionProvider"
    assert config.smart_turn.provider == "CPUExecutionProvider"
    assert config.speaker_verification.provider == "cpu"


def test_cuda_profile_selects_cuda_components(monkeypatch: pytest.MonkeyPatch) -> None:
    """CUDA 安装包使用 CUDA ASR 和 llama 后端，Text EOU 固定 CPU。"""

    monkeypatch.setenv("NEARMATE_RUNTIME_PROFILE", "cuda")
    config = load_realtime_audio_config()

    assert config.asr.engine.onnx_provider == "cuda"
    assert config.asr.engine.llm_backend == "cuda"
    assert config.text_eou.provider == "CPUExecutionProvider"
    assert config.wakeword.provider == "cpu"
    assert config.vad.provider == "CPUExecutionProvider"
    assert config.smart_turn.provider == "CPUExecutionProvider"
    assert config.speaker_verification.provider == "cpu"


def test_invalid_profile_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """拼写错误的安装 profile 必须在启动配置阶段直接暴露。"""

    monkeypatch.setenv("NEARMATE_RUNTIME_PROFILE", "cpu")
    with pytest.raises(ValueError, match="NEARMATE_RUNTIME_PROFILE"):
        resolve_runtime_profile()


def test_profile_defaults_are_explicit() -> None:
    """profile 映射同时满足 Qwen ASR 与 StreamVox 的 provider 命名。"""

    universal = runtime_profile_defaults("universal")
    assert universal.asr_onnx_provider == "dml"
    assert universal.streamvox_onnx_provider == "directml"
    assert universal.text_eou_provider == "CPUExecutionProvider"
    assert universal.llama_backend == "vulkan"
