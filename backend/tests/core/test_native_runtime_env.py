"""确保安装态在加载原生 DLL 前拒绝未经验证的语音依赖组合。"""

from types import SimpleNamespace

import pytest

from companion.core import native_runtime_env


@pytest.mark.parametrize("profile", ["cuda", "universal"])
@pytest.mark.parametrize("sherpa_version", ["1.13.2", "1.13.2+cuda12.cudnn9"])
def test_packaged_runtime_rejects_incompatible_sherpa_before_loading_dll(
    monkeypatch: pytest.MonkeyPatch, profile: str, sherpa_version: str,
) -> None:
    """不兼容组合应给出修复组件的错误，不能等创建模型时发生原生崩溃。"""
    monkeypatch.setenv("NEARMATE_PACKAGED", "1")
    monkeypatch.setenv("NEARMATE_RUNTIME_PROFILE", profile)
    monkeypatch.setattr(native_runtime_env, "ensure_single_onnxruntime_distribution", lambda: None)
    monkeypatch.setattr(
        native_runtime_env.importlib.metadata, "distribution",
        lambda _name: SimpleNamespace(
            version="1.21.1",
            locate_file=lambda _path: pytest.fail("不兼容依赖必须在尝试加载 DLL 前失败"),
        ),
    )
    monkeypatch.setattr(native_runtime_env.importlib.metadata, "version", lambda _name: sherpa_version)

    with pytest.raises(RuntimeError, match="sherpa-onnx 1.12.20"):
        native_runtime_env.configure_native_runtime_environment()


@pytest.mark.parametrize("core_version", [None, "1.13.2"])
def test_packaged_runtime_rejects_missing_or_mismatched_sherpa_core(
    monkeypatch: pytest.MonkeyPatch, core_version: str | None,
) -> None:
    """Python 入口版本正确也不能掩盖独立原生 core 被漏装或升级。"""
    monkeypatch.setenv("NEARMATE_PACKAGED", "1")
    monkeypatch.setattr(native_runtime_env, "ensure_single_onnxruntime_distribution", lambda: None)
    monkeypatch.setattr(
        native_runtime_env.importlib.metadata, "distribution",
        lambda _name: SimpleNamespace(version="1.21.1"),
    )

    def read_version(package_name: str) -> str:
        if package_name == "sherpa-onnx":
            return "1.12.20"
        if core_version is None:
            raise native_runtime_env.importlib.metadata.PackageNotFoundError(package_name)
        return core_version

    monkeypatch.setattr(native_runtime_env.importlib.metadata, "version", read_version)
    with pytest.raises(RuntimeError, match="sherpa-onnx-core 1.12.20"):
        native_runtime_env.configure_native_runtime_environment()
