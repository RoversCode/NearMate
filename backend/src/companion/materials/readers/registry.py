from __future__ import annotations

import os
import zipfile
from pathlib import Path
from xml.etree import ElementTree

from PIL import Image

from companion.filesystem.safety import (
    FileSystemNodeInspectionError,
    inspect_filesystem_node,
)
from companion.materials.image_encoder import MAX_DECODED_PIXELS
from companion.materials.readers.models import ReadMaterialPart, ReadMaterialResult
from companion.materials.type_registry import (
    AUDIO_SUFFIXES,
    IGNORED_DIRECTORY_NAMES,
    IMAGE_SUFFIXES,
    MATERIAL_TYPES,
    VIDEO_SUFFIXES,
)


MAX_DECOMPRESSED_DOCUMENT_BYTES = 8 * 1024 * 1024


class MaterialReaderRegistry:
    """按白名单读取文本、文档、图片、媒体路径和文件夹材料。"""

    @classmethod
    def default(cls) -> "MaterialReaderRegistry":
        """构造 V1 支持格式的默认 Reader 注册表。"""

        return cls()

    def read(self, path: Path) -> ReadMaterialResult:
        """读取单个文件或递归读取目录。

        Args:
            path: 用户材料的本地入口。

        Returns:
            保持稳定文件顺序的材料片段和工作量统计。
        """

        try:
            node_snapshot = inspect_filesystem_node(path)
        except FileNotFoundError:
            return ReadMaterialResult(ignored_file_count=1, ignored_reasons={"missing_or_symlink": 1})
        except FileSystemNodeInspectionError:
            return ReadMaterialResult(ignored_file_count=1, ignored_reasons={"unreadable_file": 1})
        if node_snapshot.is_linked_or_reparse:
            return ReadMaterialResult(ignored_file_count=1, ignored_reasons={"missing_or_symlink": 1})
        if node_snapshot.node_kind == "directory":
            return self._read_folder(path)
        if node_snapshot.node_kind != "file" or not MATERIAL_TYPES.is_supported_path(path):
            return ReadMaterialResult(ignored_file_count=1, ignored_reasons={"unsupported_extension": 1})
        if node_snapshot.size == 0:
            return ReadMaterialResult(ignored_file_count=1, ignored_reasons={"unreadable_file": 1})
        return self._read_file(path, display_name=path.name)

    def _read_folder(self, root: Path) -> ReadMaterialResult:
        """完整扫描目录，过滤无效入口但不截断可读文件。"""

        candidates: list[tuple[Path, str, int]] = []
        ignored_reasons: dict[str, int] = {}
        for current_root, directory_names, file_names in os.walk(root, followlinks=False):
            current_path = Path(current_root)
            kept_directories: list[str] = []
            for directory_name in sorted(directory_names, key=str.casefold):
                directory_path = current_path / directory_name
                if directory_name.casefold() in IGNORED_DIRECTORY_NAMES or directory_name.startswith("."):
                    _bump(ignored_reasons, "hidden_or_build_path")
                    continue
                try:
                    directory_snapshot = inspect_filesystem_node(directory_path)
                except FileNotFoundError:
                    _bump(ignored_reasons, "missing_or_symlink")
                    continue
                except FileSystemNodeInspectionError:
                    _bump(ignored_reasons, "unreadable_file")
                    continue
                if directory_snapshot.is_linked_or_reparse:
                    _bump(ignored_reasons, "missing_or_symlink")
                elif directory_snapshot.node_kind == "directory":
                    kept_directories.append(directory_name)
                else:
                    _bump(ignored_reasons, "unreadable_file")
            directory_names[:] = kept_directories

            for file_name in sorted(file_names, key=str.casefold):
                file_path = current_path / file_name
                if file_name.startswith("."):
                    _bump(ignored_reasons, "hidden_or_build_path")
                    continue
                if not MATERIAL_TYPES.is_supported_path(file_path):
                    _bump(ignored_reasons, "unsupported_extension")
                    continue
                try:
                    file_snapshot = inspect_filesystem_node(file_path)
                except FileNotFoundError:
                    _bump(ignored_reasons, "missing_or_symlink")
                    continue
                except FileSystemNodeInspectionError:
                    _bump(ignored_reasons, "unreadable_file")
                    continue
                if file_snapshot.is_linked_or_reparse:
                    _bump(ignored_reasons, "missing_or_symlink")
                    continue
                if file_snapshot.node_kind != "file":
                    _bump(ignored_reasons, "unreadable_file")
                    continue
                candidates.append(
                    (
                        file_path,
                        file_path.relative_to(root).as_posix(),
                        file_snapshot.size,
                    )
                )

        local_total_bytes = sum(size_bytes for _, _, size_bytes in candidates)
        results: list[ReadMaterialResult] = []
        for file_path, relative_name, _ in candidates:
            result = self._read_file(file_path, display_name=relative_name)
            if not result.readable:
                _bump(ignored_reasons, "unreadable_file")
                continue
            results.append(result)

        parts = [part for result in results for part in result.parts]
        return ReadMaterialResult(
            parts=parts,
            local_file_count=sum(result.local_file_count for result in results),
            visual_unit_count=sum(result.visual_unit_count for result in results),
            local_total_bytes=sum(result.local_total_bytes for result in results),
            ignored_file_count=sum(ignored_reasons.values()),
            ignored_reasons=ignored_reasons,
            readable=bool(results),
        )

    def _read_file(self, path: Path, *, display_name: str) -> ReadMaterialResult:
        """按文件类型读取一个本地文件。"""

        try:
            size_bytes = path.stat().st_size
            suffix = path.suffix.lower()
            # 音视频材料只产出路径引用，不在材料 Reader 中解码或提取正文。
            if suffix in AUDIO_SUFFIXES:
                return ReadMaterialResult(
                    parts=[ReadMaterialPart(kind="audio", display_name=display_name, path=str(path))],
                    local_file_count=1,
                    local_total_bytes=size_bytes,
                    readable=True,
                )
            if suffix in VIDEO_SUFFIXES:
                return ReadMaterialResult(
                    parts=[ReadMaterialPart(kind="video", display_name=display_name, path=str(path))],
                    local_file_count=1,
                    local_total_bytes=size_bytes,
                    readable=True,
                )
            if suffix in IMAGE_SUFFIXES:
                with Image.open(path) as image:
                    if image.width * image.height > MAX_DECODED_PIXELS:
                        return ReadMaterialResult()
                    if (image.format or "").upper() not in {"PNG", "JPEG", "WEBP"}:
                        return ReadMaterialResult()
                    image.verify()
                return ReadMaterialResult(
                    parts=[ReadMaterialPart(kind="image", display_name=display_name, path=str(path))],
                    local_file_count=1,
                    visual_unit_count=1,
                    local_total_bytes=size_bytes,
                    readable=True,
                )
            if suffix == ".pdf":
                page_count = _pdf_page_count(path)
                return ReadMaterialResult(
                    parts=[ReadMaterialPart(kind="pdf", display_name=display_name, path=str(path))],
                    local_file_count=1,
                    visual_unit_count=page_count,
                    local_total_bytes=size_bytes,
                    readable=page_count > 0,
                )
            if suffix == ".docx":
                text = _read_docx(path)
            else:
                text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError, ValueError, RuntimeError, KeyError, zipfile.BadZipFile, ElementTree.ParseError):
            return ReadMaterialResult()

        if not text.strip():
            return ReadMaterialResult()
        return ReadMaterialResult(
            parts=[ReadMaterialPart(kind="text", display_name=display_name, text=text)],
            local_file_count=1,
            local_total_bytes=size_bytes,
            readable=True,
        )


