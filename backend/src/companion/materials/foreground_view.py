from __future__ import annotations

from html import escape
from pathlib import Path

from companion.materials.failure_reasons import describe_material_failure

from companion.materials.models import MaterialAttachment, TurnMaterialContext
from companion.materials.type_registry import IMAGE_SUFFIXES


MAX_FOREGROUND_MATERIAL_ITEMS = 15
DIRECT_IMAGE_KINDS = {"clipboard_image", "selected_image"}
DIRECT_IMAGE_SUFFIXES = IMAGE_SUFFIXES


def build_foreground_turn_material_summary(turn: TurnMaterialContext | None) -> dict[str, object]:
    """生成 foreground 可读的本轮材料事实摘要。

    Args:
        turn: 当前冻结的本轮材料上下文。

    Returns:
        有界的材料名称和类型，不包含跨轮授权路径、预算或正文信息。
    """

    attachments = turn.attachments if turn is not None else []
    failure_items = [
        {
            "display_name": str(item.get("display_name") or "未命名材料"),
            "material_kind": f"无法读取（{describe_material_failure(str(item.get('reason') or 'unreadable_file'))}）",
        }
        for item in (turn.preflight_failures if turn is not None else ())
    ]
    visible_items = ([ _visible_material_item(item) for item in attachments] + failure_items)[:MAX_FOREGROUND_MATERIAL_ITEMS]
    attachment_count = len(attachments) + len(failure_items)
    return {
        "attachment_count": attachment_count,
        "shown_count": len(visible_items),
        "omitted_count": max(0, attachment_count - len(visible_items)),
        "items": visible_items,
    }


def render_material_history_note(summary: object) -> str:
    """把材料事实摘要渲染为不含可执行路径的历史附注。

    Args:
        summary: 当前 turn 的 foreground 材料事实摘要。

    Returns:
        可进入后续 LLM 历史的名称与类型 XML；没有材料时返回空字符串。
    """

    if not isinstance(summary, dict):
        return ""
    raw_items = summary.get("items")
    items = raw_items if isinstance(raw_items, list) else []
    visible_items = [item for item in items if isinstance(item, dict)]
    attachment_count = summary.get("attachment_count")
    if not isinstance(attachment_count, int) or attachment_count <= 0:
        return ""

    lines = ["<past_turn_materials>", f"<attachment_count>{attachment_count}</attachment_count>"]
    for item in visible_items:
        lines.extend(
            [
                "<material>",
                f"<display_name>{escape(str(item.get('display_name') or '未命名材料'))}</display_name>",
                f"<material_kind>{escape(str(item.get('material_kind') or '材料'))}</material_kind>",
            ]
        )
        lines.append("</material>")
    omitted_count = summary.get("omitted_count")
    if isinstance(omitted_count, int) and omitted_count > 0:
        lines.append(f"<omitted_count>{omitted_count}</omitted_count>")
    lines.append("</past_turn_materials>")
    return "\n".join(lines)


def _visible_material_item(attachment: MaterialAttachment) -> dict[str, str]:
    """把单个附件转换成不携带预算结论的 foreground 事实。

    Args:
        attachment: 当前 Turn 中的完整材料附件。

    Returns:
        材料名称和产品类型；路径仍由正式材料上下文提供给执行器。
    """

    image_path = _direct_image_path(attachment)
    visible = {
        "display_name": attachment.display_name,
        "material_kind": "图片材料" if image_path else _material_kind_text(attachment.kind, attachment.display_name),
    }
    return visible


def _direct_image_path(attachment: MaterialAttachment) -> str | None:
    """返回附件本身对应的直接图片入口。

    Args:
        attachment: 需要判断是否为直接图片的附件。

    Returns:
        本地图片路径；文档内部图片等非直接入口返回 None。
    """

    if attachment.kind in DIRECT_IMAGE_KINDS:
        return attachment.path
    if attachment.kind == "dragged_file" and Path(attachment.display_name).suffix.lower() in DIRECT_IMAGE_SUFFIXES:
        return attachment.path
    return None


def _material_kind_text(kind: str, display_name: str) -> str:
    """根据产品 kind 和展示名推断用户可理解的材料类别。

    Args:
        kind: 材料产品类型。
        display_name: 面向用户的材料名称。

    Returns:
        可放入 foreground 摘要的材料类别文案。
    """

    if kind == "selected_web":
        return "网页选区"
    if kind == "dragged_folder":
        return "文件夹材料"
    if kind in {"clipboard_image", "selected_image"}:
        return "图片材料"
    if kind in {"clipboard_text", "selected_text"}:
        return "文本材料"
    suffix = Path(display_name).suffix.lower()
    if suffix == ".pdf":
        return "PDF 文档"
    if suffix in {".png", ".jpg", ".jpeg", ".webp"}:
        return "图片材料"
    return "文件材料" if kind == "dragged_file" else "材料"
