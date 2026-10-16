from __future__ import annotations

import hashlib
import json
from importlib.metadata import version
from pathlib import Path
from types import SimpleNamespace

from companion.core.settings import Settings
from companion.core.execution_config import ExecutionConfiguration, ExecutionSelection
from companion.core.tts_setup_service import TtsSetupService
from companion.core.voice_setup_assets import voice_context


def _settings(tmp_path: Path) -> Settings:
    """为语音配置测试创建隔离的用户目录。"""

    return Settings(
        bootstrap_db_path=str(tmp_path / "bootstrap.sqlite3"),
        user_settings_path=str(tmp_path / "user-settings.json"),
        character_cards_dir=str(tmp_path / "character-cards"),
        streamvox_model_dir=str(tmp_path / "models"),
        voice_setup_path=str(tmp_path / "voice-setup.json"),
        execution_config_path=str(tmp_path / "execution-config.json"),
        model_catalog_path=str(tmp_path / "model-catalog.json"),
    )


def test_tts_setup_catalog_and_unconfigured_status_are_stable(tmp_path: Path) -> None:
    """首次启动没有模型时仍返回固定五项目录和明确未配置状态。"""

    settings = _settings(tmp_path)
    service = TtsSetupService(
        settings=settings,
        active_card_provider=lambda: None,  # type: ignore[return-value]
        save_card=lambda _card: None,
    )
    try:
        catalog = service.catalog_payload()
        status = service.status_payload()
    finally:
        service.shutdown()

    assert catalog["provider"] == "local_streamvox"
    assert {item["sdk_version"] for item in catalog["models"]} == {version("streamvox")}
    assert [item["model_id"] for item in catalog["models"]] == [
        "qwen3-tts-clone-0.6b-gguf",
        "qwen3-tts-clone-1.7b-gguf",
        "s2-pro-4b-gguf",
        "voxcpm2-gguf",
        "moss-tts-nano-onnx",
    ]
    assert {key: status[key] for key in ("provider", "is_ready", "phase", "model_id", "role_name", "error")} == {
        "provider": "local_streamvox",
        "is_ready": False,
        "phase": "unconfigured",
        "model_id": None,
        "role_name": None,
        "error": None,
    }


def test_packaged_profile_migration_keeps_legacy_ready_voice_state(tmp_path: Path, monkeypatch) -> None:
    """通用包升级后，旧 CUDA 指纹不能把已有的 ready 音色误判为待修复。"""

    settings = _settings(tmp_path)
    model_id = "s2-pro-4b-gguf"
    model_root = Path(settings.streamvox_model_dir) / model_id
    model_root.mkdir(parents=True)
    manifest = model_root / "manifest.json"
    manifest.write_text("{}", encoding="utf-8")
    manifest_sha256 = hashlib.sha256(manifest.read_bytes()).hexdigest()
    model = {
        "model_id": model_id,
        "revision": "revision",
        "manifest_sha256": manifest_sha256,
        "files": [{"path": "manifest.json", "size_bytes": manifest.stat().st_size}],
    }
    execution = {
        "schema_version": 1,
        "selection": {"onnx_provider": "cuda", "llama_backend": "cuda"},
        "model_directory": settings.streamvox_model_dir,
    }
    Path(settings.execution_config_path).write_text(json.dumps(execution), encoding="utf-8")
    card_root = Path(settings.character_cards_dir) / "card-1"
    card_root.mkdir(parents=True)
    (card_root / "reference.wav").write_bytes(b"reference")
    binding = SimpleNamespace(
        reference_audio_path="reference.wav",
        prompt_text="你好",
        role_name="huohua",
        seed=42,
        inference_options={},
        streamvox_model=model_id,
        audio_sha256="normalized-audio",
    )
    card = SimpleNamespace(meta=SimpleNamespace(card_id="card-1", voice_binding=binding))
    raw_configuration = ExecutionConfiguration(
        selection=ExecutionSelection(onnx_provider="cuda", llama_backend="cuda"),
        model_directory=settings.streamvox_model_dir,
    )
    raw_context = voice_context(card, Path(settings.character_cards_dir), model, raw_configuration)
    preview = Path(settings.voice_setup_path).parent / "voices" / raw_context["fingerprint"] / "preview.wav"
    preview.parent.mkdir(parents=True)
    preview.write_bytes(b"preview")

    service = TtsSetupService(
        settings=settings,
        active_card_provider=lambda: card,
        save_card=lambda _card: None,
    )
    service._catalog = {model_id: model}
    service._state = {
        "phase": "ready",
        "is_ready": True,
        "model_id": model_id,
        "role_name": "huohua",
        "normalized_audio_sha256": "normalized-audio",
        "preview_sha256": hashlib.sha256(preview.read_bytes()).hexdigest(),
        "context": {"fingerprint": raw_context["fingerprint"]},
        "model_files": [{"path": "manifest.json", "size_bytes": manifest.stat().st_size,
                         "mtime_ns": manifest.stat().st_mtime_ns}],
    }
    monkeypatch.setenv("NEARMATE_PACKAGED", "1")
    monkeypatch.setenv("NEARMATE_RUNTIME_PROFILE", "universal")
    try:
        status = service.status_payload()
        assert status["phase"] == "ready", status
        assert status["is_ready"] is True
    finally:
        service.shutdown()


