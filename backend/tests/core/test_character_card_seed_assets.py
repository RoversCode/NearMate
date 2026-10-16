from pathlib import Path

from companion.character_cards.store import CharacterCardStore


def test_missing_user_cards_are_seeded_from_release_resources(tmp_path: Path) -> None:
    seed_dir = tmp_path / "release" / "character-cards" / "huohua"
    seed_dir.mkdir(parents=True)
    (seed_dir / "card.yaml").write_text("meta:\n  card_id: huohua\n", encoding="utf-8")

    cards = CharacterCardStore(tmp_path / "user-cards", seed_dir=tmp_path / "release" / "character-cards")
    cards.ensure_seed_assets()

    assert (tmp_path / "user-cards" / "huohua" / "card.yaml").read_text(encoding="utf-8") == "meta:\n  card_id: huohua\n"
    assert cards.load_active_card_id() == "huohua"


def test_default_wake_audio_paths_resolve_inside_the_release_card() -> None:
    """默认角色采用便携文件名，干净 checkout 不引用开发者电脑绝对路径。"""
    import json
    from companion.wake_response.service import WakeResponseService

    card_directory = Path(__file__).parents[2] / "data/character-cards/huohua"
    audio_directory = (card_directory / "wake-response/audio").resolve()
    payload = json.loads((card_directory / "wake-response/config.json").read_text(encoding="utf-8"))
    service = WakeResponseService(asset_dir=audio_directory)
    for bucket in payload["buckets"].values():
        for item in bucket:
            relative_path = item["audio_path"]
            assert Path(relative_path).name == relative_path
            resolved = service.resolve_audio_path(relative_path)
            assert resolved.parent == audio_directory
            assert resolved.is_file()
