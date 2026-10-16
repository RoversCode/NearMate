import json
from pathlib import Path

import pytest

from companion.character_cards.store import CharacterCardStore
from companion.core.streamvox_example_console_service import (
    StreamVoxExampleConsoleService,
)
from companion.prompt.streamvox_example_generation import (
    INDEPENDENT_DELIVERABLE_EXAMPLE_REQUESTS,
    build_example_generation_messages,
)


CARDS_DIR = Path(__file__).parents[2] / "data" / "character-cards"


def test_example_generation_limits_deliverable_scene_to_independent_creation() -> None:
    """角色示例只示范无材料成果接单，不携带运行时材料状态。"""

    card = CharacterCardStore(CARDS_DIR).load_card("xiaolian")
    messages = build_example_generation_messages(card=card, target_model="generic")
    system_prompt = messages[0]["content"]

    assert "用户不依赖文件、附件、网页或长材料" in system_prompt
    assert "文章、古诗、标题、邮件、短翻译" in system_prompt
    assert all(request in system_prompt for request in INDEPENDENT_DELIVERABLE_EXAMPLE_REQUESTS)
    assert "expected_tool 填 start_background_work" in system_prompt
    assert "char 只写一句自然确认" in system_prompt
    assert "use_current_materials" not in system_prompt
    assert "material_action_policy" not in system_prompt
    assert "恰好生成 7 条" in system_prompt


@pytest.mark.parametrize("card_id", ["xiaolian", "yae-miko", "huohua"])
def test_builtin_example_sets_do_not_encode_material_routing(card_id: str) -> None:
    """内置角色卡示范动作交接与独立创作接单，不固化正式材料的动态路由。"""

    card = CharacterCardStore(CARDS_DIR).load_card(card_id)
    example_sets = [
        card.voiceprint.streamvox_examples.generic,
        *card.voiceprint.streamvox_examples.models.values(),
    ]
    forbidden_material_words = ("文件", "附件", "网页", "材料", "日志", "史料", "禀报")

    for example_set in example_sets:
        task_result_items = [
            item for item in example_set.items
            if item.expected_tool == "start_background_work"
        ]
        # 至少保留三个派单锚点；角色可以补充同类人格场景，不能把数量误当成材料路由约束。
        assert len(task_result_items) >= 3
        assert not any(
            word in item.user
            for item in example_set.items
            for word in forbidden_material_words
        )


def test_example_generation_normalizer_requires_and_keeps_exactly_seven_items() -> None:
    """示例服务必须拒绝数量偏差，并完整保存七条有效结果。"""

    service = object.__new__(StreamVoxExampleConsoleService)
    items = [
        {
            "title": f"场景 {index}",
            "user": (
                INDEPENDENT_DELIVERABLE_EXAMPLE_REQUESTS[0]
                if index == 3
                else f"用户请求 {index}"
            ),
            "char": f"角色回应 {index}",
            "expected_tool": {
                2: "start_background_work",
                3: "start_background_work",
                4: "lookup_current_fact",
                5: "analyze_deeply",
            }.get(index),
        }
        for index in range(1, 8)
    ]

    normalized = service._normalize_generated_examples(
        json.dumps({"items": items}, ensure_ascii=False),
        target_model="generic",
    )

    assert len(normalized.items) == 7
    assert normalized.items[2].expected_tool == "start_background_work"
    with pytest.raises(RuntimeError, match="恰好 7 条"):
        service._normalize_generated_examples(
            json.dumps({"items": items[:6]}, ensure_ascii=False),
            target_model="generic",
        )


def test_example_generation_normalizer_rejects_wrong_scene_action_structure() -> None:
    """生成结果即使数量正确，也不能把内容交付场景伪装成普通回复。"""

    service = object.__new__(StreamVoxExampleConsoleService)
    items = [
        {
            "title": f"场景 {index}",
            "user": (
                INDEPENDENT_DELIVERABLE_EXAMPLE_REQUESTS[0]
                if index == 3
                else f"用户请求 {index}"
            ),
            "char": f"角色回应 {index}",
            "expected_tool": {
                2: "start_background_work",
                4: "lookup_current_fact",
                5: "analyze_deeply",
            }.get(index),
        }
        for index in range(1, 8)
    ]

    with pytest.raises(RuntimeError, match="第 3 个场景工具结构无效"):
        service._normalize_generated_examples(
            json.dumps({"items": items}, ensure_ascii=False),
            target_model="generic",
        )


def test_example_generation_normalizer_rejects_material_dependent_deliverable_scene() -> None:
    """交付成果槽位不能把附件或网页请求固化进角色 few-shot。"""

    service = object.__new__(StreamVoxExampleConsoleService)
    items = [
        {
            "title": f"场景 {index}",
            "user": "总结我附带的长文档" if index == 3 else f"用户请求 {index}",
            "char": f"角色回应 {index}",
            "expected_tool": {
                2: "start_background_work",
                3: "start_background_work",
                4: "lookup_current_fact",
                5: "analyze_deeply",
            }.get(index),
        }
        for index in range(1, 8)
    ]

    with pytest.raises(RuntimeError, match="必须使用无材料创作请求"):
        service._normalize_generated_examples(
            json.dumps({"items": items}, ensure_ascii=False),
            target_model="generic",
        )
