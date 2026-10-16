"""执行近期图片的白名单校验和临时编码。"""

from __future__ import annotations

from pathlib import Path
from collections.abc import Callable
from typing import Any

from companion.filesystem.safety import FileSystemNodeInspectionError, inspect_filesystem_node
from companion.foreground_tools.inline_execution import InlineExecutionContext
from companion.materials.foreground_images import (
    FrozenVisualInput,
    ForegroundImageReference,
    VisualEvidence,
    MAX_VISUAL_SOURCE_BYTES,
)
from companion.materials.image_encoder import ImageEncodingError, ModelImageEncoder, MAX_IMAGE_SOURCE_BYTES
from companion.materials.type_registry import IMAGE_SUFFIXES
from companion.materials.recent_validation import inspect_recent_material_path


def execute_load_recent_image(
    arguments: dict[str, Any],
    *,
    foreground_context: dict[str, object],
    inline_execution_context: InlineExecutionContext,
    already_mounted_paths: frozenset[str] = frozenset(),
    already_mounted_data_urls: frozenset[str] = frozenset(),
    claim_mounted_image: Callable[[str], bool] | None = None,
) -> tuple[dict[str, object], FrozenVisualInput]:
    """校验并临时编码一张近期图片。

    Args:
        arguments: 模型传入的工具参数。
        foreground_context: 本轮冻结的近期材料白名单。
        inline_execution_context: 当前 Inline 调用的取消与截止门禁。
        already_mounted_paths: 当前请求链已经挂载过的路径。
        already_mounted_data_urls: 当前轮正式视觉输入已有的图片内容指纹。
        claim_mounted_image: 原子领取本请求唯一挂载名额的回调。

    Returns:
        可写回模型的安全载荷，以及不会写入协议的临时视觉证据。
    """

    material_path = arguments.get("material_path")
    if not isinstance(material_path, str) or not material_path.strip():
        return _failure("invalid_material_path", "近期图片路径无效。")
    # 路径必须逐字来自冻结清单；只用 strip 判断空值，不能把参数偷偷规范化后放行。
    requested_path = material_path
    recent_record = _find_recent_record(foreground_context, requested_path)
    if recent_record is None:
        return _failure("recent_material_not_registered", "这张图片当前不在我记得的近期材料里，请重新提供。")

    try:
        path = Path(str(recent_record.get("path") or ""))
    except (TypeError, ValueError):
        return _failure("recent_material_unsafe", "这张图片当前无法安全读取，请重新提供原始图片。")
    if not path.is_absolute():
        return _failure("recent_material_unsafe", "这张图片当前无法安全读取，请重新提供原始图片。")
    if str(path) in already_mounted_paths:
        return (
            {
                "state": "completed",
                "safe_message": "这张图片已经随本轮材料提供，可以直接理解。",
                "display_name": str(recent_record.get("display_name") or "未命名图片"),
            },
            FrozenVisualInput(),
        )
    if path.suffix.lower() not in IMAGE_SUFFIXES:
        return _failure("recent_material_not_image", "这项近期材料不是可直接理解的图片。")
    if str(recent_record.get("node_kind") or "") != "file":
        return _failure("recent_material_not_image", "这项近期材料不是普通图片文件。")
    status = inspect_recent_material_path(path, expected_node_kind="file")
    if status != "available":
        return _failure(
            f"recent_material_{status}",
            "这张图片当前无法读取，请重新提供原始图片。",
        )
    expected_size = recent_record.get("source_size")
    expected_mtime = recent_record.get("source_mtime_ns")
    try:
        current_stat = path.stat()
        if expected_size is not None and int(expected_size) != current_stat.st_size:
            return _failure("recent_image_changed", "这张图片在挂载前发生了变化，请重新提供原始图片。")
        if expected_mtime is not None and int(expected_mtime) != current_stat.st_mtime_ns:
            return _failure("recent_image_changed", "这张图片在挂载前发生了变化，请重新提供原始图片。")
    except (OSError, TypeError, ValueError):
        return _failure("recent_image_unreadable", "这张图片当前无法安全读取，请重新提供原始图片。")
    if inline_execution_context.cancellation_requested():
        return _failure("generation_cancelled", "当前回复已经取消，图片不会继续挂载。")

    try:
        snapshot = inspect_filesystem_node(path)
        if snapshot.size <= 0 or snapshot.size > min(MAX_IMAGE_SOURCE_BYTES, MAX_VISUAL_SOURCE_BYTES):
            raise ImageEncodingError("图片超过读取预算")
        with path.open("rb") as stream:
            raw = stream.read(min(MAX_IMAGE_SOURCE_BYTES, MAX_VISUAL_SOURCE_BYTES) + 1)
        if len(raw) != snapshot.size or len(raw) > min(MAX_IMAGE_SOURCE_BYTES, MAX_VISUAL_SOURCE_BYTES):
            raise ImageEncodingError("图片在读取期间发生变化")
        data_url = ModelImageEncoder().encode_bytes(raw)
    except (FileSystemNodeInspectionError, ImageEncodingError, OSError, ValueError):
        return _failure("recent_image_unreadable", "这张图片当前无法安全读取，请重新提供原始图片。")

    # 同一图片若已由本轮正式材料携带，只返回事实，不重复塞入图片块，并保留正式材料标签。
    if data_url in already_mounted_data_urls:
        return (
            {
                "state": "completed",
                "safe_message": "这张图片已经随本轮材料提供，可以直接理解。",
                "display_name": str(recent_record.get("display_name") or "未命名图片"),
            },
            FrozenVisualInput(),
        )
    if claim_mounted_image is not None and not claim_mounted_image(data_url):
        return _failure("recent_image_already_mounted", "本轮已经挂载过一张近期图片。")

    reference = ForegroundImageReference(
        str(recent_record.get("material_id") or ""),
        str(recent_record.get("display_name") or "未命名图片"),
        str(path),
        str(recent_record.get("material_kind") or "dragged_file"),
    )
    evidence = FrozenVisualInput(
        images=(VisualEvidence("material", reference.display_name, data_url),),
        image_materials=(reference,),
    )
    return (
        {
            "state": "completed",
            "safe_message": f"已把“{reference.display_name}”挂载到下一次图片理解输入。",
            "display_name": reference.display_name,
        },
        evidence,
    )


def _find_recent_record(
    foreground_context: dict[str, object],
    requested_path: str,
) -> dict[str, object] | None:
    """只从本轮冻结的近期材料清单中按原文字段匹配路径。"""

    records = foreground_context.get("recent_materials")
    if not isinstance(records, list):
        return None
    for record in records:
        if not isinstance(record, dict):
            continue
        if isinstance(record.get("path"), str) and record["path"] == requested_path:
            return record
    return None


def _failure(reason: str, safe_message: str) -> tuple[dict[str, object], FrozenVisualInput]:
    """构造不携带图片内容的安全失败结果。"""

    return (
        {"state": "failed", "reason": reason, "safe_message": safe_message},
        FrozenVisualInput(),
    )
