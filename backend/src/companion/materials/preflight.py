from __future__ import annotations

import os
import time
import zipfile
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from companion.filesystem.safety import (
    FileSystemNodeInspectionError,
    inspect_filesystem_node,
)
from companion.materials.models import MaterialAttachment, MaterialWorkloadProfile
from companion.materials.type_registry import (
    AUDIO_SUFFIXES,
    IGNORED_DIRECTORY_NAMES,
    IMAGE_SUFFIXES,
    MATERIAL_TYPES,
    VIDEO_SUFFIXES,
)
from companion.materials.web_selection import WebSelectionError, read_web_selection


# 材料预检预算，超限时不暴露产物工具。
MAX_LOCAL_FILES = 5  # 本地文件数。
MAX_VISUAL_UNITS = 15  # 视觉输入数。
MAX_TEXT_ONLY_BYTES = 1536 * 1024  # 纯文本字节数。
MAX_MIXED_TEXT_BYTES = 1024 * 1024  # 混合材料文本字节数。
MAX_MIXED_TOTAL_BYTES = 10 * 1024 * 1024  # 混合材料总字节数。
MAX_VISITED_ENTRIES = 1024  # 文件夹访问项数。
MAX_PREFLIGHT_SECONDS = 2.0  # 预检秒数。
MAX_TEXT_SAMPLE_BYTES = 64 * 1024  # 文本采样字节数。
MAX_DECOMPRESSED_DOCUMENT_BYTES = 8 * 1024 * 1024  # 文档解压字节数。
MAX_DECODED_PIXELS = 40_000_000  # 图片像素数。
MAX_PREFLIGHT_WORKERS = 4  # 预检并发数。
_PREFLIGHT_EXECUTOR = ThreadPoolExecutor(max_workers=MAX_PREFLIGHT_WORKERS, thread_name_prefix="material-preflight")


@dataclass(frozen=True)
class MaterialPreflightFailure:
    """描述预检阶段可向用户解释的单项失败。"""

    material_id: str
    display_name: str
    reason: str

    def to_payload(self) -> dict[str, str]:
        """转换为材料摘要和 Desktop 事件载荷。"""

        return {"material_id": self.material_id, "display_name": self.display_name, "reason": self.reason}


@dataclass(frozen=True)
class MaterialPreflightResult:
    """描述一个 generation 的异步产物预检结果。"""

    status: str
    profile: MaterialWorkloadProfile
    failures: tuple[MaterialPreflightFailure, ...]


@dataclass(frozen=True)
class _ItemFacts:
    """保存单个材料入口在预检阶段提取出的轻量统计事实。

    Args:
        local_files: 当前入口折算后的本地文件数量。
        visual_units: 当前入口包含的视觉单元数量，例如图片张数或 PDF 页数。
        text_bytes: 当前入口文本内容对应的字节数。
        total_bytes: 当前入口原始文件或结构化载荷的总字节数。
        mixed: 当前入口是否属于图片、文档、网页选区或其他混合材料。
        ignored_files: 递归文件夹时被跳过或读取失败的文件数量。
        size_limited_bytes: 需要进入既有文本、图片和文档内容预算的字节数。
            音视频只传递路径，因此该值为零。
    """

    local_files: int = 0
    visual_units: int = 0
    text_bytes: int = 0
    total_bytes: int = 0
    mixed: bool = False
    ignored_files: int = 0
    size_limited_bytes: int = 0


