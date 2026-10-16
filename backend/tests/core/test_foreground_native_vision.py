from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from backend.tests.foreground_processor_factory import build_foreground_processor_for_test
from companion.character_cards.store import CharacterCardStore
from companion.environment.models import FrozenEnvironmentSnapshot
from companion.llm.foreground_models import ForegroundModelStreamChunk, ForegroundToolCall
from companion.materials.foreground_images import load_foreground_visual_input
from companion.prompt.builder import PromptBuildInput, PromptBuilder


CARD = CharacterCardStore(Path(__file__).parents[2] / "data" / "character-cards").load_card("xiaolian")


def _visual_input(tmp_path: Path):
    """准备一张桌面图和一张有真实文件的用户图。"""

    output = BytesIO()
    Image.new("RGB", (16, 8), "red").save(output, format="PNG")
    path = tmp_path / "image.png"
    path.write_bytes(output.getvalue())
    snapshot = FrozenEnvironmentSnapshot("screen", "2026-09-13T10:00:00+08:00", "窗口", "image/png", output.getvalue(), None)
    turn = {"attachments": [{"material_id": "image", "kind": "dragged_file", "path": str(path), "display_name": path.name}]}
    return path, snapshot, turn, load_foreground_visual_input(turn, snapshot)


def _image_urls(messages: list[dict]) -> list[str]:
    """从真实模型消息中提取视觉块，并检查图片只能出现在用户消息。"""

    urls = []
    for message in messages:
        if not isinstance(message["content"], list):
            continue
        for block in message["content"]:
            if block.get("type") == "image_url":
                assert message["role"] == "user"
                urls.append(block["image_url"]["url"])
    return urls


def test_first_call_followup_and_cognitive_receive_identical_frozen_images(tmp_path: Path) -> None:
    """文件在模型首轮返回前改变，后续模型和工具仍必须看到来源轮像素。"""

    path, snapshot, turn, frozen = _visual_input(tmp_path)
    requests = []
    received = []

    class Client:
        """只模拟模型选择工具和最终表达，不访问外部服务。"""

        def stream_message(self, **kwargs):
            """记录请求并在首轮之后修改来源文件。"""

            requests.append(kwargs["messages"])
            if len(requests) == 1:
                path.write_bytes(b"changed-after-freeze")
                return [ForegroundModelStreamChunk(kind="tool_calls", tool_calls=[ForegroundToolCall("lookup", "lookup_current_fact", {"brief": "核查图片外部信息"})])]
            return [ForegroundModelStreamChunk(kind="text_delta", text="暂时查不到更多资料。")]

    agent = build_foreground_processor_for_test(client=Client(), prompt_builder=PromptBuilder())

    def execute(**kwargs):
        """记录认知工具接收的像素，并模拟安全失败。"""

        received.append(kwargs["frozen_visual_input"])
        return ({"state": "rejected", "kind": "lookup_current_fact", "reason": "test_unavailable", "safe_message": "暂时查不到更多资料。"},)

    agent._execute_cognitive_tool_calls = execute
    try:
        events = list(agent.stream_user_text(
            "核查这张图里的信息", now=0, interaction_session_id="test", character_card=CARD,
            user_call_name="用户", user_self_intro="", user_extra_notes="",
            foreground_context={"active_turn_material_context": turn, "environment_snapshot": snapshot.to_payload()},
            environment_snapshot=snapshot, frozen_visual_input=frozen,
        ))
        assert len(requests) == 2
        assert _image_urls(requests[0]) == _image_urls(requests[1]) == [image.data_url for image in frozen.images]
        assert received == [frozen]
        assert "data:image" not in str([event.payload for event in events])
        assert "data:image" not in str(agent._session_context.recent_messages())
    finally:
        agent.shutdown()


@pytest.mark.parametrize("mode", ["system_notice", "timed_reminder_notice"])
def test_notice_prompt_never_uses_images_even_when_caller_supplies_them(tmp_path: Path, mode: str) -> None:
    """提醒表达不能顺便读取桌面或复用用户图片。"""

    _, _, _, frozen = _visual_input(tmp_path)
    messages = PromptBuilder().build(PromptBuildInput(current_user_text="提醒", character_card=CARD, request_mode=mode, frozen_visual_input=frozen,
        current_time="2026-09-13 10:00:00 UTC+08:00",
        foreground_context={"timed_reminder_notice": {"reminders": [{"content": "喝水", "trigger_at": "2026-09-13T10:00:00+08:00"}]}},
    ))
    assert not _image_urls(messages)


def test_recovery_marks_source_images_as_historical_view(tmp_path: Path) -> None:
    """恢复必须清楚标出来源视野，不可把旧图说成当前实时屏幕。"""

    _, snapshot, _, frozen = _visual_input(tmp_path)
    messages = PromptBuilder().build(PromptBuildInput(
        current_user_text="原始问题", character_card=CARD,
        request_mode="self_reflection_recovery", frozen_visual_input=frozen,
        foreground_context={"environment_snapshot": snapshot.to_payload()},
        self_reflection_recovery_instruction="仅恢复来源任务。",
    ))
    assert _image_urls(messages) == [image.data_url for image in frozen.images]
    assert "不代表恢复时的实时桌面" in str(messages)
