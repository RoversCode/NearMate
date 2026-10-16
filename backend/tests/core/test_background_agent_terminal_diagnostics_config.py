from __future__ import annotations

from types import SimpleNamespace
from pathlib import Path
import pytest

from companion.core.settings import Settings
from companion.realtime.assistant_process import AssistantProcessConfig


def test_background_terminal_diagnostics_defaults_off_but_development_yaml_enables_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """代码默认关闭诊断，开发 YAML 显式开启后必须传入助手子进程。"""

    # 本用例验证开发配置，不应让发行资源明确关闭诊断的配置改变测试前置条件。
    monkeypatch.setattr('companion.core.settings.ENV_YAML_PATH', Path(__file__).resolve().parents[3] / 'backend/config/env.yaml')
    settings = Settings()
    process_config = AssistantProcessConfig.from_settings(
        settings,
        experience_config=SimpleNamespace(
            streamvox_model="",
            streamvox_device="auto",
            streamvox_llama_backend=None,
            streamvox_role_name="",
            streamvox_seed=None,
            streamvox_inference_options={},
            streamvox_reference_audio_path=None,
            streamvox_prompt_text=None,
        ),
    )

    assert Settings.model_fields[
        "background_agent_terminal_diagnostics_enabled"
    ].default is False
    assert settings.background_agent_terminal_diagnostics_enabled is True
    assert process_config.background_agent_terminal_diagnostics_enabled is True
