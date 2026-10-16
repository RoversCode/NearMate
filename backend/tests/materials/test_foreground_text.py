from pathlib import Path

from companion.materials.foreground_text import load_foreground_text_batch
from companion.materials.models import MaterialAttachment, TurnMaterialContext


def test_small_text_batch_loads_all_materials_atomically(tmp_path: Path) -> None:
    """验证整批纯文本在总预算内时会完整进入 foreground。"""

    first = tmp_path / "first.txt"
    second = tmp_path / "second.md"
    first.write_text("甲乙", encoding="utf-8")
    second.write_text("丙丁", encoding="utf-8")

    result = load_foreground_text_batch(
        _turn(first, second),
        token_counter=len,
        max_tokens=5,
    )

    assert result.reason == "ready"
    assert result.token_count == 5
    assert [item.text for item in result.materials] == ["甲乙", "丙丁"]


def test_text_batch_over_token_limit_returns_no_partial_content(tmp_path: Path) -> None:
    """验证任一读取片段导致整批超限时不会暴露前面已读正文。"""

    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("1234", encoding="utf-8")
    second.write_text("56", encoding="utf-8")

    result = load_foreground_text_batch(
        _turn(first, second),
        token_counter=len,
        max_tokens=6,
        chunk_bytes=2,
    )

    assert result.reason == "token_limit_exceeded"
    assert result.materials == ()


def test_mixed_or_unreadable_batch_returns_only_failure_reason(tmp_path: Path) -> None:
    """验证非文本或非法 UTF-8 会让整批原子回退到材料事实摘要。"""

    text_path = tmp_path / "notes.txt"
    image_path = tmp_path / "image.png"
    invalid_path = tmp_path / "invalid.txt"
    text_path.write_text("可读正文", encoding="utf-8")
    image_path.write_bytes(b"image")
    invalid_path.write_bytes(b"\xff")

    mixed = load_foreground_text_batch(_turn(text_path, image_path), token_counter=len)
    unreadable = load_foreground_text_batch(_turn(text_path, invalid_path), token_counter=len)

    assert mixed.reason == "non_text_material"
    assert mixed.materials == ()
    assert unreadable.reason == "unreadable_text"
    assert unreadable.materials == ()


def test_selected_web_json_is_not_treated_as_plain_text(tmp_path: Path) -> None:
    """验证网页选区即使落成 JSON，也不会绕过 mixed 材料边界。"""

    path = tmp_path / "selection.json"
    path.write_text('{"parts": []}', encoding="utf-8")
    attachment = MaterialAttachment(
        material_id="material-web",
        turn_id="turn-test",
        kind="selected_web",
        source="selection_capture",
        display_name="网页选区",
        path=str(path),
        created_at="2026-07-12T00:00:00+08:00",
        node_kind="file",
        media_class="mixed_or_document",
    )
    turn = TurnMaterialContext(
        context_id="context-test",
        turn_id="turn-test",
        created_at="2026-07-12T00:00:00+08:00",
        attachments=[attachment],
    )

    result = load_foreground_text_batch(turn, token_counter=len)

    assert result.reason == "non_text_material"
    assert result.materials == ()


def test_text_batch_stops_at_hard_byte_limit(tmp_path: Path) -> None:
    """验证硬字节上限独立于 token 预算并且不会返回部分正文。"""

    path = tmp_path / "large.txt"
    path.write_text("a" * 9, encoding="utf-8")

    result = load_foreground_text_batch(
        _turn(path),
        token_counter=lambda _text: 1,
        max_bytes=8,
    )

    assert result.reason == "byte_limit_exceeded"
    assert result.materials == ()


def _turn(*paths: Path) -> TurnMaterialContext:
    """生成 foreground 文本装载测试使用的本轮材料上下文。"""

    attachments = [
        MaterialAttachment(
            material_id=f"material-{index}",
            turn_id="turn-test",
            kind="dragged_file",
            source="test",
            display_name=path.name,
            path=str(path),
            created_at="2026-07-12T00:00:00+08:00",
            node_kind="file",
            media_class="text",
        )
        for index, path in enumerate(paths)
    ]
    return TurnMaterialContext(
        context_id="context-test",
        turn_id="turn-test",
        created_at="2026-07-12T00:00:00+08:00",
        attachments=attachments,
    )
