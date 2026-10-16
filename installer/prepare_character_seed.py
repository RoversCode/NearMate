"""把开发角色素材中的预录回复转换为可移植的安装种子。"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import yaml


def prepare_character_seed(source_directory: Path, target_directory: Path) -> None:
    """保留实际登记的默认录音，去掉开发机路径、合成指纹和字幕校准。

    Args:
        source_directory: 原始默认角色卡目录；只读，不改写素材。
        target_directory: 构建流程已经复制好的 staging 角色目录。
    Returns:
        无；核验成功后仅写回 staging 的配置和角色卡。
    """
    source_directory = source_directory.resolve()
    target_directory = target_directory.resolve()
    if source_directory == target_directory:
        raise ValueError("发行种子必须写入独立 staging 目录")
    source_audio = (source_directory / "wake-response/audio").resolve()
    target_audio = (target_directory / "wake-response/audio").resolve()
    source_config = source_directory / "wake-response/config.json"
    payload = json.loads(source_config.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("buckets"), dict):
        raise ValueError("默认唤醒回复配置必须包含 buckets")
    buckets = {}
    for bucket, items in payload["buckets"].items():
        if not isinstance(items, list):
            raise ValueError("默认唤醒回复时段必须是列表")
        portable_items = []
        for item in items:
            if not isinstance(item, dict) or not item.get("id") or not item.get("text"):
                raise ValueError("默认唤醒回复必须有编号和文字")
            declared_path = item.get("audio_path")
            if not isinstance(declared_path, str) or not declared_path:
                raise ValueError("默认唤醒回复缺少预录音频")
            path = Path(declared_path)
            path = (path if path.is_absolute() else source_audio / path).resolve()
            if path.parent != source_audio:
                raise ValueError("默认唤醒回复音频路径越过角色音频目录")
            wav_bytes = path.read_bytes()
            staged_path = (target_audio / path.name).resolve()
            if staged_path.parent != target_audio or staged_path.read_bytes() != wav_bytes:
                raise ValueError("staging 默认唤醒音频与原始素材不一致")
            portable_items.append({"id": item["id"], "text": item["text"], "audio_path": path.name,
                                   "bundled_audio_sha256": hashlib.sha256(wav_bytes).hexdigest()})
        buckets[bucket] = portable_items
    if not any(buckets.values()):
        raise ValueError("默认唤醒回复不能是空配置")
    card_path = target_directory / "card.yaml"
    card = yaml.safe_load(card_path.read_text(encoding="utf-8"))
    card["meta"]["voice_binding"].pop("caption_timing", None)
    # 所有引用都核验成功后才写回；不把开发机合成指纹伪装成新用户确认的语音。
    (target_directory / "wake-response/config.json").write_text(
        json.dumps({"buckets": buckets}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    card_path.write_text(yaml.safe_dump(card, allow_unicode=True, sort_keys=False), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_directory", type=Path)
    parser.add_argument("target_directory", type=Path)
    arguments = parser.parse_args()
    prepare_character_seed(arguments.source_directory, arguments.target_directory)
