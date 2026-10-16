"""验证冻结 Python 的 C++ 运行库边界，防止开发机的系统 DLL 掩盖漏包。"""

import hashlib
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from installer import verify_python_runtime


@pytest.fixture
def msvc_runtime(tmp_path: Path) -> Path:
    """创建带完整清单的小型 DLL 目录，便于独立制造缺失和篡改场景。

    Args:
        tmp_path: Pytest 提供的临时目录。

    Returns:
        测试专用的 Python 根目录。
    """
    manifest_path = Path(__file__).parents[1] / "msvc-runtime.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    license_directory = tmp_path / "licenses" / "msvc-runtime"
    for records, directory in ((manifest["files"], tmp_path / "Scripts"), (manifest["licenses"], license_directory)):
        directory.mkdir(parents=True, exist_ok=True)
        for record in records:
            content = record["name"].encode()
            (directory / record["name"]).write_bytes(content)
            record["sha256"] = hashlib.sha256(content).hexdigest()
    (license_directory / "msvc-runtime.json").write_text(json.dumps(manifest), encoding="utf-8")
    (license_directory / "SOURCE.txt").write_text("official source notice", encoding="utf-8")
    return tmp_path


def install_loaded_module_fixture(monkeypatch: pytest.MonkeyPatch, paths: dict[str, Path]) -> None:
    """模拟 Windows 返回的实际已加载模块路径，不预加载任何 DLL。

    Args:
        monkeypatch: Pytest 的替换工具。
        paths: DLL 名称与假设的实际加载路径。

    Returns:
        无；当前测试会用这些路径代替 Windows 模块查询。
    """
    def get_module_handle(name: str) -> str | None:
        return name if name in paths else None

    def get_module_file_name(handle: str, buffer: object, size: int) -> int:
        buffer.value = str(paths[handle])
        return len(buffer.value)

    kernel32 = SimpleNamespace(GetModuleHandleW=get_module_handle, GetModuleFileNameW=get_module_file_name)
    monkeypatch.setattr(verify_python_runtime.ctypes, "WinDLL", lambda *args, **kwargs: kernel32, raising=False)


def test_msvc_verification_accepts_complete_local_loaded_runtime(msvc_runtime: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """文件齐全且原生依赖实际使用安装目录时才允许通过。"""
    names = ("msvcp140.dll", "msvcp140_1.dll", "vcruntime140.dll", "vcruntime140_1.dll")
    paths = {name: msvc_runtime / "Scripts" / name for name in names}
    install_loaded_module_fixture(monkeypatch, paths)

    report = verify_python_runtime.verify_msvc_runtime(msvc_runtime)

    assert report["version"] == "14.42.34433.0"
    assert report["loaded"] == {name: str(path) for name, path in paths.items()}


def test_msvc_verification_rejects_system_dll_fallback(msvc_runtime: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """随包 DLL 即使齐全，已加载的系统副本也必须阻断自包含验收。"""
    install_loaded_module_fixture(monkeypatch, {"msvcp140.dll": msvc_runtime / "System32" / "msvcp140.dll"})

    with pytest.raises(RuntimeError, match="必须从随包 Scripts 加载"):
        verify_python_runtime.verify_msvc_runtime(msvc_runtime)


def test_msvc_verification_rejects_unloaded_dependency(msvc_runtime: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """文件存在不能代替真实导入后的原生加载证据。"""
    install_loaded_module_fixture(monkeypatch, {})

    with pytest.raises(RuntimeError, match="没有加载必需"):
        verify_python_runtime.verify_msvc_runtime(msvc_runtime)


@pytest.mark.parametrize("file_name", ["msvcp140_1.dll", "msvcp140_atomic_wait.dll"])
def test_msvc_verification_rejects_missing_dll(msvc_runtime: Path, file_name: str) -> None:
    """直接依赖和其余固定闭包都必须完整，不能只检查主 DLL。"""
    (msvc_runtime / "Scripts" / file_name).unlink()

    with pytest.raises(RuntimeError, match="SHA256"):
        verify_python_runtime.verify_msvc_runtime(msvc_runtime)


def test_msvc_verification_rejects_replaced_dll(msvc_runtime: Path) -> None:
    """同名 DLL 的内容被替换也必须拒绝。"""
    (msvc_runtime / "Scripts" / "msvcp140.dll").write_bytes(b"replaced")

    with pytest.raises(RuntimeError, match="SHA256"):
        verify_python_runtime.verify_msvc_runtime(msvc_runtime)


def test_msvc_verification_rejects_manifest_that_omits_dependency(msvc_runtime: Path) -> None:
    """删掉清单记录不能把缺少运行库变成验证成功。"""
    manifest_path = msvc_runtime / "licenses" / "msvc-runtime" / "msvc-runtime.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"] = manifest["files"][:-1]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(RuntimeError, match="清单不完整"):
        verify_python_runtime.verify_msvc_runtime(msvc_runtime)


@pytest.mark.skipif(os.name != "nt", reason="需要 Windows 的目录联接")
@pytest.mark.parametrize("relative_directory", ["Scripts", "licenses", "licenses/msvc-runtime"])
def test_msvc_verification_rejects_link_to_identical_external_material(
    msvc_runtime: Path, monkeypatch: pytest.MonkeyPatch, relative_directory: str,
) -> None:
    """相同内容也不能通过目录联接借用包外 DLL、许可或其父目录。

    Args:
        msvc_runtime: 包含完整运行库和许可的小型 Python 目录。
        monkeypatch: 模拟已装载 DLL 的 Windows 查询结果。
        relative_directory: 本次替换为包外联接的目录。

    Returns:
        无；实际目录联接必须在加载路径查询前被拒绝。
    """
    target = msvc_runtime / relative_directory
    external = msvc_runtime.parent / (msvc_runtime.name + "-external-" + target.name)
    target.rename(external)
    # 原文件内容未改动，故只有路径边界检查能识别这次借用外部资源。
    command = "New-Item -ItemType Junction -Path '" + str(target).replace("'", "''")
    command += "' -Target '" + str(external).replace("'", "''") + "' | Out-Null"
    subprocess.run(["pwsh", "-NoProfile", "-Command", command], check=True, capture_output=True)
    names = ("msvcp140.dll", "msvcp140_1.dll", "vcruntime140.dll", "vcruntime140_1.dll")
    install_loaded_module_fixture(monkeypatch, {name: (msvc_runtime / "Scripts" / name).resolve() for name in names})
    try:
        with pytest.raises(RuntimeError, match="重解析点"):
            verify_python_runtime.verify_msvc_runtime(msvc_runtime)
    finally:
        target.rmdir()
        external.rename(target)