def inspect_material_batch(
    attachments: list[MaterialAttachment],
    cancelled: Callable[[], bool] | None = None,
) -> MaterialPreflightResult:
    """在固定资源边界内检查一批材料的产物适用性。

    Args:
        attachments: 当前 generation 的稳定材料引用快照。

    Returns:
        可原子提交的预检状态、画像和单项失败。
    """

    is_cancelled = cancelled or (lambda: False)
    deadline = time.monotonic() + MAX_PREFLIGHT_SECONDS
    facts: list[_ItemFacts] = []
    failures: list[MaterialPreflightFailure] = []
    # 每轮最多派发四项，既利用本地磁盘并行，也避免一次性把整批昂贵检查塞进线程池。
    for offset in range(0, len(attachments), MAX_PREFLIGHT_WORKERS):
        if is_cancelled() or time.monotonic() >= deadline:
            return _result("indeterminate", facts, failures, "preflight_timeout")
        batch = attachments[offset:offset + MAX_PREFLIGHT_WORKERS]
        # 预检
        futures = [
            _PREFLIGHT_EXECUTOR.submit(_inspect_item, attachment, deadline=deadline, cancelled=is_cancelled)
            for attachment in batch
        ]
        for attachment, future in zip(batch, futures, strict=True):
            try:
                item_facts = future.result(timeout=max(0.0, deadline - time.monotonic()))
            except _PreflightFailure as exception:
                failures.append(MaterialPreflightFailure(attachment.material_id, attachment.display_name, exception.reason))
                continue
            except (TimeoutError, _PreflightIndeterminate):
                for remaining in futures:
                    remaining.cancel()
                return _result("indeterminate", facts, failures, "preflight_indeterminate")
            facts.append(item_facts)
            # 统计目前的材料规模
            blocking_reason = _blocking_reason(facts)
            if blocking_reason is not None:
                for remaining in futures:
                    remaining.cancel()
                return _result("over_limit", facts, failures, blocking_reason)
    return _result("ready", facts, failures, None)


def _inspect_item(
    attachment: MaterialAttachment,
    *,
    deadline: float,
    cancelled: Callable[[], bool] | None = None,
) -> _ItemFacts:
    """检查单个材料引用，并提取预检所需的最小事实。

    Args:
        attachment: 当前要预检的单个材料附件。
        deadline: 本轮预检允许执行到的单调时钟截止时间。
        cancelled: 可选取消信号；返回 True 时立即中止本次检查。

    Returns:
        当前材料对应的轻量事实，包括字节数、视觉单元数和是否属于混合材料。

    Raises:
        _PreflightFailure: 材料路径失效、网页选区损坏或底层文件不可用时抛出。
        _PreflightIndeterminate: 当前检查在开始前已被取消时抛出。
    """

    # 单项检查进入前先尊重上层取消信号，避免继续消耗本轮有限预检预算。
    if cancelled is not None and cancelled():
        raise _PreflightIndeterminate()
    path = Path(attachment.path)
    try:
        node_snapshot = inspect_filesystem_node(path)
    except FileNotFoundError as exception:
        raise _PreflightFailure("missing_or_symlink") from exception
    except FileSystemNodeInspectionError as exception:
        raise _PreflightFailure("unreadable_file") from exception
    if node_snapshot.is_linked_or_reparse:
        raise _PreflightFailure("missing_or_symlink")
    # 网页选区不是普通文件语义，而是结构化 JSON，需要单独统计正文与远程图片单元。
    if attachment.kind == "selected_web":
        try:
            selection = read_web_selection(path)
        except WebSelectionError as exception:
            raise _PreflightFailure("invalid_web_selection") from exception
        text_bytes = selection.text_byte_count()
        visual_units = sum(part.kind == "image_url" for part in selection.parts)
        selection_size = path.stat().st_size
        return _ItemFacts(
            visual_units=visual_units,
            text_bytes=text_bytes,
            total_bytes=selection_size,
            mixed=True,
            size_limited_bytes=selection_size,
        )
    # 目录入口会递归聚合其子文件；普通文件则走单文件轻量检查。
    if node_snapshot.node_kind == "directory":
        return _inspect_folder(path, deadline=deadline, cancelled=cancelled)
    if node_snapshot.node_kind != "file":
        raise _PreflightFailure("unreadable_file")
    return _inspect_file(path)


