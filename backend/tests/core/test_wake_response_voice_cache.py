"""验证唤醒回复缓存始终绑定当前角色已确认的音色和执行方式。"""

from collections import OrderedDict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from companion.audio.pcm16_utils import pcm16_to_wav_bytes
from companion.core.wake_response_console_service import WakeResponseConsoleService
from companion.wake_response.service import (
    WAKE_RESPONSE_BUCKETS,
    WakeResponseConfig,
    WakeResponseItem,
    WakeResponseService,
)


@pytest.fixture
def voice_cache(tmp_path):
    """使用可切换的确认指纹，模拟用户修改语音执行方式。"""
    confirmed = SimpleNamespace(fingerprint='a' * 64)
    synthesize = Mock(return_value=pcm16_to_wav_bytes(b'\x10\x01' * 2400))
    service = WakeResponseService(asset_dir=tmp_path, synthesize_wav=synthesize,
        voice_fingerprint_provider=lambda: confirmed.fingerprint)
    empty = service.normalize_config({})
    requested = service.normalize_config({'buckets': {'morning': [{'id': 'greeting', 'text': '早上好'}]}})
    saved = service.save_config(current_config=empty, next_config=requested)
    synthesize.reset_mock()
    return service, confirmed, synthesize, saved


def test_confirmed_wake_audio_roundtrips_and_reuses(voice_cache):
    """相同角色音色可重用已有文件，保存后指纹不会丢失。"""
    service, confirmed, synthesize, saved = voice_cache
    restored = service.normalize_config(service.to_payload(saved))
    service.save_config(current_config=saved, next_config=restored)
    synthesize.assert_not_called()
    selected = service.select_for_time(restored, hour=8)
    assert selected.voice_fingerprint == confirmed.fingerprint
    assert service.read_audio(selected).pcm16


def test_single_item_synthesis_does_not_touch_other_items(voice_cache):
    """单行合成只调用目标文本，并保留其他条目原样。"""
    service, _, synthesize, saved = voice_cache
    saved.buckets['evening'].append(WakeResponseItem(id='evening', text='晚上好', audio_path=None))

    updated = service.synthesize_item(
        current_config=saved, bucket='morning', item_id='greeting', text='早上新句子'
    )

    synthesize.assert_called_once_with('早上新句子')
    assert updated.buckets['morning'][0].text == '早上新句子'
    assert updated.buckets['evening'][0].text == '晚上好'


def test_single_item_synthesis_can_create_new_item(voice_cache):
    """单行合成支持把新增草稿直接保存为新条目。"""
    service, _, synthesize, saved = voice_cache
    updated = service.synthesize_item(
        current_config=saved, bucket='night', item_id='new-night', text='夜里我在'
    )

    synthesize.assert_called_once_with('夜里我在')
    assert updated.buckets['night'][0].id == 'new-night'


@pytest.mark.parametrize('fingerprint', [None, 'b' * 64])
def test_changed_or_unready_voice_never_plays_cached_audio(voice_cache, fingerprint):
    """后端或素材改变后，即使旧文件还在，也不能继续播放。"""
    service, confirmed, _, saved = voice_cache
    selected_before_change = service.select_for_time(saved, hour=8)
    confirmed.fingerprint = fingerprint
    assert service.select_for_time(saved, hour=8).audio_path is None
    assert service.read_audio(selected_before_change) is None
    payload = service.to_payload(saved, include_audio_url=True, audio_url_base='/test/audio')
    assert payload['buckets']['morning'][0]['audio_url'] is None


def test_old_cache_without_fingerprint_is_not_playable(voice_cache):
    """历史音频没有当前配置凭据时必须重新生成。"""
    service, _, _, saved = voice_cache
    payload = service.to_payload(saved)
    payload['buckets']['morning'][0].pop('voice_fingerprint')
    assert service.select_for_time(service.normalize_config(payload), hour=8).audio_path is None


def test_changed_voice_regenerates_audio_and_preserves_old_file(voice_cache):
    """新指纹使用新文件，已选中的旧路径不会被另一种声音覆盖。"""
    service, confirmed, synthesize, saved = voice_cache
    previous_item = saved.buckets['morning'][0]
    confirmed.fingerprint = 'b' * 64
    next_config = service.save_config(current_config=saved, next_config=saved)
    next_item = next_config.buckets['morning'][0]
    synthesize.assert_called_once_with('早上好')
    assert next_item.voice_fingerprint == confirmed.fingerprint
    assert next_item.audio_path != previous_item.audio_path


