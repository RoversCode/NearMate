"""把 Foreground 冻结材料转换为后台工作输入。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import Literal, cast
from uuid import uuid4

from companion.background_agent.contracts import AgentMaterial
from companion.materials.models import (
    MaterialKind,
    MaterialMediaClass,
    MaterialNodeKind,
    normalize_material_path,
)
from companion.materials.recent_validation import inspect_recent_material_path


MAX_RECENT_MATERIAL_PATHS = 8
RecentMaterialFailureReason = Literal[
    "recent_material_not_registered",
    "recent_material_missing",
    "recent_material_type_changed",
    "recent_material_unsafe",
    "recent_material_unreadable",
]


@dataclass(frozen=True, slots=True)
class RecentMaterialValidationFailure:
    """保存一项近期路径的内部失败码和用户安全说明。

    Args:
        reason: Backend 稳定机械失败码，不进入模型工具结果。
        display_name: 可以向用户展示的材料名称。
        detail: 不含绝对路径和内部身份的自然语言事实。
    """

    reason: RecentMaterialFailureReason
    display_name: str
    detail: str

    def to_tool_payload(self) -> dict[str, str]:
        """转换成工具后二轮可以安全读取的最小字段。

        Returns:
            只包含展示名称和自然语言失败事实的安全载荷。
        """

        return {
            "display_name": self.display_name,
            "detail": self.detail,
        }


def materials_from_foreground_context(
    foreground_context: dict[str, object],
) -> tuple[AgentMaterial, ...]:
    """读取本轮权威附件，生成后台工作材料白名单。

    Args:
        foreground_context: 当前轮冻结的产品态势。

    Returns:
        保持附件顺序的正式材料；字段非法的项目会被忽略。
    """

    context = foreground_context.get("active_turn_material_context")
    if not isinstance(context, dict):
        return ()
    attachments = context.get("attachments")
    if not isinstance(attachments, list):
        return ()

    materials: list[AgentMaterial] = []
    for attachment in attachments:
        if not isinstance(attachment, dict):
            continue
        material_id = _clean_text(attachment.get("material_id"))
        display_name = _clean_text(attachment.get("display_name"))
        if not material_id or not display_name:
            continue
        material_kind = _clean_text(attachment.get("kind"))
        media_class = _clean_text(attachment.get("media_class"))
        local_path = _clean_text(attachment.get("path")) or None
        node_kind = _clean_text(attachment.get("node_kind"))
        if (
            material_kind not in {
                "clipboard_text",
                "clipboard_image",
                "selected_text",
                "selected_image",
                "selected_web",
                "dragged_file",
                "dragged_folder",
            }
            or media_class not in {"text", "mixed_or_document", "audio", "video"}
            or node_kind not in {"file", "directory"}
        ):
            continue
        materials.append(
            AgentMaterial(
                material_id=material_id,
                display_name=display_name,
                material_kind=cast(MaterialKind, material_kind),
                media_class=cast(MaterialMediaClass, media_class),
                local_path=local_path,
                node_kind=cast(MaterialNodeKind, node_kind),
            )
        )
    return tuple(materials)


def recent_materials_from_paths(
    foreground_context: dict[str, object],
    requested_paths: list[str],
) -> tuple[tuple[AgentMaterial, ...], RecentMaterialValidationFailure | None]:
    """把模型逐字选择的近期路径校验为新的任务级材料。

    路径代表当前实时工作对象，不比较内容、大小或修改时间。任意一项机械检查失败时
    立即返回首个失败，调用方必须在 Background ``prepare()`` 前整体拒绝。

    Args:
        foreground_context: 当前轮冻结的产品态势，携带近期路径能力快照。
        requested_paths: 已先排除本轮自动附挂路径的候选列表。

    Returns:
        全部成功时返回新任务级材料和空失败；否则返回空材料和首个安全失败。
    """

    allowlist: dict[str, dict[str, object]] = {}
    raw_records = foreground_context.get("recent_materials")
    if isinstance(raw_records, list):
        for record in raw_records:
            if not isinstance(record, dict):
                continue
            key = normalize_material_path(record.get("path"))
            node_kind = _clean_text(record.get("node_kind"))
            media_class = _clean_text(record.get("media_class"))
            material_kind = _clean_text(record.get("material_kind"))
            if (
                key
                and _clean_text(record.get("display_name"))
                and node_kind in {"file", "directory"}
                and media_class in {"text", "mixed_or_document", "audio", "video"}
                and material_kind in {
                    "clipboard_text",
                    "clipboard_image",
                    "selected_text",
                    "selected_image",
                    "selected_web",
                    "dragged_file",
                    "dragged_folder",
                }
            ):
                allowlist[key] = record

    materials: list[AgentMaterial] = []
    seen: set[str] = set()
    for raw_path in requested_paths:
        key = normalize_material_path(raw_path)
        if not key or key in seen:
            continue
        seen.add(key)
        record = allowlist.get(key)
        if record is None:
            display_name = (
                PurePath(raw_path.strip().replace("\\", "/")).name
                or "这份材料"
            )
            return (), RecentMaterialValidationFailure(
                reason="recent_material_not_registered",
                display_name=display_name,
                detail=f"“{display_name}”已经不在我当前记得的材料里了，请重新提供。",
            )

        display_name = _clean_text(record.get("display_name")) or "未命名材料"
        registered_path = _clean_text(record.get("path"))
        expected_kind = _clean_text(record.get("node_kind"))
        candidate = Path(registered_path)
        status = inspect_recent_material_path(
            candidate,
            expected_node_kind=cast(MaterialNodeKind, expected_kind),
        )
        if status == "missing":
            return (), RecentMaterialValidationFailure(
                reason="recent_material_missing",
                display_name=display_name,
                detail=f"刚才的“{display_name}”已经不在原位置了，请重新提供。",
            )
        if status == "unreadable":
            return (), RecentMaterialValidationFailure(
                reason="recent_material_unreadable",
                display_name=display_name,
                detail=f"我现在读取不了“{display_name}”，请检查后重新提供。",
            )
        if status == "unsafe":
            return (), RecentMaterialValidationFailure(
                reason="recent_material_unsafe",
                display_name=display_name,
                detail=f"“{display_name}”现在指向了不安全的链接，请重新提供原始文件或目录。",
            )
        if status == "type_changed":
            return (), RecentMaterialValidationFailure(
                reason="recent_material_type_changed",
                display_name=display_name,
                detail=f"“{display_name}”现在已经不是原来的文件或目录了，请重新提供。",
            )

        materials.append(
            AgentMaterial(
                material_id=f"recent-{uuid4().hex}",
                display_name=display_name,
                material_kind=cast(
                    MaterialKind,
                    _clean_text(record.get("material_kind")),
                ),
                media_class=cast(
                    MaterialMediaClass,
                    _clean_text(record.get("media_class")),
                ),
                local_path=registered_path,
                node_kind=cast(MaterialNodeKind, expected_kind),
            )
        )
    return tuple(materials), None


def _clean_text(value: object) -> str:
    """把未知字段收窄为去除首尾空白的文字。

    Args:
        value: 尚未验证的协议字段。

    Returns:
        去除首尾空白的字符串；非字符串返回空字符串。
    """

    return value.strip() if isinstance(value, str) else ""
