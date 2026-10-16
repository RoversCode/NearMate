"""按请求冻结桌面截图和用户提交的图片，不把像素写进历史或材料元数据。"""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Iterable
from pathlib import Path

from companion.environment.models import FrozenEnvironmentSnapshot
from companion.filesystem.safety import FileSystemNodeInspectionError, inspect_filesystem_node
from companion.materials.image_encoder import MAX_IMAGE_SOURCE_BYTES, ImageEncodingError, ModelImageEncoder
from companion.materials.models import TurnMaterialContext
from companion.materials.type_registry import IMAGE_SUFFIXES


# 为文本、工具声明和协议开销留出空间，不贴着厂商 48MiB 请求体上限发送。
MAX_VISUAL_IMAGES = 16
MAX_VISUAL_SOURCE_BYTES = 64 * 1024 * 1024
MAX_VISUAL_ENCODED_BYTES = 32 * 1024 * 1024


def bound_image_evidence(
    values: Iterable[VisualEvidence],
    *,
    deduplicate: bool = True,
) -> tuple[tuple[VisualEvidence, ...], tuple[str, ...]]:
    """对跨任务合并的图片证据再施加一次发送上限。

    按像素内容去重，来源标签随首次出现保留；同一像素换新标签再次出现时不重复占位。

    Args:
        values: 按本轮图片优先、旧证据在后的顺序排列的图片证据。
        deduplicate: 是否按图片内容去重；合并已有冻结输入时关闭，以保持来源轮原样。

    Returns:
        保序去重且没有超过发送上限的图片证据，以及被省略时的限制说明。
    """

    images: list[VisualEvidence] = []
    seen: set[str] = set()
    byte_count = 0
    omitted = False
    for value in values:
        if deduplicate and value.data_url in seen:
            continue
        seen.add(value.data_url)
        if len(images) >= MAX_VISUAL_IMAGES or byte_count + len(value.data_url) > MAX_VISUAL_ENCODED_BYTES:
            omitted = True
            continue
        images.append(value)
        byte_count += len(value.data_url)
    limitations = ("合并后的图片超过输入上限，部分旧证据未附带，不能假定这些图片可见。",) if omitted else ()
    return tuple(images), limitations


@dataclass(frozen=True, slots=True)
class VisualEvidence:
    """保存一张已经压缩且带来源标记的图片证据。

    来源标签必须随像素一起贯穿前台冻结、认知规划和 Cognitive 请求，
    否则执行模型无法区分桌面画面和用户主动提交的图片。

    Args:
        source: 桌面截图或用户主动提交的图片。
        label: 图片名称；桌面截图同时注明采集时间。
        data_url: 可以直接发送给模型的图片内容。
    """

    source: str
    label: str
    data_url: str

    @property
    def prompt_tag(self) -> str:
        """返回提示词中标记这张图片来源的 XML 标签名。

        Returns:
            桌面截图为 desktop_view，其余图片为 user_image_material。
        """

        return "desktop_view" if self.source == "desktop" else "user_image_material"


@dataclass(frozen=True, slots=True)
class ForegroundImageReference:
    """保存可在派生任务中重新校验的本地图片引用，不保存像素。

    Args:
        material_id: 来源正式材料编号。
        display_name: 用户提交时的图片名称。
        path: 来源材料路径，后续读取必须重新检查文件。
        kind: 原始采集来源，保留没有文件扩展名的剪贴板图片身份。
    """

    material_id: str
    display_name: str
    path: str
    kind: str = "dragged_file"

    def to_payload(self) -> dict[str, str]:
        """返回派生任务可以重新交给材料装载器的入口。

        Returns:
            本地图片材料引用，不包含编码内容。
        """

        return {"kind": self.kind, "material_id": self.material_id,
                "display_name": self.display_name, "path": self.path}


@dataclass(frozen=True, slots=True)
class FrozenVisualInput:
    """保存首轮、工具续写和恢复共同使用的瞬态视觉证据。

    Args:
        images: 已成功装载的图片，桌面截图排在最前。
        limitations: 未能读取的图片和固定失败说明，不包含文件路径。
        image_materials: 成功装载的本地图片引用，供派生任务重新校验。
    """

    images: tuple[VisualEvidence, ...] = ()
    limitations: tuple[str, ...] = ()
    image_materials: tuple[ForegroundImageReference, ...] = ()

    @property
    def screen_available(self) -> bool:
        """判断本轮是否确实装载了桌面像素。

        Returns:
            图片中含桌面截图时返回真。
        """

        return any(image.source == "desktop" for image in self.images)


