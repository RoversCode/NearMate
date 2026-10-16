from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


_SAFE_NAME_PATTERN = re.compile(r"[^A-Za-z0-9_.-]+")
_SAFE_SUFFIX_PATTERN = re.compile(r"[^A-Za-z0-9]+")


@dataclass(frozen=True)
class StreamVoxRoleAsset:
    """描述控制台上传的 StreamVox 角色参考音频资产。"""

    # role_name 是裁剪后的原始业务角色名，保留给 StreamVox 作为缓存键。
    role_name: str
    # audio_path 是后端持久保存的参考音频绝对路径。
    audio_path: Path


def save_streamvox_role_reference_audio(
    *,
    assets_root: Path,
    model: str,
    role_name: str,
    original_filename: str,
    content: bytes,
) -> StreamVoxRoleAsset:
    """保存控制台上传的单参考音频。"""

    # 业务角色名必须保留原文传给 StreamVox，但落盘目录名需要安全化，二者职责不能混用。
    normalized_role_name = role_name.strip()
    if not normalized_role_name:
        raise ValueError("StreamVox role_name must not be blank")

    # 空文件没有创建音色的业务意义，提前拒绝比让 SDK 报解析错误更清晰。
    if not content:
        raise ValueError("StreamVox reference audio file must not be empty")

    # 模型和角色目录名只用于本地文件系统，非法字符统一折叠为下划线，防止路径穿越或 Windows 保留字符。
    safe_model = _safe_path_part(model)
    safe_role = _safe_path_part(normalized_role_name)
    suffix = _safe_suffix(original_filename)
    role_dir = assets_root / safe_model / safe_role
    role_dir.mkdir(parents=True, exist_ok=True)

    # reference.<ext> 是每个角色当前参考音频的固定文件名；重复创建同名角色会在 provider 层被拒绝。
    audio_path = role_dir / f"reference{suffix}"
    audio_path.write_bytes(content)
    return StreamVoxRoleAsset(role_name=normalized_role_name, audio_path=audio_path)


def _safe_path_part(value: str) -> str:
    """把模型名或角色名转换为文件系统安全目录片段。"""

    # strip 点号和空白，避免 Windows 下出现当前目录、父目录或不可见目录名。
    safe_value = _SAFE_NAME_PATTERN.sub("_", value.strip()).strip(" .")
    return safe_value or "item"


def _safe_suffix(filename: str) -> str:
    """从上传文件名提取安全扩展名。"""

    # 只取 suffix，不信任完整文件名；浏览器可能传入带路径的旧式文件名。
    suffix = Path(filename).suffix.lower()
    if not suffix:
        return ".wav"
    safe_suffix = _SAFE_SUFFIX_PATTERN.sub("", suffix.lstrip("."))
    return f".{safe_suffix or 'wav'}"
