"""集中解析 NearMate 的只读 RuntimeLayout 与可写用户数据边界。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re


_LAYOUT_PATH_KEYS = (
    "python",
    "backend",
    "native",
    "shared",
    "console",
    "aec_package",
    "model_catalog",
    "seed_character_cards",
    "assets",
)
_WINDOWS_RESERVED_NAME = re.compile(
    r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])$",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class RuntimeLayout:
    """保存 Backend 进程启动后不可变的运行路径快照。"""

    runtime_root: Path
    resource_root: Path
    manifest_path: Path | None
    profile: str
    python_path: Path
    backend_root: Path
    native_root: Path
    shared_root: Path
    console_root: Path
    aec_package_root: Path
    model_catalog_path: Path
    seed_character_cards_root: Path
    assets_root: Path


def _find_development_root() -> Path:
    """找到包含共享契约的开发仓库根目录。

    Returns:
        开发仓库根目录；如果调用环境不是仓库，则返回当前目录。
    """

    candidates = [Path.cwd().resolve(), *Path(__file__).resolve().parents]
    for candidate in candidates:
        if (candidate / "shared" / "default-settings.json").is_file() and (
            candidate / "backend" / "src"
        ).is_dir():
            return candidate
    return Path.cwd().resolve()


DEVELOPMENT_ROOT = _find_development_root()


def load_runtime_layout(
    *,
    environment: Mapping[str, str] | None = None,
    development_root: Path | None = None,
) -> RuntimeLayout:
    """读取安装清单或构造开发态布局。

    Args:
        environment: 启动器注入的受管环境；测试可传入独立字典。
        development_root: 开发态仓库根目录；测试可显式指定。

    Returns:
        Backend、原生库、共享契约和静态资源共用的绝对路径快照。

    Raises:
        RuntimeError: 安装态缺少 schema v3 清单、路径越界或 profile 漂移时抛出。
    """

    current_environment = environment if environment is not None else os.environ
    repository_root = (development_root or DEVELOPMENT_ROOT).expanduser().resolve()
    if current_environment.get("NEARMATE_PACKAGED") != "1":
        return _development_layout(repository_root, current_environment)

    runtime_root = _required_absolute_path(
        current_environment,
        "NEARMATE_RUNTIME_ROOT",
    )
    resource_root = _required_absolute_path(
        current_environment,
        "NEARMATE_RESOURCE_ROOT",
    )
    manifest_path = _required_absolute_path(
        current_environment,
        "NEARMATE_RUNTIME_MANIFEST",
    )
    if manifest_path.parent != runtime_root or manifest_path.name != "runtime-manifest.json":
        raise RuntimeError("NEARMATE_RUNTIME_MANIFEST 必须指向 runtime 根目录中的清单")

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exception:
        raise RuntimeError(f"无法读取安装态 RuntimeLayout 清单：{manifest_path}") from exception

    if not isinstance(manifest, dict):
        raise RuntimeError("安装态 RuntimeLayout 清单必须是 JSON 对象")
    profile = manifest.get("profile")
    if (
        manifest.get("schema_version") != 3
        or manifest.get("product") != "NearMate"
        or manifest.get("platform") != "windows-x64"
        or manifest.get("python_version") != "3.12.13"
        or profile not in {"cuda", "universal"}
    ):
        raise RuntimeError("Backend 只接受 NearMate windows-x64 schema v3 RuntimeLayout")
    injected_profile = current_environment.get("NEARMATE_RUNTIME_PROFILE", "").strip()
    if injected_profile != profile:
        raise RuntimeError("NEARMATE_RUNTIME_PROFILE 与 RuntimeLayout 清单不一致")

    raw_paths = manifest.get("paths")
    if not isinstance(raw_paths, dict):
        raise RuntimeError("安装态 RuntimeLayout 清单缺少 paths 对象")
    resolved_paths = {
        key: _resolve_runtime_member(runtime_root, raw_paths.get(key), field=f"paths.{key}")
        for key in _LAYOUT_PATH_KEYS
    }
    return RuntimeLayout(
        runtime_root=runtime_root,
        resource_root=resource_root,
        manifest_path=manifest_path,
        profile=str(profile),
        python_path=resolved_paths["python"],
        backend_root=resolved_paths["backend"],
        native_root=resolved_paths["native"],
        shared_root=resolved_paths["shared"],
        console_root=resolved_paths["console"],
        aec_package_root=resolved_paths["aec_package"],
        model_catalog_path=resolved_paths["model_catalog"],
        seed_character_cards_root=resolved_paths["seed_character_cards"],
        assets_root=resolved_paths["assets"],
    )


def resolve_user_data_root(
    *,
    environment: Mapping[str, str] | None = None,
    development_root: Path | None = None,
) -> Path:
    """返回唯一可写的用户数据根目录。

    Args:
        environment: 启动器注入的受管环境；测试可传入独立字典。
        development_root: 开发态仓库根目录；测试可显式指定。

    Returns:
        安装态的用户数据目录，或开发仓库中的 ``backend/data``。

    Raises:
        RuntimeError: 安装态没有注入规范的 ``NEARMATE_DATA_ROOT`` 时抛出。
    """

    current_environment = environment if environment is not None else os.environ
    configured_root = current_environment.get("NEARMATE_DATA_ROOT", "").strip()
    if configured_root:
        path = Path(configured_root).expanduser()
        if not path.is_absolute():
            raise RuntimeError("NEARMATE_DATA_ROOT 必须是绝对路径")
        return path.resolve()
    if current_environment.get("NEARMATE_PACKAGED") == "1":
        raise RuntimeError("安装态缺少 NEARMATE_DATA_ROOT")
    return (development_root or DEVELOPMENT_ROOT).expanduser().resolve() / "backend" / "data"


def _development_layout(
    repository_root: Path,
    environment: Mapping[str, str],
) -> RuntimeLayout:
    """构造不依赖发行清单的开发态布局。"""

    profile = environment.get("NEARMATE_RUNTIME_PROFILE", "cuda").strip().lower()
    if profile not in {"cuda", "universal"}:
        raise RuntimeError("NEARMATE_RUNTIME_PROFILE 只能是 cuda 或 universal")
    backend_root = repository_root / "backend"
    return RuntimeLayout(
        runtime_root=repository_root,
        resource_root=repository_root,
        manifest_path=None,
        profile=profile,
        python_path=repository_root / ".venv" / "Scripts" / "python.exe",
        backend_root=backend_root,
        native_root=backend_root / "bin",
        shared_root=repository_root / "shared",
        console_root=repository_root / "console" / "dist",
        aec_package_root=(
            repository_root / "tools" / "audio-aec-poc" / "src" / "audio_aec_sidecar"
        ),
        model_catalog_path=(
            repository_root / "installer" / "model-catalog.json"
        ),
        seed_character_cards_root=backend_root / "data" / "character-cards",
        assets_root=repository_root / "assets",
    )


def _required_absolute_path(environment: Mapping[str, str], variable_name: str) -> Path:
    """读取安装启动器必须提供的绝对路径。"""

    raw_value = environment.get(variable_name, "").strip()
    path = Path(raw_value).expanduser()
    if not raw_value or not path.is_absolute():
        raise RuntimeError(f"安装态缺少绝对路径环境变量 {variable_name}")
    return path.resolve()


def _resolve_runtime_member(runtime_root: Path, value: object, *, field: str) -> Path:
    """校验并解析一个清单相对路径，禁止越过 runtime 根目录。"""

    if not isinstance(value, str):
        raise RuntimeError(f"安装态 RuntimeLayout 缺少路径字段 {field}")
    segments = value.split("/")
    has_control_character = any(
        ord(character) < 0x20 or ord(character) == 0x7F
        for character in value
    )
    has_reserved_name = any(
        _WINDOWS_RESERVED_NAME.fullmatch(segment.split(".", 1)[0]) is not None
        for segment in segments
    )
    if (
        not value
        or value.startswith("/")
        or re.match(r"^[A-Za-z]:", value) is not None
        or "\\" in value
        or has_control_character
        or any(segment in {"", ".", ".."} for segment in segments)
        or any(segment.endswith((" ", ".")) for segment in segments)
        or has_reserved_name
    ):
        raise RuntimeError(f"RuntimeLayout 路径 {field} 必须是安全的正斜杠相对路径")
    resolved_path = (runtime_root / Path(*segments)).resolve()
    if not resolved_path.is_relative_to(runtime_root):
        raise RuntimeError(f"RuntimeLayout 路径 {field} 越过 runtime 根目录")
    return resolved_path


RUNTIME_LAYOUT = load_runtime_layout()
RUNTIME_ROOT = RUNTIME_LAYOUT.runtime_root
RESOURCE_ROOT = RUNTIME_LAYOUT.resource_root
RUNTIME_MANIFEST_PATH = RUNTIME_LAYOUT.manifest_path
RUNTIME_PROFILE = RUNTIME_LAYOUT.profile
BACKEND_ROOT = RUNTIME_LAYOUT.backend_root
NATIVE_ROOT = RUNTIME_LAYOUT.native_root
SHARED_ROOT = RUNTIME_LAYOUT.shared_root
CONSOLE_ROOT = RUNTIME_LAYOUT.console_root
AEC_PACKAGE_ROOT = RUNTIME_LAYOUT.aec_package_root
MODEL_CATALOG_PATH = RUNTIME_LAYOUT.model_catalog_path
SEED_CHARACTER_CARDS_ROOT = RUNTIME_LAYOUT.seed_character_cards_root
ASSETS_ROOT = RUNTIME_LAYOUT.assets_root
USER_DATA_ROOT = resolve_user_data_root()
DATA_ROOT = USER_DATA_ROOT
