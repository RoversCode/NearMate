"""验证执行配置的原子提交和发行 profile 迁移。"""

import json
import os
from pathlib import Path
import time

import pytest

from companion.core.execution_config import atomic_write_json, read_execution_configuration
from companion.core.runtime_profile import normalize_execution_selection


@pytest.mark.skipif(os.name != "nt", reason="需要 Windows 文件共享语义")
def test_state_write_survives_a_temporary_windows_reader(tmp_path: Path, monkeypatch) -> None:
    """普通只读句柄也会阻止替换；读取结束后应继续保存同一份进度。"""

    path = tmp_path / "voice-setup.json"
    path.write_text('{"completed_bytes": 0}', encoding="utf-8")
    reader = path.open("r", encoding="utf-8")
    waits = []

    def release_reader(delay: float) -> None:
        waits.append(delay)
        reader.close()

    monkeypatch.setattr(time, "sleep", release_reader)
    try:
        atomic_write_json(path, {"completed_bytes": 100})
    finally:
        reader.close()

    assert waits
    assert json.loads(path.read_text(encoding="utf-8")) == {"completed_bytes": 100}
    assert list(tmp_path.glob(".*.tmp")) == []


@pytest.mark.parametrize("winerror", [5, 32, 33])
def test_state_write_retries_windows_access_conflicts(tmp_path: Path, monkeypatch, winerror: int) -> None:
    """访问拒绝、共享冲突和锁冲突恢复后，都要完成原子替换。"""

    path = tmp_path / "voice-setup.json"
    path.write_text('{"phase": "download"}', encoding="utf-8")
    replace = os.replace
    calls = []
    waits = []

    def replace_after_two_conflicts(source, destination) -> None:
        calls.append((source, destination))
        if len(calls) <= 2:
            error = PermissionError("Windows 文件暂时被占用")
            error.winerror = winerror
            raise error
        replace(source, destination)

    monkeypatch.setattr(os, "replace", replace_after_two_conflicts)
    monkeypatch.setattr(time, "sleep", waits.append)
    atomic_write_json(path, {"phase": "load"})

    assert len(waits) == 2
    assert len({source for source, _ in calls}) == 1
    assert json.loads(path.read_text(encoding="utf-8")) == {"phase": "load"}


def test_state_write_stops_retrying_persistent_access_denial(tmp_path: Path, monkeypatch) -> None:
    """持续权限错误必须有界失败，原有完整状态仍留在磁盘上。"""

    path = tmp_path / "voice-setup.json"
    original = b'{"phase": "download"}'
    path.write_bytes(original)
    failure = PermissionError("Windows 持续拒绝访问")
    failure.winerror = 5
    calls = []
    waits = []

    def deny_replace(source, destination) -> None:
        calls.append((source, destination))
        raise failure

    monkeypatch.setattr(os, "replace", deny_replace)
    monkeypatch.setattr(time, "sleep", waits.append)
    with pytest.raises(PermissionError) as caught:
        atomic_write_json(path, {"phase": "load"})

    assert caught.value is failure
    assert 1 < len(calls) <= 10
    assert 0 < sum(waits) <= 2
    assert path.read_bytes() == original
    assert list(tmp_path.glob(".*.tmp")) == []


@pytest.mark.parametrize("failure", [OSError(28, "磁盘已满"), PermissionError(13, "权限不足")])
def test_state_write_does_not_retry_unrelated_errors(tmp_path: Path, monkeypatch, failure: OSError) -> None:
    """磁盘错误及不带 Windows 占用错误码的权限错误，应立即返回调用方。"""

    path = tmp_path / "voice-setup.json"
    path.write_text("{}", encoding="utf-8")
    waits = []

    def fail_replace(source, destination) -> None:
        raise failure

    monkeypatch.setattr(os, "replace", fail_replace)
    monkeypatch.setattr(time, "sleep", waits.append)
    with pytest.raises(OSError) as caught:
        atomic_write_json(path, {"phase": "load"})

    assert caught.value is failure
    assert waits == []
    assert path.read_text(encoding="utf-8") == "{}"


@pytest.mark.parametrize(
    ("profile", "selection"),
    [
        ("universal", {"onnx_provider": "cuda", "llama_backend": "cuda"}),
        ("universal", {"onnx_provider": "directml", "llama_backend": "vulkan"}),
        ("cuda", {"onnx_provider": "directml", "llama_backend": "vulkan"}),
    ],
)
def test_packaged_profile_migrates_incompatible_saved_execution_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    profile: str,
    selection: dict[str, str],
) -> None:
    """升级后读取旧配置时，后端选择会迁移到当前发行包支持的组合。"""

    monkeypatch.setenv("NEARMATE_PACKAGED", "1")
    monkeypatch.setenv("NEARMATE_RUNTIME_PROFILE", profile)
    path = tmp_path / "execution-config.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "selection": selection,
                "model_directory": str(tmp_path / "models"),
            }
        ),
        encoding="utf-8",
    )

    configuration = read_execution_configuration(path)
    assert configuration is not None
    migrated = normalize_execution_selection(
        configuration.selection.onnx_provider,
        configuration.selection.llama_backend,
        profile,
    )
    expected = {
        "universal": ("directml", "vulkan"),
        "cuda": ("cuda", "cuda"),
    }[profile]
    assert migrated == expected


@pytest.mark.parametrize(
    ("profile", "selection"),
    [
        ("universal", {"onnx_provider": "directml", "llama_backend": "vulkan"}),
        ("universal", {"onnx_provider": "cpu", "llama_backend": "cpu"}),
        ("cuda", {"onnx_provider": "cuda", "llama_backend": "cuda"}),
        ("cuda", {"onnx_provider": "cpu", "llama_backend": "cpu"}),
    ],
)
def test_packaged_profile_accepts_compatible_saved_execution_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    profile: str,
    selection: dict[str, str],
) -> None:
    """当前 profile 支持的 GPU 或 CPU 选择仍应正常恢复。"""

    monkeypatch.setenv("NEARMATE_PACKAGED", "1")
    monkeypatch.setenv("NEARMATE_RUNTIME_PROFILE", profile)
    path = tmp_path / "execution-config.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "selection": selection,
                "model_directory": str(tmp_path / "models"),
            }
        ),
        encoding="utf-8",
    )

    configuration = read_execution_configuration(path)
    assert configuration is not None
    assert configuration.selection.model_dump() == selection