def _inspect_folder(
    root: Path,
    *,
    deadline: float,
    cancelled: Callable[[], bool] | None = None,
) -> _ItemFacts:
    """递归检查文件夹材料，并在可确定超限时尽早返回聚合结果。

    Args:
        root: 当前要预检的文件夹根目录。
        deadline: 本轮预检允许执行到的单调时钟截止时间。
        cancelled: 可选取消信号；返回 True 时立即中止本轮检查。

    Returns:
        当前文件夹内所有可用文件聚合后的轻量事实。

    Raises:
        _PreflightFailure: 文件夹为空，或最终没有任何可接纳文件时抛出。
        _PreflightIndeterminate: 遍历超时、被取消，或目录项数量超出安全上限时抛出。
    """

    local_files = visual_units = text_bytes = total_bytes = visited = ignored_files = 0
    size_limited_bytes = 0
    mixed = True
    for current_root, directory_names, file_names in os.walk(root, followlinks=False):
        # 文件夹预检必须受时间和取消信号约束，避免深目录把整轮预检拖死。
        if (cancelled is not None and cancelled()) or time.monotonic() >= deadline:
            raise _PreflightIndeterminate()
        # 这里只保留可继续递归的正常目录，避免 Windows Junction 被 os.walk 当成普通目录。
        safe_directory_names: list[str] = []
        for directory_name in sorted(directory_names, key=str.casefold):
            if (
                directory_name.startswith(".")
                or directory_name.casefold() in IGNORED_DIRECTORY_NAMES
            ):
                continue
            directory_path = Path(current_root) / directory_name
            try:
                directory_snapshot = inspect_filesystem_node(directory_path)
            except (FileNotFoundError, FileSystemNodeInspectionError):
                continue
            if (
                directory_snapshot.node_kind == "directory"
                and not directory_snapshot.is_linked_or_reparse
            ):
                safe_directory_names.append(directory_name)
        directory_names[:] = safe_directory_names
        visited += len(directory_names)
        if visited > MAX_VISITED_ENTRIES:
            raise _PreflightIndeterminate()
        # 遍历文件
        for file_name in sorted(file_names, key=str.casefold):
            if (cancelled is not None and cancelled()) or time.monotonic() >= deadline:
                raise _PreflightIndeterminate()
            visited += 1
            if visited > MAX_VISITED_ENTRIES:
                raise _PreflightIndeterminate()
            path = Path(current_root) / file_name
            # 不支持的文件、隐藏文件和重定向节点只计入忽略数，不让单个坏文件拖垮整批文件夹。
            if file_name.startswith(".") or not MATERIAL_TYPES.is_supported_path(path):
                ignored_files += 1
                continue
            try:
                file_snapshot = inspect_filesystem_node(path)
            except (FileNotFoundError, FileSystemNodeInspectionError):
                ignored_files += 1
                continue
            if (
                file_snapshot.node_kind != "file"
                or file_snapshot.is_linked_or_reparse
            ):
                ignored_files += 1
                continue
            try:
                item = _inspect_file(path)
            except _PreflightFailure:
                # 文件夹内单个文件读取失败属于局部失败，继续扫描其余候选文件即可。
                ignored_files += 1
                continue
            local_files += item.local_files
            visual_units += item.visual_units
            text_bytes += item.text_bytes
            total_bytes += item.total_bytes
            size_limited_bytes += item.size_limited_bytes
            mixed = mixed or item.mixed
            aggregate = _ItemFacts(
                local_files=local_files,
                visual_units=visual_units,
                text_bytes=text_bytes,
                total_bytes=total_bytes,
                mixed=True,
                ignored_files=ignored_files,
                size_limited_bytes=size_limited_bytes,
            )
            # 文件夹一旦已经明确超预算，就没必要继续深入遍历。
            if _blocking_reason([aggregate]) is not None:
                return aggregate
    if local_files == 0:
        raise _PreflightFailure("empty_folder")
    return _ItemFacts(
        local_files=local_files,
        visual_units=visual_units,
        text_bytes=text_bytes,
        total_bytes=total_bytes,
        mixed=mixed,
        ignored_files=ignored_files,
        size_limited_bytes=size_limited_bytes,
    )


