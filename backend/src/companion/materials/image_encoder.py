from __future__ import annotations

import base64
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageOps

from companion.filesystem.safety import FileSystemNodeInspectionError, inspect_filesystem_node
from companion.materials.type_registry import IMAGE_SUFFIXES


MAX_LONG_EDGE_PX = 1600
MAX_DECODED_PIXELS = 40_000_000
MAX_IMAGE_SOURCE_BYTES = 20 * 1024 * 1024


class ImageEncodingError(ValueError):
    """表示本地图片无法安全转换为模型输入。"""


class ModelImageEncoder:
    """把本地图片归一化为成本受控的模型data URL。"""

    def encode_path(self, path: Path) -> str:
        """读取、定向、缩放并编码一张本地图片。

        Args:
            path: PNG、JPEG 或 WebP 本地文件。

        Returns:
            保持合适格式的图片 data URL。

        Raises:
            ImageEncodingError: 图片不可读、格式不支持或解码尺寸过大。
        """

        try:
            snapshot = inspect_filesystem_node(path)
            if (
                not path.is_absolute()
                or path.suffix.lower() not in IMAGE_SUFFIXES
                or snapshot.node_kind != "file"
                or snapshot.is_linked_or_reparse
                or not 0 < snapshot.size <= MAX_IMAGE_SOURCE_BYTES
            ):
                raise ImageEncodingError("图片入口不符合普通文件和大小限制。")
            with path.open("rb") as stream:
                raw = stream.read(MAX_IMAGE_SOURCE_BYTES + 1)
            if len(raw) > MAX_IMAGE_SOURCE_BYTES:
                raise ImageEncodingError("图片读取时超过大小限制。")
            return self.encode_bytes(raw)
        except ImageEncodingError:
            raise
        except (OSError, ValueError, FileSystemNodeInspectionError, Image.DecompressionBombError) as exception:
            raise ImageEncodingError(f"图片无法读取：{path}") from exception

    def encode_bytes(self, value: bytes) -> str:
        """把内存图片应用同一归一化策略。

        Args:
            value: PDF 渲染器等内部来源生成的图片字节。

        Returns:
            归一化后的图片 data URL。
        """

        try:
            with Image.open(BytesIO(value)) as source:
                return self._encode_source(source)
        except ImageEncodingError:
            raise
        except (OSError, ValueError, Image.DecompressionBombError) as exception:
            raise ImageEncodingError("图片字节无法读取。") from exception

    def _encode_source(self, source: Image.Image) -> str:
        """对已解码 Pillow 图片执行方向、像素和格式策略。"""

        # 先拦掉解码后像素过大的图片，避免超大图在后续旋转、缩放和编码阶段吃掉过多内存。
        if source.width * source.height > MAX_DECODED_PIXELS:
            raise ImageEncodingError("图片解码尺寸超过 40MP 限制。")
        # 统一按 EXIF 方向纠正图片，避免手机照片这类“像素没转、方向写在元数据里”的输入被看反。
        image = ImageOps.exif_transpose(source)
        output_format = _output_format(source.format)
        # 长边超过阈值时先缩小，再编码成 data URL，控制传给模型的体积和成本。
        if max(image.size) > MAX_LONG_EDGE_PX:
            image.thumbnail((MAX_LONG_EDGE_PX, MAX_LONG_EDGE_PX), Image.Resampling.LANCZOS)
        return _encode_image(image, output_format)


def _output_format(source_format: str | None) -> str:
    """把 Pillow 来源格式限制为产品支持的三种输出。"""

    normalized = (source_format or "").upper()
    if normalized in {"PNG", "JPEG", "WEBP"}:
        return normalized
    raise ImageEncodingError("图片格式不受支持。")


def _encode_image(image: Image.Image, output_format: str) -> str:
    """按格式策略编码图片，并返回 data URL。"""

    output = BytesIO()
    if output_format == "PNG":
        image.save(output, format="PNG", optimize=True)
        mime_type = "image/png"
    elif output_format == "WEBP":
        image.convert("RGB").save(output, format="WEBP", quality=85, method=6)
        mime_type = "image/webp"
    else:
        image.convert("RGB").save(output, format="JPEG", quality=85, optimize=True)
        mime_type = "image/jpeg"
    encoded = base64.b64encode(output.getvalue()).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"
