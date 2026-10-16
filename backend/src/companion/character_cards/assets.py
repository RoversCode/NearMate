from __future__ import annotations

from pathlib import Path

from companion.character_cards.models import CharacterCardStoreError


MAX_CHARACTER_CARD_COVER_BYTES = 2 * 1024 * 1024
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_JPEG_SIGNATURE = b"\xff\xd8\xff"
_COVER_FILENAME_BY_MEDIA_TYPE = {
    "image/png": "icon.png",
    "image/jpeg": "icon.jpg",
}
_KNOWN_COVER_FILENAMES = ("icon.png", "icon.jpg", "icon.jpeg")


def detect_cover_asset_filename(*, content_type: str, content: bytes) -> str:
    """校验上传封面并返回标准封面文件名。

    Args:
        content_type: 浏览器上传文件声明的媒体类型。
        content: 上传图片的完整二进制内容。

    Returns:
        标准化后的封面文件名。
    """

    normalized_content_type = content_type.split(";", maxsplit=1)[0].strip().lower()
    if normalized_content_type not in _COVER_FILENAME_BY_MEDIA_TYPE:
        raise CharacterCardStoreError("角色卡封面只支持 PNG 或 JPEG")
    if not content:
        raise CharacterCardStoreError("角色卡封面不能为空")
    if len(content) > MAX_CHARACTER_CARD_COVER_BYTES:
        raise CharacterCardStoreError("角色卡封面不能超过 2MB")

    # 不能只相信浏览器 content-type；本地 API 也可能被手写请求调用，必须用文件头再校验一次。
    if normalized_content_type == "image/png" and not content.startswith(_PNG_SIGNATURE):
        raise CharacterCardStoreError("角色卡封面不是合法 PNG")
    if normalized_content_type == "image/jpeg" and not content.startswith(_JPEG_SIGNATURE):
        raise CharacterCardStoreError("角色卡封面不是合法 JPEG")
    return _COVER_FILENAME_BY_MEDIA_TYPE[normalized_content_type]


def save_cover_asset_file(card_dir: Path, *, filename: str, content: bytes) -> str:
    """保存封面资产并删除同卡旧封面。

    Args:
        card_dir: 当前角色卡目录。
        filename: 已校验的标准封面文件名。
        content: 上传图片的二进制内容。

    Returns:
        写入 YAML 的相对资产路径。
    """

    if filename not in _KNOWN_COVER_FILENAMES:
        raise CharacterCardStoreError("角色卡封面文件名非法")

    assets_dir = card_dir / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    for stale_filename in _KNOWN_COVER_FILENAMES:
        stale_path = assets_dir / stale_filename
        if stale_path.exists():
            stale_path.unlink()

    cover_path = assets_dir / filename
    cover_path.write_bytes(content)
    return f"assets/{filename}"


def delete_cover_asset_file(card_dir: Path, avatar_path: str | None) -> None:
    """删除当前封面资产。

    Args:
        card_dir: 当前角色卡目录。
        avatar_path: YAML 中记录的当前封面相对路径。

    Returns:
        无返回值。
    """

    assets_dir = card_dir / "assets"
    if not assets_dir.exists():
        return

    filenames = set(_KNOWN_COVER_FILENAMES)
    if avatar_path:
        filenames.add(Path(avatar_path).name)
    for filename in filenames:
        target_path = assets_dir / filename
        if target_path.exists():
            target_path.unlink()


def resolve_cover_asset_file(card_dir: Path, *, declared_avatar_path: str | None, requested_asset_path: str) -> Path:
    """把封面相对路径解析成真实文件路径。

    Args:
        card_dir: 当前角色卡目录。
        declared_avatar_path: 当前角色卡 meta.avatar_path。
        requested_asset_path: API 路径中请求的资产相对路径。

    Returns:
        可安全读取的封面文件路径。
    """

    normalized_requested_path = requested_asset_path.replace("\\", "/").strip("/")
    if not declared_avatar_path or normalized_requested_path != declared_avatar_path:
        raise CharacterCardStoreError("角色卡封面不存在")
    if ".." in Path(normalized_requested_path).parts or Path(normalized_requested_path).is_absolute():
        raise CharacterCardStoreError("角色卡封面路径非法")

    resolved_card_dir = card_dir.resolve()
    resolved_asset_path = (card_dir / normalized_requested_path).resolve()
    if resolved_card_dir not in resolved_asset_path.parents:
        raise CharacterCardStoreError("角色卡封面路径越界")
    if not resolved_asset_path.is_file():
        raise CharacterCardStoreError("角色卡封面不存在")
    return resolved_asset_path