def _inspect_file(path: Path) -> _ItemFacts:
    """按文件类型提取最小必要事实，而不加载完整正文。

    Args:
        path: 当前要检查的单个文件路径。

    Returns:
        单个文件对应的轻量事实，包括文件数、视觉单元数和估算字节数。

    Raises:
        _PreflightFailure: 文件不存在、不可读、格式非法或内容为空时抛出。
    """

    suffix = path.suffix.lower()
    try:
        size = path.stat().st_size
        if size <= 0:
            raise _PreflightFailure("empty_file")
        # 音视频在产品内部只作为路径引用。具体容器、编码和可处理性由后台任务选择的
        # Skill 或脚本判断，材料预检不读取内容，也不把文件大小计入旧内容预算。
        if suffix in AUDIO_SUFFIXES | VIDEO_SUFFIXES:
            return _ItemFacts(
                local_files=1,
                total_bytes=size,
                mixed=True,
                size_limited_bytes=0,
            )
        # 图片只验证格式、像素和基础可解码性，不在预检阶段做完整转码。
        if suffix in IMAGE_SUFFIXES:
            with Image.open(path) as image:
                if image.width * image.height > MAX_DECODED_PIXELS:
                    raise _PreflightFailure("image_too_large")
                if (image.format or "").upper() not in {"PNG", "JPEG", "WEBP"}:
                    raise _PreflightFailure("invalid_image")
            return _ItemFacts(
                local_files=1,
                visual_units=1,
                total_bytes=size,
                mixed=True,
                size_limited_bytes=size,
            )
        # PDF 只读取页数，用页数近似视觉单元，避免在预检阶段渲染页面。
        if suffix == ".pdf":
            import fitz  # type: ignore[import-not-found]
            with fitz.open(str(path)) as document:
                if document.needs_pass or len(document) == 0:
                    raise _PreflightFailure("invalid_or_encrypted_pdf")
                return _ItemFacts(
                    local_files=1,
                    visual_units=len(document),
                    total_bytes=size,
                    mixed=True,
                    size_limited_bytes=size,
                )
        # DOCX 只检查主文档 XML 是否存在且解压后不过大，不读取完整正文。
        if suffix == ".docx":
            with zipfile.ZipFile(path) as archive:
                info = archive.getinfo("word/document.xml")
                if info.flag_bits & 0x1 or info.file_size > MAX_DECOMPRESSED_DOCUMENT_BYTES:
                    raise _PreflightFailure("invalid_or_encrypted_docx")
            return _ItemFacts(
                local_files=1,
                text_bytes=info.file_size,
                total_bytes=size,
                mixed=True,
                size_limited_bytes=size,
            )
        # 纯文本类文件只抽样首尾内容，快速确认编码和非空性。
        _validate_text_sample(path, size)
        return _ItemFacts(
            local_files=1,
            text_bytes=size,
            total_bytes=size,
            mixed=False,
            size_limited_bytes=size,
        )
    # 已经被上面业务分支归一化过的稳定失败，直接原样向上抛出即可。
    except _PreflightFailure:
        raise
    # 路径在预检过程中消失，说明材料已被修改或删除。
    except FileNotFoundError as exception:
        raise _PreflightFailure("missing_path") from exception
    # 文件存在但当前进程没有读取权限，统一映射为权限问题。
    except PermissionError as exception:
        raise _PreflightFailure("permission_denied") from exception
    # 文本抽样解码失败时，按非法文本编码处理。
    except UnicodeError as exception:
        raise _PreflightFailure("invalid_text_encoding") from exception
    # Pillow 的解压炸弹保护命中时，视为图片体量超出安全上限。
    except Image.DecompressionBombError as exception:
        raise _PreflightFailure("image_too_large") from exception
    # DOCX 结构缺失或压缩包损坏时，都统一视为无效或不可解密文档。
    except (KeyError, zipfile.BadZipFile) as exception:
        raise _PreflightFailure("invalid_or_encrypted_docx") from exception
    # 其余底层异常按文件类型归一化，确保上层只消费稳定原因码而不是实现细节。
    except (OSError, ValueError, RuntimeError) as exception:
        if suffix in IMAGE_SUFFIXES:
            raise _PreflightFailure("invalid_image") from exception
        if suffix == ".pdf":
            raise _PreflightFailure("invalid_or_encrypted_pdf") from exception
        if suffix == ".docx":
            raise _PreflightFailure("invalid_or_encrypted_docx") from exception
        raise _PreflightFailure("io_error") from exception


