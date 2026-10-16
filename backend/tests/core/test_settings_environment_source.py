"""验证安装启动器注入的进程环境变量不会被共享默认值吞掉。"""

from pathlib import Path

from companion.core import settings as settings_module
from companion.core.settings import Settings


def test_external_livekit_environment_overrides_shared_defaults(
    monkeypatch, tmp_path: Path
) -> None:
    """发行环境注入的 LiveKit 凭据必须覆盖仓库里的开发默认值。"""

    env_path = tmp_path / "env.yaml"
    env_path.write_text(
        "livekit_url: ws://yaml.example:7880\n"
        "livekit_api_key: yaml-key\n"
        "livekit_api_secret: yaml-secret\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(settings_module, "ENV_YAML_PATH", env_path)
    monkeypatch.setenv("LIVEKIT_URL", "wss://external.example:443")
    monkeypatch.setenv("LIVEKIT_API_KEY", "external-key")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "external-secret")

    settings = Settings()

    assert settings.livekit_url == "wss://external.example:443"
    assert settings.livekit_api_key == "external-key"
    assert settings.livekit_api_secret == "external-secret"


def test_env_yaml_remains_a_fallback_when_external_value_is_absent(
    monkeypatch, tmp_path: Path
) -> None:
    """没有外部凭据时，发行 env.yaml 仍可提供非敏感配置。"""

    env_path = tmp_path / "env.yaml"
    env_path.write_text("livekit_url: ws://yaml.example:7880\n", encoding="utf-8")
    monkeypatch.setattr(settings_module, "ENV_YAML_PATH", env_path)
    monkeypatch.delenv("LIVEKIT_URL", raising=False)

    settings = Settings()

    assert settings.livekit_url == "ws://yaml.example:7880"