def test_tts_setup_restores_interrupted_task_as_retryable(tmp_path: Path) -> None:
    """程序在下载中退出后，下一次启动必须显示中断并允许重试。"""

    settings = _settings(tmp_path)
    Path(settings.voice_setup_path).write_text(
        '{"provider":"local_streamvox","is_ready":false,"phase":"download",'
        '"model_id":"qwen3-tts-clone-0.6b-gguf","task_id":"task-1",'
        '"progress":{"phase":"download","completed_bytes":12,"total_bytes":100}}',
        encoding="utf-8",
    )

    service = TtsSetupService(settings=settings, active_card_provider=lambda: None, save_card=lambda _card: None)
    try:
        status = service.status_payload()
        assert status["phase"] == "interrupted"
        assert status["task_id"] == "task-1"
        assert status["can_retry"] is True
        assert status["progress"]["completed_bytes"] == 12
    finally:
        service.shutdown()


def test_legacy_sparse_progress_is_complete_when_restored(tmp_path: Path) -> None:
    """旧版本在校验文件时遗漏的字节字段不能让刷新后的页面再次拒绝状态。"""

    settings = _settings(tmp_path)
    Path(settings.voice_setup_path).write_text(json.dumps({
        'phase': 'failed', 'model_id': 's2-pro-4b-gguf', 'task_id': 'existing-task',
        'error': '语音组件不可用', 'progress': {'phase': 'verify', 'current_file': 'weights.gguf'},
    }), encoding='utf-8')
    service = TtsSetupService(settings=settings, active_card_provider=lambda: None, save_card=lambda _card: None)
    try:
        status = service.status_payload()
        assert status['progress'] == {'phase': 'verify', 'current_file': 'weights.gguf', 'completed_bytes': 0, 'total_bytes': None}
        assert status['error'] == '语音组件不可用'
        assert status['can_retry']
    finally:
        service.shutdown()


def test_legacy_confirmation_requires_current_execution_and_asset_verification(tmp_path: Path) -> None:
    """没有素材与后端指纹的旧试听必须重新准备，不能沿用旧确认资格。"""

    settings = _settings(tmp_path)
    model_id = "qwen3-tts-clone-0.6b-gguf"
    preview_path = Path(settings.streamvox_model_dir) / model_id / ".nearmate-preview.wav"
    preview_path.parent.mkdir(parents=True)
    preview_path.write_bytes(b"RIFF-test")
    Path(settings.voice_setup_path).write_text(
        json.dumps(
            {
                "provider": "local_streamvox",
                "is_ready": False,
                "phase": "awaiting_confirmation",
                "model_id": model_id,
                "task_id": "task-2",
            }
        ),
        encoding="utf-8",
    )

    service = TtsSetupService(settings=settings, active_card_provider=lambda: None, save_card=lambda _card: None)
    try:
        status = service.status_payload()
        assert status["phase"] == "repair_required"
        assert status["can_confirm"] is False
        assert status["can_retry"] is True
        assert "preview_path" not in status
        assert "preview_path" not in json.dumps(status)
    finally:
        service.shutdown()


def test_tts_setup_discards_invalid_persisted_model_id(tmp_path: Path) -> None:
    """持久化文件里的模型编号必须来自固定目录，防止路径穿越读取试听文件。"""

    settings = _settings(tmp_path)
    outside_preview = tmp_path / "outside.wav"
    outside_preview.write_bytes(b"must-not-read")
    Path(settings.voice_setup_path).write_text(
        json.dumps(
            {
                "provider": "local_streamvox",
                "is_ready": False,
                "phase": "awaiting_confirmation",
                "model_id": "..",
                "task_id": "task-3",
            }
        ),
        encoding="utf-8",
    )

    service = TtsSetupService(settings=settings, active_card_provider=lambda: None, save_card=lambda _card: None)
    try:
        assert service.status_payload()["phase"] == "unconfigured"
        assert service.status_payload()["model_id"] is None
    finally:
        service.shutdown()


def test_interrupted_file_download_restores_without_overwriting_voice_state(tmp_path: Path) -> None:
    """重启只把独立文件下载标为中断，保留语音配置文件的原始内容。"""

    settings = _settings(tmp_path)
    voice_state = {'schema_version': 2, 'phase': 'unconfigured', 'is_ready': False}
    Path(settings.voice_setup_path).write_text(json.dumps(voice_state), encoding='utf-8')
    original_voice = Path(settings.voice_setup_path).read_bytes()
    download_path = Path(settings.voice_setup_path).with_name('voice-setup-model-download.json')
    download_path.write_text(json.dumps({'phase': 'download', 'model_id': 's2-pro-4b-gguf',
        'task_id': 'old-download', 'progress': {'phase': 'download', 'completed_bytes': 2, 'total_bytes': 10}}), encoding='utf-8')
    service = TtsSetupService(settings=settings, active_card_provider=lambda: None, save_card=lambda _card: None)
    try:
        status = service.download_status_payload()
        assert status['phase'] == 'interrupted'
        assert status['can_retry']
        assert status['progress']['completed_bytes'] == 2
        assert not status['can_confirm']
        assert Path(settings.voice_setup_path).read_bytes() == original_voice
    finally:
        service.shutdown()
