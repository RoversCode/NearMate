from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from companion.character_cards.models import CharacterCardStoreError, load_character_card_definition
from companion.character_cards.store import CharacterCardStore


def test_missing_user_cards_are_seeded_from_read_only_install_assets(tmp_path: Path) -> None:
    seed_dir = tmp_path / "seed"
    (seed_dir / "huohua").mkdir(parents=True)
    (seed_dir / "huohua" / "card.yaml").write_text(
        "meta:\n  card_id: huohua\n  name: 火花\n  title: 默认\n  voice_binding:\n    streamvox_model: qwen3-tts-clone-0.6b-gguf\n    role_name: huohua\n    reference_audio_path: voice/reference.wav\n    prompt_text: 你好\n",
        encoding="utf-8",
    )

    store = CharacterCardStore(tmp_path / "user-cards", seed_dir=seed_dir)
    store.ensure_seed_assets()

    assert store.load_active_card_id() == "huohua"
    assert (tmp_path / "user-cards" / "huohua" / "card.yaml").exists()


def test_voice_setup_can_load_existing_card_with_retired_scene_tool(tmp_path: Path) -> None:
    """旧用户卡片里的读图工具已退休，但语音初始化仍须读取当前角色。"""

    source_path = Path(__file__).parents[2] / "data" / "character-cards" / "huohua" / "card.yaml"
    payload = yaml.safe_load(source_path.read_text(encoding="utf-8"))
    legacy_example = payload["voiceprint"]["streamvox_examples"]["generic"]["items"][0]
    legacy_example["expected_tool"] = "inspect_current_scene"
    cards_dir = tmp_path / "user-cards"
    card_path = cards_dir / "huohua" / "card.yaml"
    card_path.parent.mkdir(parents=True)
    card_path.write_text(yaml.safe_dump(payload, allow_unicode=True), encoding="utf-8")

    # 当前接口仍须严格拒绝已退休工具；只有磁盘上的历史卡片可以迁移。
    with pytest.raises(CharacterCardStoreError, match="角色卡结构无效"):
        load_character_card_definition(payload)

    card = CharacterCardStore(cards_dir).load_active_card()

    assert card.meta.card_id == "huohua"
    assert card.voiceprint.streamvox_examples.generic.items[0].expected_tool is None
    assert yaml.safe_load(card_path.read_text(encoding="utf-8"))["voiceprint"]["streamvox_examples"]["generic"]["items"][0]["expected_tool"] == "inspect_current_scene"
