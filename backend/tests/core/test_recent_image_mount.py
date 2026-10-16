from __future__ import annotations

from pathlib import Path

from PIL import Image

from companion.foreground_tools.catalog.service import ForegroundToolService
from companion.foreground_tools.inline_execution import InlineExecutionContext
from companion.foreground_tools.recent_image.execution import execute_load_recent_image


def _image(path: Path) -> None:
    """写入一张最小可解码 PNG。"""

    Image.new("RGB", (8, 8), "red").save(path, format="PNG")


def _context(path: Path) -> dict[str, object]:
    """构造只包含冻结近期材料的请求上下文。"""

    return {
        "recent_materials": [
            {
                "material_id": "m1",
                "path": str(path),
                "display_name": path.name,
                "node_kind": "file",
                "material_kind": "dragged_file",
            }
        ]
    }


def test_recent_image_tool_schema_and_catalog_are_registered() -> None:
    """普通轮目录应包含近期图片挂载工具。"""

    service = ForegroundToolService()
    schema_names = {item["function"]["name"] for item in service.tool_schemas()}
    catalog_names = {item.name for item in service.tool_catalog()}
    assert "load_recent_image_for_foreground" in schema_names
    assert "load_recent_image_for_foreground" in catalog_names
    recovery_names = {
        definition.schema["function"]["name"]
        for definition in service.self_reflection_tools()
    }
    assert "load_recent_image_for_foreground" in recovery_names


def test_recent_image_mount_returns_visual_side_channel_without_base64(tmp_path: Path) -> None:
    """成功结果只含安全事实，图片编码留在运行期视觉通道。"""

    path = tmp_path / "商品.png"
    _image(path)
    payload, visual_input = execute_load_recent_image(
        {"material_path": str(path)},
        foreground_context=_context(path),
        inline_execution_context=InlineExecutionContext(None),
    )
    assert payload["state"] == "completed"
    assert "base64" not in str(payload)
    assert visual_input.images and visual_input.images[0].source == "material"
    assert visual_input.images[0].data_url.startswith("data:image/")


def test_recent_image_mount_rejects_non_whitelisted_and_non_image_paths(tmp_path: Path) -> None:
    """任意路径和冻结清单中的非图片都必须安全拒绝。"""

    path = tmp_path / "note.txt"
    path.write_text("hello", encoding="utf-8")
    payload, visual_input = execute_load_recent_image(
        {"material_path": str(path)},
        foreground_context=_context(path),
        inline_execution_context=InlineExecutionContext(None),
    )
    assert payload["state"] == "failed"
    assert not visual_input.images

    missing = tmp_path / "missing.png"
    payload, visual_input = execute_load_recent_image(
        {"material_path": str(missing)},
        foreground_context=_context(path),
        inline_execution_context=InlineExecutionContext(None),
    )
    assert payload["state"] == "failed"
    assert not visual_input.images


def test_recent_image_mount_rejects_second_concurrent_claim(tmp_path: Path) -> None:
    """同一请求第二次领取挂载名额必须失败。"""

    path = tmp_path / "one.png"
    _image(path)
    claimed: set[str] = set()

    def claim_once(value: str) -> bool:
        """模拟请求级原子挂载名额。"""

        if value in claimed:
            return False
        claimed.add(value)
        return True

    first, first_input = execute_load_recent_image(
        {"material_path": str(path)},
        foreground_context=_context(path),
        inline_execution_context=InlineExecutionContext(None),
        claim_mounted_image=claim_once,
    )
    assert first["state"] == "completed" and first_input.images
    second, second_input = execute_load_recent_image(
        {"material_path": str(path)},
        foreground_context=_context(path),
        inline_execution_context=InlineExecutionContext(None),
        claim_mounted_image=claim_once,
    )
    assert second["state"] == "failed"
    assert not second_input.images


def test_recent_image_mount_rejects_changed_registered_file(tmp_path: Path) -> None:
    """登记后文件被替换时不能把新内容冒充原图片。"""

    path = tmp_path / "changed.png"
    _image(path)
    stat = path.stat()
    context = _context(path)
    context["recent_materials"][0]["source_size"] = str(stat.st_size)
    context["recent_materials"][0]["source_mtime_ns"] = str(stat.st_mtime_ns)
    path.write_bytes(b"different-content")
    payload, visual_input = execute_load_recent_image(
        {"material_path": str(path)},
        foreground_context=context,
        inline_execution_context=InlineExecutionContext(None),
    )
    assert payload["state"] == "failed"
    assert payload["reason"] == "recent_image_changed"
    assert not visual_input.images