def merge_frozen_visual_inputs(
    *inputs: FrozenVisualInput,
) -> FrozenVisualInput:
    """合并多个临时视觉输入并统一执行图片上限。

    Args:
        inputs: 按优先顺序排列的视觉输入；前面的输入在去重时保留来源标签。

    Returns:
        合并、去重并受预算限制的视觉输入。
    """

    images: list[VisualEvidence] = []
    limitations: list[str] = []
    image_materials: list[ForegroundImageReference] = []
    seen_data_urls: set[str] = set()
    for input_index, visual_input in enumerate(inputs):
        for image in visual_input.images:
            # 来源轮本身的图片保持原样；仅对后续挂载证据按内容去重。
            if input_index > 0 and image.data_url in seen_data_urls:
                continue
            images.append(image)
            seen_data_urls.add(image.data_url)
        limitations.extend(visual_input.limitations)
        image_materials.extend(visual_input.image_materials)
    bounded_images, bound_limitations = bound_image_evidence(images, deduplicate=False)
    deduplicated_materials = tuple(dict.fromkeys(image_materials))
    return FrozenVisualInput(
        images=bounded_images,
        limitations=tuple(dict.fromkeys((*limitations, *bound_limitations))),
        image_materials=deduplicated_materials,
    )


def load_foreground_visual_input(
    turn: TurnMaterialContext | dict[str, object] | None,
    environment_snapshot: FrozenEnvironmentSnapshot | None,
    *,
    report_missing_screen: bool = True,
) -> FrozenVisualInput:
    """有界读取本轮直接图片并冻结编码结果，文档和文件夹仍由 Background 读取。

    Args:
        turn: 请求已认领的正式材料，不能传入全局最近材料。
        environment_snapshot: 请求已认领的桌面截图，不再重新截屏。
        report_missing_screen: 普通用户轮报告截图缺失；只重读父材料时不宣称需要新截图。

    Returns:
        可直接传输的不可变视觉输入；单图失败不会丢掉其它有效图片。
    """

    images: list[VisualEvidence] = []
    image_materials: list[ForegroundImageReference] = []
    limitations: list[str] = []
    encoder = ModelImageEncoder()
    source_bytes = 0
    encoded_bytes = 0
    candidates: list[tuple[str, str, bytes | Path, ForegroundImageReference | None]] = []
    if environment_snapshot is not None and environment_snapshot.screenshot_bytes:
        candidates.append((
            "desktop",
            f"桌面截图，采集于 {environment_snapshot.captured_at}",
            environment_snapshot.screenshot_bytes,
            None,
        ))
    elif report_missing_screen:
        limitations.append("本轮未取得桌面截图；不能据此声称看到了当前屏幕。")

    attachments = (
        [item.to_payload() for item in turn.attachments]
        if isinstance(turn, TurnMaterialContext)
        else turn.get("attachments", []) if isinstance(turn, dict) else []
    )
    for item in attachments if isinstance(attachments, list) else []:
        if not isinstance(item, dict):
            continue
        label = str(item.get("display_name") or "未命名图片")
        kind = item.get("kind")
        path = Path(str(item.get("path") or ""))
        # mixed_or_document 同时包含图片和文档，必须按真实入口筛选，不能尝试解码 PDF。
        if kind not in {"clipboard_image", "selected_image"} and not (
            kind == "dragged_file" and path.suffix.lower() in IMAGE_SUFFIXES
        ):
            continue
        candidates.append(("material", label, path, ForegroundImageReference(
            str(item.get("material_id") or ""), label, str(path), str(kind),
        )))

    for index, (source, label, value, reference) in enumerate(candidates):
        if index >= MAX_VISUAL_IMAGES:
            limitations.append("本轮图片数量超过直接视觉输入上限，剩余图片没有读取。")
            break
        remaining_bytes = min(MAX_IMAGE_SOURCE_BYTES, MAX_VISUAL_SOURCE_BYTES - source_bytes)
        try:
            if isinstance(value, Path):
                snapshot = inspect_filesystem_node(value)
                if (
                    not value.is_absolute()
                    or value.suffix.lower() not in IMAGE_SUFFIXES
                    or snapshot.node_kind != "file"
                    or snapshot.is_linked_or_reparse
                    or snapshot.size <= 0
                ):
                    raise ImageEncodingError("图片入口不是可读取的普通文件")
                if snapshot.size > remaining_bytes:
                    raise ImageEncodingError("图片超过读取预算")
                # 读取时再次限制字节，避免文件在检查后增长造成无界内存分配。
                with value.open("rb") as stream:
                    raw = stream.read(remaining_bytes + 1)
            else:
                raw = value
            if not raw or len(raw) > remaining_bytes:
                raise ImageEncodingError("图片超过读取预算或没有内容")
            source_bytes += len(raw)
            data_url = encoder.encode_bytes(raw)
            if encoded_bytes + len(data_url) > MAX_VISUAL_ENCODED_BYTES:
                raise ImageEncodingError("图片超过发送预算")
            encoded_bytes += len(data_url)
            images.append(VisualEvidence(source, label, data_url))
            if reference is not None:
                image_materials.append(reference)
        except (ImageEncodingError, FileSystemNodeInspectionError, OSError, ValueError):
            limitations.append(f"{label}：图片未能安全装载，没有可用像素。")

    return FrozenVisualInput(tuple(images), tuple(limitations), tuple(image_materials))
