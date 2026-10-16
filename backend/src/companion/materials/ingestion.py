from __future__ import annotations

from pathlib import Path
from typing import Any

from companion.filesystem.safety import (
    FileSystemNodeInspectionError,
    inspect_filesystem_node,
)
from companion.materials.models import (
    MaterialAttachment,
    MaterialNodeKind,
    MaterialRejectionReason,
)
from companion.materials.type_registry import MATERIAL_TYPES


class MaterialIngestionService:
    """对材料入口做轻量接纳并生成稳定引用。"""

    def ingest_staged_material(
        self,
        item: dict[str, Any],
        *,
        turn_id: str,
        created_at: str,
        material_id: str,
    ) -> tuple[MaterialAttachment | None, MaterialRejectionReason | None]:
        """归一化单个材料载荷并生成服务端工作量检查。

        Args:
            item: 外部提交的材料项载荷。
            turn_id: 材料最初进入的 turn 编号。
            created_at: 服务端接收时间。
            material_id: 服务端生成的材料编号。

        Returns:
            可进入本轮材料上下文的附件。
        """

        node_kind, rejection_reason = _admission_result(item)
        if rejection_reason is not None:
            return None, rejection_reason
        if node_kind is None:
            raise RuntimeError("材料接纳成功时必须已经确认节点类型")
        display_name = str(item.get("display_name") or "未命名材料").strip() or "未命名材料"
        return MaterialAttachment(
            material_id=material_id,
            turn_id=turn_id,
            kind=item["kind"],
            source=str(item.get("source") or "unknown"),
            display_name=display_name,
            path=str(item["path"]).strip(),
            created_at=created_at,
            node_kind=node_kind,
            media_class=_media_class(item),
            managed_asset_id=_optional_text(item.get("managed_asset_id")),
        ), None


def _optional_text(value: object) -> str | None:
    """裁剪可选文本字段并把空字符串归一化为 None。"""

    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _admission_result(
    item: dict[str, Any],
) -> tuple[MaterialNodeKind | None, MaterialRejectionReason | None]:
    """核对入口声明与真实节点，并给出可登记的材料类型。

    Args:
        item: 外部提交的单项材料载荷。

    Returns:
        成功时返回节点类型和空原因；失败时返回空类型与稳定拒绝原因。
    """

    kind = str(item.get("kind") or "").strip()
    path_text = str(item.get("path") or "").strip()
    if not path_text:
        return None, "missing_path"
    path = Path(path_text)
    try:
        node = inspect_filesystem_node(path)
    except FileNotFoundError:
        return None, "missing_path"
    except FileSystemNodeInspectionError:
        return None, "metadata_unreadable"
    if node.is_linked_or_reparse:
        return None, "symlink_not_allowed"

    expected_node_kind: MaterialNodeKind = "directory" if kind == "dragged_folder" else "file"
    if node.node_kind != expected_node_kind:
        return None, "not_regular_file"
    if expected_node_kind == "directory":
        return "directory", None
    if not MATERIAL_TYPES.is_supported_path(path):
        return None, "unsupported_extension"
    if node.size <= 0:
        return None, "empty_file"
    return "file", None


def _media_class(item: dict[str, Any]) -> str:
    """根据产品 kind 和路径白名单确定预检预算类别。"""

    kind = str(item.get("kind") or "").strip()
    if kind in {"clipboard_text", "selected_text"}:
        return "text"
    if kind in {"selected_web", "dragged_folder"}:
        return "mixed_or_document"
    path_text = str(item.get("path") or "").strip()
    if path_text and kind != "dragged_folder":
        return MATERIAL_TYPES.classify_path_media(Path(path_text))
    return "mixed_or_document"
