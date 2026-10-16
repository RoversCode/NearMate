from __future__ import annotations

import json
from pathlib import Path

import pytest

from companion.core.runtime_paths import load_runtime_layout, resolve_user_data_root


def _write_runtime_manifest(runtime_root: Path, *, profile: str = "cuda") -> Path:
    """写入 RuntimeLayout 测试使用的最小 schema v3 清单。

    Args:
        runtime_root: 测试运行时根目录。
        profile: 清单声明的发行档位。

    Returns:
        已写入的清单路径。
    """

    runtime_root.mkdir(parents=True)
    manifest_path = runtime_root / "runtime-manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 3,
                "product": "NearMate",
                "platform": "windows-x64",
                "python_version": "3.12.13",
                "profile": profile,
                "paths": {
                    "python": "embedded/python.exe",
                    "backend": "application/backend",
                    "native": "application/native",
                    "shared": "contracts",
                    "console": "web/console",
                    "aec_package": "embedded/site-packages/audio_aec_sidecar",
                    "model_catalog": "catalog/models.json",
                    "seed_character_cards": "application/seed-cards",
                    "assets": "desktop-assets",
                },
            }
        ),
        encoding="utf-8",
    )
    return manifest_path


def test_development_layout_uses_repository_boundaries(tmp_path: Path) -> None:
    """开发态不要求发行清单，并统一锚定显式仓库根目录。"""

    layout = load_runtime_layout(environment={}, development_root=tmp_path)

    assert layout.runtime_root == tmp_path.resolve()
    assert layout.backend_root == tmp_path.resolve() / "backend"
    assert layout.native_root == tmp_path.resolve() / "backend" / "bin"
    assert layout.shared_root == tmp_path.resolve() / "shared"
    assert layout.model_catalog_path == tmp_path.resolve() / "installer" / "model-catalog.json"
    assert resolve_user_data_root(environment={}, development_root=tmp_path) == (
        tmp_path.resolve() / "backend" / "data"
    )


def test_packaged_layout_reads_every_path_from_manifest(tmp_path: Path) -> None:
    """安装态 Backend 必须消费 schema v3 paths，不能重新猜目录名。"""

    runtime_root = tmp_path / "runtime"
    manifest_path = _write_runtime_manifest(runtime_root)
    layout = load_runtime_layout(
        environment={
            "NEARMATE_PACKAGED": "1",
            "NEARMATE_RUNTIME_ROOT": str(runtime_root),
            "NEARMATE_RESOURCE_ROOT": str(tmp_path),
            "NEARMATE_RUNTIME_MANIFEST": str(manifest_path),
            "NEARMATE_RUNTIME_PROFILE": "cuda",
        }
    )

    assert layout.backend_root == runtime_root / "application" / "backend"
    assert layout.native_root == runtime_root / "application" / "native"
    assert layout.shared_root == runtime_root / "contracts"
    assert layout.console_root == runtime_root / "web" / "console"
    assert layout.model_catalog_path == runtime_root / "catalog" / "models.json"


def test_packaged_layout_rejects_profile_drift(tmp_path: Path) -> None:
    """启动参数和清单 profile 不一致时必须在导入业务模块前失败。"""

    runtime_root = tmp_path / "runtime"
    manifest_path = _write_runtime_manifest(runtime_root, profile="cuda")

    with pytest.raises(RuntimeError, match="RuntimeLayout"):
        load_runtime_layout(
            environment={
                "NEARMATE_PACKAGED": "1",
                "NEARMATE_RUNTIME_ROOT": str(runtime_root),
                "NEARMATE_RESOURCE_ROOT": str(tmp_path),
                "NEARMATE_RUNTIME_MANIFEST": str(manifest_path),
                "NEARMATE_RUNTIME_PROFILE": "universal",
            }
        )


def test_packaged_layout_rejects_path_escape(tmp_path: Path) -> None:
    """清单路径不得通过父目录越过 runtime 根目录。"""

    runtime_root = tmp_path / "runtime"
    manifest_path = _write_runtime_manifest(runtime_root)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["paths"]["native"] = "../native"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(RuntimeError, match="正斜杠相对路径"):
        load_runtime_layout(
            environment={
                "NEARMATE_PACKAGED": "1",
                "NEARMATE_RUNTIME_ROOT": str(runtime_root),
                "NEARMATE_RESOURCE_ROOT": str(tmp_path),
                "NEARMATE_RUNTIME_MANIFEST": str(manifest_path),
                "NEARMATE_RUNTIME_PROFILE": "cuda",
            }
        )


def test_packaged_user_data_requires_canonical_variable(tmp_path: Path) -> None:
    """安装态不接受已删除的用户数据目录兼容别名。"""

    environment = {
        "NEARMATE_PACKAGED": "1",
        "NEARMATE_USER_DATA_DIR": str(tmp_path / "legacy"),
    }
    with pytest.raises(RuntimeError, match="NEARMATE_DATA_ROOT"):
        resolve_user_data_root(environment=environment)

    user_data_root = tmp_path / "NearMate"
    environment["NEARMATE_DATA_ROOT"] = str(user_data_root)
    assert resolve_user_data_root(environment=environment) == user_data_root.resolve()
