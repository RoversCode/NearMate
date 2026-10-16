from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from companion.environment.models import FrozenEnvironmentSnapshot
from companion.materials.foreground_images import load_foreground_visual_input
from companion.materials.foreground_images import VisualEvidence
from companion.materials.foreground_images import bound_image_evidence
from companion.observability.prompt_audit_sanitizer import sanitize_chat_messages
from companion.prompt.visual_input import append_visual_input


def _png() -> bytes:
    """生成一张能被真实编码器读取的小图片。"""

    output = BytesIO()
    Image.new("RGB", (12, 8), "blue").save(output, format="PNG")
    return output.getvalue()


def _snapshot(value: bytes | None) -> FrozenEnvironmentSnapshot:
    """构造带稳定时间的来源桌面快照。"""

    return FrozenEnvironmentSnapshot("screen-a", "2026-09-13T10:00:00+08:00", "编辑器", "image/png", value, None)


def _material(path: Path, *, kind: str = "dragged_file") -> dict[str, str]:
    """模拟请求入口已经认领的一项材料。"""

    return {"kind": kind, "path": str(path), "display_name": path.name, "material_id": path.stem}


def test_screen_and_image_materials_have_distinct_sources_and_ignore_documents(tmp_path: Path) -> None:
    """桌面、用户图片与文档必须保留不同的产品语义。"""

    photo = tmp_path / "photo.png"
    photo.write_bytes(_png())
    document = tmp_path / "report.pdf"
    document.write_bytes(b"not-an-image")
    result = load_foreground_visual_input({"attachments": [_material(photo), _material(document)]}, _snapshot(_png()))

    assert [item.source for item in result.images] == ["desktop", "material"]
    assert result.screen_available
    assert not result.limitations
    assert result.image_materials[0].path == str(photo)
    content = append_visual_input("这两张图分别是什么？", result)
    assert [block["type"] for block in content].count("image_url") == 2
    assert "desktop_view" in str(content) and "user_image_material" in str(content)
    assert str(photo) not in str(content)


def test_frozen_pixels_survive_source_file_changes(tmp_path: Path) -> None:
    """后续轮必须复用原始像素，不能再次读取已经被覆盖的文件。"""

    photo = tmp_path / "photo.png"
    photo.write_bytes(_png())
    result = load_foreground_visual_input({"attachments": [_material(photo)]}, _snapshot(_png()))
    first = append_visual_input("问题", result)
    photo.write_bytes(b"changed")
    assert append_visual_input("问题", result) == first
    recovery = append_visual_input("问题", result, is_recovery=True)
    assert "不代表恢复时的实时桌面" in str(recovery)
    assert [b for b in recovery if b["type"] == "image_url"] == [b for b in first if b["type"] == "image_url"]


@pytest.mark.parametrize("screenshot", [None, b"broken-png"])
def test_unavailable_or_invalid_screen_never_claims_available_pixels(screenshot: bytes | None) -> None:
    """窗口标题和图片元数据存在也不能冒充可读取的屏幕。"""

    result = load_foreground_visual_input(None, _snapshot(screenshot))
    assert not result.screen_available
    assert not result.images
    assert result.limitations


def test_invalid_image_does_not_drop_valid_image(tmp_path: Path) -> None:
    """单图损坏只影响这一张，用户仍可获得其它图片的回答。"""

    bad = tmp_path / "bad.png"
    good = tmp_path / "good.png"
    bad.write_bytes(b"broken")
    good.write_bytes(_png())
    result = load_foreground_visual_input({"attachments": [_material(bad), _material(good)]}, None)
    assert len(result.images) == 1
    assert result.images[0].label == good.name
    assert any(bad.name in item for item in result.limitations)


def test_count_and_byte_budgets_bound_visual_input(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """图片数量和字节预算都必须在请求发送前生效。"""

    path = tmp_path / "image.png"
    path.write_bytes(_png())
    monkeypatch.setattr("companion.materials.foreground_images.MAX_VISUAL_IMAGES", 1)
    result = load_foreground_visual_input({"attachments": [_material(path)]}, _snapshot(_png()))
    assert len(result.images) == 1 and result.screen_available
    assert any("数量" in item for item in result.limitations)
    monkeypatch.setattr("companion.materials.foreground_images.MAX_VISUAL_ENCODED_BYTES", 1)
    assert not load_foreground_visual_input(None, _snapshot(_png())).images


def test_directory_and_linked_nodes_are_not_decoded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """材料路径被替换成目录或链接后不能继续按图片读取。"""

    from types import SimpleNamespace

    path = tmp_path / "image.png"
    path.write_bytes(_png())
    monkeypatch.setattr("companion.materials.foreground_images.inspect_filesystem_node", lambda _p: SimpleNamespace(node_kind="file", is_linked_or_reparse=True, size=10))
    result = load_foreground_visual_input({"attachments": [_material(path)]}, None)
    assert not result.images


def test_prompt_audit_redacts_native_images() -> None:
    """原生视觉请求审计只保存摘要，不能落盘保存像素。"""

    result = load_foreground_visual_input(None, _snapshot(_png()))
    messages = [{"role": "user", "content": append_visual_input("看图", result)}]
    audit = str(sanitize_chat_messages(messages))
    assert result.images[0].data_url not in audit
    assert "sha256" in audit and "image/png" in audit


def test_clipboard_image_reference_preserves_kind_without_display_extension(tmp_path: Path) -> None:
    """显示名没有扩展名的剪贴板图仍能在派生时重新校验。"""

    path = tmp_path / "managed.png"
    path.write_bytes(_png())
    material = {**_material(path, kind="clipboard_image"), "display_name": "剪贴板图片"}
    frozen = load_foreground_visual_input({"attachments": [material]}, None, report_missing_screen=False)
    reference = frozen.image_materials[0].to_payload()
    assert reference["kind"] == "clipboard_image"
    assert load_foreground_visual_input({"attachments": [reference]}, None, report_missing_screen=False).images
    path.write_bytes(b"now-corrupt")
    reloaded = load_foreground_visual_input({"attachments": [reference]}, None, report_missing_screen=False)
    assert not reloaded.images and reloaded.limitations


def test_dragged_image_uses_path_extension_when_display_name_has_none(tmp_path: Path) -> None:
    """拖拽图片的展示名无后缀时，仍按真实路径识别图片。"""

    path = tmp_path / "renamed-image.png"
    path.write_bytes(_png())
    result = load_foreground_visual_input(
        {"attachments": [_material(path) | {"display_name": "截图"}]},
        None,
    )
    assert len(result.images) == 1


def test_merged_evidence_is_bounded_after_deduplication(monkeypatch: pytest.MonkeyPatch) -> None:
    """父任务图片合并后仍受同一上限约束，优先保留当前输入，标签随首次出现保留。"""

    monkeypatch.setattr("companion.materials.foreground_images.MAX_VISUAL_IMAGES", 2)
    current = VisualEvidence("desktop", "桌面截图，采集于 2026-09-13 10:00", "current")
    duplicate = VisualEvidence("material", "重复标签.png", "current")
    old_a = VisualEvidence("material", "旧图a.png", "old-a")
    old_b = VisualEvidence("material", "旧图b.png", "old-b")
    images, limitations = bound_image_evidence([current, duplicate, old_a, old_b])
    assert images == (current, old_a)
    assert limitations
