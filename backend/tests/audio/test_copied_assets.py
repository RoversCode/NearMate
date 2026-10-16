from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from companion.audio.bootstrap import copied_assets
from companion.audio.bootstrap.asset_registry import (
    TEXT_EOU_MODEL_FILE_NAMES,
    TEXT_EOU_TOKENIZER_RELATIVE_PATH,
)
from companion.audio.bootstrap.qwen_asr_config import (
    QWEN_ENCODER_BACKEND_NAME,
    QWEN_ENCODER_FRONTEND_NAME,
    QWEN_LLM_NAME,
)
from companion.audio.bootstrap.realtime_asr_runtime import SILERO_VAD_NAME
from companion.audio.bootstrap.smart_turn_runtime import SMART_TURN_MODEL_NAME
from companion.audio.bootstrap.wakeword_runtime import (
    DECODER_MODEL_NAME,
    ENCODER_MODEL_NAME,
    JOINER_MODEL_NAME,
)


def _build_asset_paths(tmp_path: Path) -> SimpleNamespace:
    """构造与真实仓库结构一致的临时语音资产目录。"""

    repo_root = tmp_path / "repo"
    ckpt_dir = repo_root / "backend" / "ckpt"
    return SimpleNamespace(
        repo_root=repo_root,
        ckpt_dir=ckpt_dir,
        native_dir=repo_root / "native",
        audio_assets_dir=repo_root / "backend" / "src" / "companion" / "audio" / "assets",
    )


def _minimum_asset_files(paths: SimpleNamespace) -> list[Path]:
    """返回默认首次语音输入链路需要创建的全部文件。"""

    qwen_asr_dir = paths.ckpt_dir / "qwen_asr"
    wakeword_dir = paths.ckpt_dir / "wakeword"
    text_eou_dir = paths.ckpt_dir / "turn_detector"
    return [
        paths.native_dir / "llama.dll",
        paths.native_dir / "ggml.dll",
        paths.native_dir / "ggml-base.dll",
        paths.ckpt_dir / SMART_TURN_MODEL_NAME,
        wakeword_dir / "keywords.txt",
        wakeword_dir / "tokens.txt",
        wakeword_dir / ENCODER_MODEL_NAME,
        wakeword_dir / DECODER_MODEL_NAME,
        wakeword_dir / JOINER_MODEL_NAME,
        qwen_asr_dir / SILERO_VAD_NAME,
        qwen_asr_dir / QWEN_ENCODER_FRONTEND_NAME,
        qwen_asr_dir / QWEN_ENCODER_BACKEND_NAME,
        qwen_asr_dir / QWEN_LLM_NAME,
        text_eou_dir / TEXT_EOU_TOKENIZER_RELATIVE_PATH,
        *(
            text_eou_dir / model_name
            for model_name in TEXT_EOU_MODEL_FILE_NAMES.values()
        ),
    ]


def _touch_all(paths: list[Path]) -> None:
    """创建测试所需的空文件，并自动补齐父目录。"""

    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()


def test_validate_minimum_realtime_assets_accepts_current_epoch13_cpu_assets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """当前运行时使用的 epoch-13 非 int8 唤醒词资产应通过最小校验。"""

    paths = _build_asset_paths(tmp_path)
    _touch_all(_minimum_asset_files(paths))
    monkeypatch.setattr(copied_assets, "resolve_local_audio_asset_paths", lambda: paths)

    assert copied_assets.validate_minimum_realtime_assets(
        llm_backend="cpu",
        llm_use_gpu=False,
    ) == []


def test_validate_minimum_realtime_assets_reports_missing_default_voice_assets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """缺少首次语音链路任一模型时，诊断结果应精确列出对应文件。"""

    paths = _build_asset_paths(tmp_path)
    files = _minimum_asset_files(paths)
    _touch_all(files)
    missing_file = paths.ckpt_dir / "qwen_asr" / QWEN_LLM_NAME
    missing_file.unlink()
    monkeypatch.setattr(copied_assets, "resolve_local_audio_asset_paths", lambda: paths)

    missing_paths = copied_assets.validate_minimum_realtime_assets(
        llm_backend="cpu",
        llm_use_gpu=False,
    )

    assert missing_paths == [str(missing_file)]