def _read_docx(path: Path) -> str:
    """从 DOCX 主文档 XML 中提取按段落排列的纯文本。"""

    with zipfile.ZipFile(path) as archive:
        document_info = archive.getinfo("word/document.xml")
        if document_info.flag_bits & 0x1 or document_info.file_size > MAX_DECOMPRESSED_DOCUMENT_BYTES:
            raise ValueError("DOCX 主文档超过安全读取限制。")
        with archive.open(document_info) as document_file:
            document_xml = document_file.read(MAX_DECOMPRESSED_DOCUMENT_BYTES + 1)
        if len(document_xml) > MAX_DECOMPRESSED_DOCUMENT_BYTES:
            raise ValueError("DOCX 主文档超过安全读取限制。")
        if b"<!DOCTYPE" in document_xml.upper() or b"<!ENTITY" in document_xml.upper():
            raise ValueError("DOCX 主文档包含不允许的实体声明。")
        root = ElementTree.fromstring(document_xml)
    paragraphs: list[str] = []
    for paragraph in root.iter("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p"):
        text = "".join(
            node.text or ""
            for node in paragraph.iter("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t")
        ).strip()
        if text:
            paragraphs.append(text)
    return "\n".join(paragraphs)


def _pdf_page_count(path: Path) -> int:
    """读取 PDF 总页数，失败时由调用方标记为不可读。"""

    import fitz  # type: ignore[import-not-found]

    with fitz.open(str(path)) as document:
        return len(document)
def _bump(counter: dict[str, int], reason: str) -> None:
    """累加一个目录过滤原因。"""

    counter[reason] = counter.get(reason, 0) + 1
