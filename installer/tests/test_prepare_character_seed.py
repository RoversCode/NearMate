"""用真实默认角色素材验证发行预录音频配置可移植且不泄漏开发缓存。"""

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from companion.character_cards.experience_assets import CharacterCardExperienceAssets
from companion.character_cards.store import CharacterCardStore
from companion.wake_response.service import SelectedWakeResponse, WakeResponseService

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
HELPER = REPOSITORY_ROOT / "installer/prepare_character_seed.py"


def test_release_keeps_five_declared_wavs_as_portable_trusted_audio(tmp_path: Path):
    """在 staging 转换真实配置，不改源素材，也不要求用户重新生成音频。"""
    source = REPOSITORY_ROOT / "backend/data/character-cards/huohua"
    staging = tmp_path / "huohua"
    shutil.copytree(source, staging)
    source_config_bytes = (source / "wake-response/config.json").read_bytes()
    before_files = {path.name: path.read_bytes() for path in (staging / "wake-response/audio").glob("*.wav")}
    result = subprocess.run([sys.executable, "-I", "-B", str(HELPER), str(source), str(staging)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    payload = json.loads((staging / "wake-response/config.json").read_text(encoding="utf-8"))
    items = [item for bucket in payload['buckets'].values() for item in bucket]
    assert len(items) == 5
    for item in items:
        assert Path(item['audio_path']).name == item['audio_path']
        assert item['bundled_audio_sha256'] == hashlib.sha256(before_files[item['audio_path']]).hexdigest()
        assert 'voice_fingerprint' not in item
        assert 'synthesized_at' not in item
    assert {path.name: path.read_bytes() for path in (staging / "wake-response/audio").glob("*.wav")} == before_files
    assert (source / "wake-response/config.json").read_bytes() == source_config_bytes
    card = yaml.safe_load((staging / "card.yaml").read_text(encoding="utf-8"))
    assert 'caption_timing' not in card['meta']['voice_binding']
    # 按安装态流程从只读种子复制到另一目录，验证五条真实录音都能直接消费。
    seed_root = tmp_path / "release"
    shutil.copytree(staging, seed_root / "huohua")
    user_root = tmp_path / "user"
    CharacterCardStore(user_root, seed_dir=seed_root).ensure_seed_assets()
    assets = CharacterCardExperienceAssets(user_root, seed_dir=seed_root)
    service = WakeResponseService(
        asset_dir=assets.wake_response_audio_dir("huohua"),
        voice_fingerprint_provider=lambda: "confirmed-on-this-machine",
        bundled_audio_validator=lambda item: assets.bundled_wake_responses.is_audio_current("huohua", item),
    )
    config = service.normalize_config(assets.load_wake_response("huohua"))
    for bucket, bucket_items in config.buckets.items():
        for item in bucket_items:
            assert item.voice_fingerprint is None
            assert service.read_audio(SelectedWakeResponse(bucket, item.text, item.audio_path)).pcm16


@pytest.mark.parametrize("audio_path", ["../outside.wav", "missing.wav"])
def test_release_rejects_invalid_audio_reference_before_writing(tmp_path: Path, audio_path):
    """缺失文件或目录穿越必须让构建失败，不能发布空的默认音频。"""
    staging = tmp_path / "huohua"
    (staging / "wake-response/audio").mkdir(parents=True)
    card = {"meta": {"voice_binding": {"caption_timing": {"ms_per_unit": 1}}}}
    card_path = staging / "card.yaml"
    card_path.write_text(yaml.safe_dump(card), encoding="utf-8")
    payload = {"buckets": {"afternoon": [{"id": "wake", "text": "我在", "audio_path": audio_path}]}}
    config_path = staging / "wake-response/config.json"
    config_path.write_text(json.dumps(payload), encoding="utf-8")
    source = tmp_path / "source"
    shutil.copytree(staging, source)
    before = config_path.read_bytes(), card_path.read_bytes()
    result = subprocess.run([sys.executable, "-I", "-B", str(HELPER), str(source), str(staging)], capture_output=True, text=True)
    assert result.returncode != 0
    assert (config_path.read_bytes(), card_path.read_bytes()) == before
