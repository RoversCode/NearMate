from companion.runtime_memory.models import RuntimeMemoryMessage
from companion.runtime_memory.summary_prompt import format_runtime_messages_for_summary
from companion.runtime_memory.token_accounting import protocol_message_token_text
from companion.materials.foreground_view import (
    build_foreground_turn_material_summary,
    render_material_history_note,
)
from companion.materials.models import MaterialAttachment, TurnMaterialContext
from companion.prompt.builder import _current_input_content
from companion.prompt.material_prompt import render_recent_materials_block
from companion.session.context import SessionContext


def test_current_text_materials_are_escaped_and_not_written_to_history() -> None:
    """验证当前正文只进入临时输入，历史仅保留安全材料事实。"""

    current_input = _current_input_content(
        "解释材料",
        [{
            "material_id": "material-a",
            "display_name": "notes.txt",
            "text": "<正文>&内容",
        }],
    )
    history_note = render_material_history_note({
        "attachment_count": 1,
        "omitted_count": 0,
        "items": [{
            "display_name": "notes.txt",
            "material_kind": "文本材料",
            "image_path": "G:\\secret\\notes.txt",
        }],
    })
    context = SessionContext(token_counter=len)
    message = context.record_user_message("解释材料", llm_context_note=history_note)

    assert "<current_turn_text_materials>" in current_input
    assert "\n  <text_material>\n" in current_input
    assert "\n    <display_name>notes.txt</display_name>\n" in current_input
    assert "\n    <content>&lt;正文&gt;&amp;内容</content>\n" in current_input
    assert "&lt;正文&gt;&amp;内容" in current_input
    assert "<正文>&内容" not in message.llm_visible_content()
    assert "notes.txt" in message.llm_visible_content()
    assert "G:\\secret" not in message.llm_visible_content()
    summary_lines = format_runtime_messages_for_summary(
        (
            RuntimeMemoryMessage(
                message_id=message.message_id or 1,
                role=message.role,
                content=message.content,
                token_count=message.token_count,
                created_at=message.created_at,
                llm_context_note=message.llm_context_note,
            ),
        )
    )
    assert "notes.txt" in summary_lines[1]
    assert message.token_count == len(protocol_message_token_text(
        role="user",
        content=message.content,
        llm_context_note=message.llm_context_note,
    ))


def _attachment(
    material_id: str,
    path: str,
    kind: str,
    *,
    managed_asset_id: str | None = None,
) -> MaterialAttachment:
    """构造指定受管状态与类型的测试材料。"""

    return MaterialAttachment(
        material_id=material_id,
        turn_id="turn-a",
        kind=kind,  # type: ignore[arg-type]
        source="drag_drop",
        display_name=path.replace("\\", "/").rsplit("/", 1)[-1],
        path=path,
        created_at="2026-08-22T10:00:00+08:00",
        node_kind="file",
        media_class="mixed_or_document",
        managed_asset_id=managed_asset_id,
    )


def test_turn_summary_never_contains_cross_turn_authorization_paths() -> None:
    """当前轮摘要只保存语义材料事实，不承担跨轮路径授权。"""

    summary = build_foreground_turn_material_summary(
        TurnMaterialContext(
            context_id="context-a",
            turn_id="turn-a",
            created_at="2026-08-22T10:00:00+08:00",
            attachments=[
                _attachment("material-a", "D:/录音/新录音 3.m4a", "dragged_file"),
                _attachment(
                    "material-b",
                    "G:/managed/clipboard.png",
                    "clipboard_image",
                    managed_asset_id="asset-a",
                ),
            ],
        )
    )

    items = summary["items"]
    assert isinstance(items, list)
    assert "path" not in items[0]
    assert "path" not in items[1]
    # 受管图片的当前轮直读入口不受影响，只在当前轮图片通道可见。
    assert "image_path" not in items[1]


def test_history_note_keeps_material_semantics_without_any_local_path() -> None:
    """历史附注只记录名称和类型，即使输入摘要误带路径也必须剥离。"""

    note = render_material_history_note({
        "attachment_count": 2,
        "omitted_count": 0,
        "items": [
            {
                "display_name": "新录音 3.m4a",
                "material_kind": "文件材料",
                "path": "D:\\录音\\新录音 3.m4a",
            },
            {
                "display_name": "截图.png",
                "material_kind": "图片材料",
                "image_path": "G:\\managed\\截图.png",
            },
        ],
    })

    assert "<past_turn_materials>" in note
    assert "新录音 3.m4a" in note
    assert "D:\\录音" not in note
    assert "G:\\managed" not in note


def test_recent_materials_block_is_the_only_cross_turn_path_projection() -> None:
    """近期材料块应提供可逐字选择的路径，并明确默认读写与禁止猜测。"""

    block = render_recent_materials_block(
        {
            "recent_materials": [
                {
                    "display_name": "app.py",
                    "node_kind": "file",
                    "media_class": "text",
                    "path": "D:\\project\\app.py",
                }
            ]
        }
    )

    assert block is not None
    assert "<recent_materials>" in block
    assert "D:\\project\\app.py" in block
    assert "material_paths" in block
    assert "对该任务默认开放读写" in block
    assert "不得填写 URL" in block
