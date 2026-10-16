"""验证模型留在用户目录，防止升级清理安装文件时误删模型。"""

import json
import os
import subprocess

import pytest

from companion.core import runtime_paths
from companion.core.execution_config import read_execution_configuration
from companion.core.voice_setup_assets import validate_model_directory


@pytest.fixture
def installation(tmp_path, monkeypatch):
    """模拟安装目录与独立用户目录，不使用真正的发行文件。"""
    root = tmp_path / "installed-app"
    resource_root = root / "resources"
    resource_root.mkdir(parents=True)
    monkeypatch.setenv("NEARMATE_PACKAGED", "1")
    monkeypatch.setattr(runtime_paths, "RESOURCE_ROOT", resource_root)
    return root


@pytest.mark.parametrize("relative", [".", "resources/models", "runtime-models"])
def test_installation_directory_is_rejected_before_writing(installation, relative):
    """误选安装根或其子目录时，不先创建文件再报告错误。"""
    target = installation / relative
    existed = target.exists()
    with pytest.raises(ValueError, match="安装目录"):
        validate_model_directory(str(target))
    assert target.exists() is existed


def test_saved_unsafe_directory_requires_confirmation_again(installation, tmp_path):
    """旧配置不能在重启时绕过新的目录边界。"""
    configuration = tmp_path / "execution.json"
    configuration.write_text(json.dumps({
        "schema_version": 1, "selection": {"onnx_provider": "cpu", "llama_backend": "cpu"},
        "model_directory": str(installation / "resources" / "models"),
    }), encoding="utf-8")
    assert read_execution_configuration(configuration) is None


def test_user_directory_remains_writable(installation, tmp_path):
    """用户在其它目录或磁盘保存模型的能力继续有效。"""
    target = tmp_path / "user-models"
    assert validate_model_directory(str(target)) == target.resolve()
    assert target.is_dir()


@pytest.mark.skipif(os.name != "nt", reason="验证 Windows 目录联接")
def test_directory_junction_cannot_point_downloads_into_installation(installation, tmp_path):
    """外部盘符或目录别名仍不能把模型文件落进安装目录。"""
    alias = tmp_path / "model-disk"
    subprocess.run(["cmd", "/d", "/c", "mklink", "/J", str(alias), str(installation / "resources")],
                   check=True, capture_output=True)
    try:
        with pytest.raises(ValueError, match="安装目录"):
            validate_model_directory(str(alias / "models"))
        assert not (installation / "resources" / "models").exists()
    finally:
        # 只移除测试创建的联接本身，不遍历它指向的安装资源。
        os.rmdir(alias)
