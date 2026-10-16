"""实时音频配置在已确认执行配置下的回归测试。"""

from __future__ import annotations

import json
from pathlib import Path

from companion.config.realtime_audio import load_realtime_audio_config


def test_load_realtime_audio_config_accepts_execution_config_path(tmp_path: Path, monkeypatch) -> None:
    """正式输入运行时必须能沿用语音配置服务确认的执行方式。"""

    execution_path = tmp_path / "execution-config.json"
    execution_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "selection": {"onnx_provider": "directml", "llama_backend": "vulkan"},
                "model_directory": str(tmp_path / "models"),
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.delenv("NEARMATE_PACKAGED", raising=False)
    config = load_realtime_audio_config(
        Path("backend/config/realtime_audio.yaml"),
        execution_config_path=execution_path,
    )

    assert config.vad.provider == "DmlExecutionProvider"
    assert config.asr.engine.llm_backend == "vulkan"
    assert config.speaker_verification.high_similarity == 0.28
    assert config.speaker_verification.low_similarity == 0.20
    assert config.speaker_verification.mean_similarity == 0.28


def test_packaged_universal_migrates_stale_cuda_execution_config(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """通用包读取旧 CUDA 配置时，实时输入必须切换到 DirectML/Vulkan。"""

    execution_path = tmp_path / "execution-config.json"
    execution_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "selection": {"onnx_provider": "cuda", "llama_backend": "cuda"},
                "model_directory": str(tmp_path / "models"),
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("NEARMATE_PACKAGED", "1")
    monkeypatch.setenv("NEARMATE_RUNTIME_PROFILE", "universal")

    config = load_realtime_audio_config(
        Path("backend/config/realtime_audio.yaml"),
        execution_config_path=execution_path,
    )

    assert config.vad.provider == "DmlExecutionProvider"
    assert config.asr.engine.onnx_provider == "dml"
    assert config.asr.engine.llm_backend == "vulkan"
    assert config.smart_turn.provider == "DmlExecutionProvider"
