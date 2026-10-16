"""验证默认角色的预录唤醒音频可以直接使用，同时保留用户编辑与语音门禁。"""

import hashlib
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import yaml

from companion.audio.pcm16_utils import pcm16_to_wav_bytes
from companion.character_cards.experience_assets import CharacterCardExperienceAssets
from companion.character_cards.store import CharacterCardStore
from companion.core.wake_response_console_service import WakeResponseConsoleService
from companion.core.realtime_input_runtime_owner import RealtimeInputRuntimeOwner
from companion.wake_response.service import WakeResponseService


def test_bundled_library_imports_first_in_a_clean_process():
    """预录库独立导入时不能通过角色包公开导出绕回尚未初始化的自身。"""
    source_root = Path(__file__).resolve().parents[2] / "src"
    code = (
        "import sys; "
        f"sys.path.insert(0, {str(source_root)!r}); "
        "from companion.wake_response.bundled import BundledWakeResponseLibrary; "
        "from companion.character_cards import CharacterCardStore"
    )
    result = subprocess.run([sys.executable, "-I", "-B", "-c", code], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


@pytest.fixture
def bundled_card(tmp_path: Path):
    """创建可移到另一台电脑的只读发行素材和用户角色卡。"""
    seed_root = tmp_path / "release"
    seed_card = seed_root / "huohua"
    source = Path(__file__).parents[2] / "data/character-cards/huohua/card.yaml"
    seed_card.mkdir(parents=True)
    card_payload = yaml.safe_load(source.read_text(encoding="utf-8"))
    (seed_card / "card.yaml").write_text(yaml.safe_dump(card_payload, allow_unicode=True), encoding="utf-8")
    (seed_card / "voice").mkdir()
    (seed_card / "voice/reference.wav").write_bytes(b"default-voice-reference")
    audio_directory = seed_card / "wake-response/audio"
    audio_directory.mkdir(parents=True)
    wav_bytes = pcm16_to_wav_bytes(b"\x10\x01" * 320)
    (audio_directory / "wake.wav").write_bytes(wav_bytes)
    item = {"id": "default-wake", "text": "我在呢，宝子。", "audio_path": "wake.wav",
            "bundled_audio_sha256": hashlib.sha256(wav_bytes).hexdigest()}
    (seed_card / "wake-response/config.json").write_text(
        json.dumps({"buckets": {"afternoon": [item]}}), encoding="utf-8")
    cards_root = tmp_path / "user-cards"
    store = CharacterCardStore(cards_root, seed_dir=seed_root)
    store.ensure_seed_assets()
    return cards_root, seed_root, item


def make_service(cards_root, seed_root, *, fingerprint="confirmed"):
    """通过生产资产服务创建带发行音频白名单的唤醒服务。"""
    assets = CharacterCardExperienceAssets(cards_root, seed_dir=seed_root)
    service = WakeResponseService(
        asset_dir=assets.wake_response_audio_dir("huohua"),
        voice_fingerprint_provider=lambda: fingerprint,
        bundled_audio_validator=lambda item: assets.bundled_wake_responses.is_audio_current("huohua", item),
    )
    return service, assets


def test_default_bundled_audio_plays_without_tts_or_machine_fingerprint(bundled_card):
    """安装用户确认语音后，预录回复无需再合成就能试听和播放。"""
    cards_root, seed_root, _ = bundled_card
    service, assets = make_service(cards_root, seed_root)
    config = service.normalize_config(assets.load_wake_response("huohua"))
    selected = service.select_for_time(config, hour=15)
    assert selected.text == "我在呢，宝子。"
    assert selected.voice_fingerprint is None
    assert service.read_audio(selected).pcm16 == b"\xff\x7f" * 320
    assert service.to_payload(config, include_audio_url=True, audio_url_base="/wake")['buckets']['afternoon'][0]['audio_url'] == "/wake/wake.wav"
    assert service.save_config(current_config=config, next_config=config) == config


@pytest.mark.parametrize("mutation", ["not_ready", "text", "content", "reference", "prompt", "role", "outside"])
def test_bundled_audio_rejects_unconfirmed_or_changed_assets(bundled_card, mutation):
    """只读种子白名单不能给改文字、改音色、篡改文件或越界路径放行。"""
    cards_root, seed_root, _ = bundled_card
    service, assets = make_service(cards_root, seed_root, fingerprint=None if mutation == "not_ready" else "confirmed")
    payload = assets.load_wake_response("huohua")
    item = payload['buckets']['afternoon'][0]
    if mutation == "text":
        item['text'] = "用户改写"
    elif mutation == "content":
        (cards_root / "huohua/wake-response/audio/wake.wav").write_bytes(b"changed")
    elif mutation == "reference":
        (cards_root / "huohua/voice/reference.wav").write_bytes(b"another-voice")
    elif mutation in {"prompt", "role"}:
        card_path = cards_root / "huohua/card.yaml"
        card = yaml.safe_load(card_path.read_text(encoding="utf-8"))
        card['meta']['voice_binding']['prompt_text' if mutation == "prompt" else 'role_name'] = "another"
        card_path.write_text(yaml.safe_dump(card, allow_unicode=True), encoding="utf-8")
    elif mutation == "outside":
        item['audio_path'] = "../wake.wav"
    config = service.normalize_config(payload)
    assert service.select_for_time(config, hour=15).audio_path is None
    assert service.to_payload(config, include_audio_url=True, audio_url_base="/wake")['buckets']['afternoon'][0]['audio_url'] is None


def test_other_character_cannot_borrow_default_bundled_audio(bundled_card):
    """复制卡片不能仅凭相同文件内容取得默认角色的白名单资格。"""
    cards_root, seed_root, _ = bundled_card
    service, assets = make_service(cards_root, seed_root)
    item = service.normalize_config(assets.load_wake_response("huohua")).buckets['afternoon'][0]
    assert not assets.bundled_wake_responses.is_audio_current("other-card", item)


def test_existing_user_only_recovers_unchanged_missing_audio(bundled_card):
    """升级只补回相同条目的缺音频，保留用户修改、删除与自行合成缓存。"""
    cards_root, seed_root, seed_item = bundled_card
    config_path = cards_root / "huohua/wake-response/config.json"
    missing = {"id": seed_item['id'], "text": seed_item['text']}
    edited = {"id": "edited", "text": "用户写的"}
    own = {"id": "own", "text": "自己的", "audio_path": "own.wav", "voice_fingerprint": "own-confirmed"}
    config_path.write_text(json.dumps({"buckets": {"afternoon": [missing, edited, own], "night": []}}), encoding="utf-8")
    (cards_root / "huohua/wake-response/audio/wake.wav").unlink()
    store = CharacterCardStore(cards_root, seed_dir=seed_root)
    store.ensure_seed_assets()
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    assert payload['buckets']['afternoon'][0]['audio_path'] == "wake.wav"
    assert (cards_root / "huohua/wake-response/audio/wake.wav").is_file()
    assert payload['buckets']['afternoon'][1:] == [edited, own]
    assert payload['buckets']['night'] == []


@pytest.mark.parametrize("change", ["edited", "deleted", "own_cache"])
def test_upgrade_does_not_restore_over_user_choice(bundled_card, change):
    """同编号改写、删除和已有音频均由用户决定，升级不重置这些条目。"""
    cards_root, seed_root, seed_item = bundled_card
    config_path = cards_root / "huohua/wake-response/config.json"
    item = {"id": seed_item['id'], "text": seed_item['text']}
    if change == "edited":
        item['text'] = "用户改写"
    elif change == "own_cache":
        item.update(audio_path="own.wav", voice_fingerprint="custom")
    payload = {"buckets": {"afternoon": [] if change == "deleted" else [item]}}
    config_path.write_text(json.dumps(payload), encoding="utf-8")
    CharacterCardStore(cards_root, seed_dir=seed_root).ensure_seed_assets()
    assert json.loads(config_path.read_text(encoding="utf-8")) == payload


def test_store_keeps_runtime_seed_recovery_without_repeating_audio_backfill(bundled_card):
    """一次音频补回不能取消原有缺卡恢复，也不能在每次读取时改写用户选择。"""
    cards_root, seed_root, seed_item = bundled_card
    store = CharacterCardStore(cards_root, seed_dir=seed_root)
    store.ensure_seed_assets()
    (cards_root / "huohua/card.yaml").unlink()
    (cards_root / "active.json").unlink()
    store.ensure_seed_assets()
    assert (cards_root / "huohua/card.yaml").is_file()
    assert store.load_active_card_id() == "huohua"
    config_path = cards_root / "huohua/wake-response/config.json"
    payload = {"buckets": {"afternoon": [{"id": seed_item['id'], "text": seed_item['text']}]}}
    config_path.write_text(json.dumps(payload), encoding="utf-8")
    store.ensure_seed_assets()
    assert json.loads(config_path.read_text(encoding="utf-8")) == payload


def test_console_uses_default_bundled_audio_lookup(bundled_card):
    """Console 试听使用与正式唤醒相同的白名单，而不是请求一次新 TTS。"""
    cards_root, seed_root, _ = bundled_card
    assets = CharacterCardExperienceAssets(cards_root, seed_dir=seed_root)
    hub = SimpleNamespace(character_cards=CharacterCardStore(cards_root), character_card_assets=assets)
    console = WakeResponseConsoleService(hub=hub, assistant_process_supervisor=Mock(), prompt_audit_recorder=Mock())
    console.voice_fingerprint_for_card = lambda _: "confirmed"
    assert console.audio_path_for_card("huohua", "wake.wav") == (cards_root / "huohua/wake-response/audio/wake.wav").resolve()


def test_runtime_uses_bundled_whitelist_and_packaged_confirmation_gate(bundled_card, monkeypatch):
    """正式唤醒 Runtime 接入同一默认音频资格，安装态不能用开发指纹绕过确认。"""
    cards_root, seed_root, _ = bundled_card
    assets = CharacterCardExperienceAssets(cards_root, seed_dir=seed_root)
    service = WakeResponseService(asset_dir=assets.wake_response_audio_dir("huohua"))
    # Bootstrap 对外投影会主动移除 fingerprint；正式 Runtime 必须接收内部 TTS 状态提供器。
    voice = {
        "provider": "local_streamvox",
        "is_ready": True,
        "phase": "ready",
        "has_confirmed_voice": True,
    }
    internal_voice = {"fingerprint": "confirmed"}
    hub = SimpleNamespace(
        character_cards=CharacterCardStore(cards_root), character_card_assets=assets,
        wake_response_service=service, bootstrap=SimpleNamespace(status_payload=lambda: {"voice": voice}),
    )
    owner = object.__new__(RealtimeInputRuntimeOwner)
    owner.hub = hub
    owner._voice_status_provider = lambda: internal_voice
    runtime = owner.attach_wake_response_runtime(SimpleNamespace())
    config = service.normalize_config(runtime.wake_response_config_provider())
    assert service.read_audio(service.select_for_time(config, hour=15)).pcm16
    monkeypatch.setenv("NEARMATE_PACKAGED", "1")
    internal_voice["fingerprint"] = None
    assert service.select_for_time(config, hour=15).audio_path is None


def test_concurrent_synthesis_preserves_both_updates_and_reads_old_snapshot(bundled_card):
    """后台合成串行提交，GET 在长合成期间仍能立即读取旧的完整配置。"""
    cards_root, _, _ = bundled_card
    assets = CharacterCardExperienceAssets(cards_root)
    assets.save_wake_response("huohua", {"buckets": {"afternoon": []}})
    hub = SimpleNamespace(character_cards=CharacterCardStore(cards_root), character_card_assets=assets)
    console = WakeResponseConsoleService(hub=hub, assistant_process_supervisor=Mock(), prompt_audit_recorder=Mock())
    console.voice_fingerprint_for_card = lambda _: "confirmed"
    first_started, release_first, second_attempted, second_started = (Event() for _ in range(4))

    def synthesize(text):
        """让第一个真实写事务暂停，确定两个写请求确实发生重叠。"""
        if text == "first":
            first_started.set()
            assert release_first.wait(3)
        else:
            second_started.set()
        return pcm16_to_wav_bytes(b"\x10\x01" * 320)

    def second_request():
        """标记第二个调用已经开始，避免线程调度延迟掩盖并发错误。"""
        second_attempted.set()
        return console.synthesize_item("huohua", "second", bucket="afternoon", text="second")

    console.synthesize_wav = lambda _card, text: synthesize(text)
    with ThreadPoolExecutor(max_workers=3) as executor:
        first = executor.submit(console.synthesize_item, "huohua", "first", bucket="afternoon", text="first")
        try:
            assert first_started.wait(2)
            second = executor.submit(second_request)
            assert second_attempted.wait(2)
            assert not second_started.wait(0.2)
            snapshot = executor.submit(console.config_payload, "huohua").result(timeout=1)
            assert snapshot['buckets']['afternoon'] == []
        finally:
            release_first.set()
        first.result(timeout=3)
        second.result(timeout=3)
    assert {item['id'] for item in assets.load_wake_response("huohua")['buckets']['afternoon']} == {"first", "second"}