def test_voice_change_during_synthesis_does_not_publish_audio(voice_cache):
    """合成中的配置变化必须使这份结果失败，不能伪装成新音色。"""
    service, confirmed, synthesize, saved = voice_cache

    def change_voice(_text):
        """在合成完成前模拟一次执行方式切换。"""
        confirmed.fingerprint = 'b' * 64
        return pcm16_to_wav_bytes(b'\x10\x01' * 2400)

    synthesize.side_effect = change_voice
    requested = service.normalize_config({'buckets': {'morning': [{'id': 'new-item', 'text': '你好'}]}})
    files_before = set(service.asset_dir.glob('*.wav'))
    with pytest.raises(RuntimeError, match='合成期间发生变化'):
        service.save_config(current_config=saved, next_config=requested)
    assert set(service.asset_dir.glob('*.wav')) == files_before


def test_delete_stale_item_does_not_require_tts(voice_cache):
    """删除旧短句不应因尚未重新配置语音而被阻止。"""
    service, confirmed, synthesize, saved = voice_cache
    confirmed.fingerprint = None
    updated = service.remove_item(saved, 'greeting')
    assert updated.buckets['morning'] == []
    synthesize.assert_not_called()


def test_console_audio_lookup_rejects_previous_voice_cache(voice_cache):
    """直接访问旧试听地址也必须经过指纹校验。"""
    service, confirmed, _, saved = voice_cache
    hub = SimpleNamespace(character_cards=Mock(), character_card_assets=SimpleNamespace(
        load_wake_response=lambda _: service.to_payload(saved)))
    console = WakeResponseConsoleService(hub=hub, assistant_process_supervisor=Mock(),
                                         prompt_audit_recorder=Mock())
    console.service_for_card = lambda _: service
    from pathlib import Path

    audio_path = Path(saved.buckets['morning'][0].audio_path)
    assert console.audio_path_for_card('current-card', audio_path.name) == audio_path
    confirmed.fingerprint = 'b' * 64
    with pytest.raises(KeyError, match='no longer current'):
        console.audio_path_for_card('current-card', audio_path.name)


def test_runtime_service_resolves_active_character_audio_directory(tmp_path: Path):
    """角色切换后，运行时必须立即改用新角色的受管音频目录。"""
    active_card = {"id": "old-card"}
    old_audio_dir = tmp_path / "old-card"
    new_audio_dir = tmp_path / "new-card"
    old_audio_dir.mkdir()
    new_audio_dir.mkdir()
    old_audio = old_audio_dir / "wake.wav"
    old_audio.write_bytes(pcm16_to_wav_bytes(b"\x10\x01" * 32))

    service = WakeResponseService(
        asset_dir=old_audio_dir,
        asset_dir_provider=lambda: tmp_path / active_card["id"],
        voice_fingerprint_provider=lambda: "confirmed",
        random_seed=1,
    )
    item = WakeResponseItem(
        id="wake-1", text="我在", audio_path=str(old_audio), voice_fingerprint="confirmed"
    )
    config = WakeResponseConfig(
        buckets=OrderedDict((bucket, []) for bucket in WAKE_RESPONSE_BUCKETS)
    )
    config.buckets["morning"].append(item)

    assert service.read_audio(service.select_for_time(config, hour=6)).pcm16
    active_card["id"] = "new-card"
    assert service.select_for_time(config, hour=6).audio_path is None


def test_remove_deleted_assets_never_deletes_relative_path_outside_managed_dir(tmp_path: Path):
    """清理资产时相对路径不能被误判成当前目录内的受管文件。"""
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()
    audio_path = audio_dir / "wake.wav"
    audio_path.write_bytes(b"wav")
    service = WakeResponseService(asset_dir=audio_dir)
    service._remove_deleted_assets(  # noqa: SLF001
        current_items_by_id={
            "wake-1": WakeResponseItem(
                id="wake-1", text="我在", audio_path=str(audio_path.relative_to(tmp_path))
            )
        },
        next_ids=set(),
    )
    assert audio_path.exists()