def _validate_text_sample(path: Path, size: int) -> None:
    """抽样文本首尾内容，快速验证编码可读且正文非空。

    Args:
        path: 要抽样检查的文本文件路径。
        size: 文件总字节数，用于决定是否需要回读尾部样本。

    Raises:
        _PreflightFailure: 文本解码失败或去空白后没有有效内容时抛出。
    """

    # 只采样首尾可以显著降低大文本预检成本，同时仍能发现编码错误和空白文件。
    with path.open("rb") as stream:
        head = stream.read(MAX_TEXT_SAMPLE_BYTES)
        tail = b""
        if size > MAX_TEXT_SAMPLE_BYTES:
            stream.seek(max(0, size - MAX_TEXT_SAMPLE_BYTES))
            tail = stream.read(MAX_TEXT_SAMPLE_BYTES)
    sample = (head + tail).decode("utf-8")
    if not sample.strip():
        raise _PreflightFailure("empty_text")


def _blocking_reason(facts: list[_ItemFacts]) -> str | None:
    """根据累计事实返回第一个可确定的预算阻断原因。

    Args:
        facts: 已完成预检的单项或聚合事实列表。

    Returns:
        命中的稳定阻断原因码；未超预算时返回 None。
    """

    local_files = sum(item.local_files for item in facts)
    visuals = sum(item.visual_units for item in facts)
    text_bytes = sum(item.text_bytes for item in facts)
    size_limited_bytes = sum(item.size_limited_bytes for item in facts)
    mixed = any(item.mixed for item in facts)
    # 这里返回的是第一个已被确定命中的硬边界，供上层直接映射为 over_limit。
    if local_files > MAX_LOCAL_FILES:
        return "too_many_files"
    if visuals > MAX_VISUAL_UNITS:
        return "too_many_visual_units"
    if mixed and text_bytes > MAX_MIXED_TEXT_BYTES:
        return "mixed_text_size_exceeded"
    if mixed and size_limited_bytes > MAX_MIXED_TOTAL_BYTES:
        return "mixed_total_size_exceeded"
    if not mixed and text_bytes > MAX_TEXT_ONLY_BYTES:
        return "text_size_exceeded"
    return None


def _result(status: str, facts: list[_ItemFacts], failures: list[MaterialPreflightFailure], reason: str | None) -> MaterialPreflightResult:
    """把累计事实封装成对上层稳定的预检结果对象。

    Args:
        status: 本轮预检的最终状态。
        facts: 已成功聚合的材料事实列表。
        failures: 需要向上层暴露的单项失败明细。
        reason: 可选的稳定阻断原因码。

    Returns:
        包含状态、工作量画像和失败明细的预检结果。
    """

    blocking_reasons = (reason,) if reason else ()
    # 只有 ready 且确实拿到了有效事实时，才允许后台工作读取这批材料。
    profile = MaterialWorkloadProfile(
        task_result_material_eligible=status == "ready" and bool(facts),
        local_file_count=sum(item.local_files for item in facts),
        visual_unit_count=sum(item.visual_units for item in facts),
        text_byte_count=sum(item.text_bytes for item in facts),
        local_total_bytes=sum(item.total_bytes for item in facts),
        contains_mixed_material=any(item.mixed for item in facts),
        ignored_file_count=sum(item.ignored_files for item in facts),
        blocking_reasons=blocking_reasons,
    )
    return MaterialPreflightResult(status, profile, tuple(failures))


class _PreflightFailure(RuntimeError):
    """表示单项材料可解释的预检失败。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _PreflightIndeterminate(RuntimeError):
    """表示预检在资源边界内无法得出结论。"""
