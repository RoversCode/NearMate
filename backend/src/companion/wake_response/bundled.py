"""只认安装包默认角色素材的预录唤醒音频白名单。"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any

from companion.character_cards.models import load_character_card_yaml_payload

if TYPE_CHECKING:
    from companion.wake_response.service import SelectedWakeResponse, WakeResponseItem


class BundledWakeResponseLibrary:
    """核验预录默认回复，并在启动时补回旧安装丢失的音频引用。

    Args:
        cards_dir: 用户可写角色卡根目录。
        seed_dir: 安装包只读角色卡根目录；为空时不开放预录音频。

    Returns:
        保存可信发行白名单的校验服务，不改变用户音色确认状态。
    """

    def __init__(self, cards_dir: Path, seed_dir: Path | None) -> None:
        """读取默认角色素材和已核验的录音登记，种子不完整时关闭预录资格。

        Args:
            cards_dir: 用户角色卡目录。
            seed_dir: 只读发行种子目录；开发态可以为空。

        Returns:
            无返回值；把核验通过的默认音色素材和回复登记保存在实例中。
        """

        self.cards_dir = cards_dir
        self.seed_card = seed_dir / "huohua" if seed_dir is not None else None
        self.items: list[dict[str, Any]] = []
        self.voice_binding: dict[str, Any] = {}
        self.reference_audio_sha256: str | None = None
        if self.seed_card is None:
            return
        try:
            payload = json.loads((self.seed_card / "wake-response/config.json").read_text(encoding="utf-8"))
            self.voice_binding = load_character_card_yaml_payload(self.seed_card / "card.yaml")["meta"]["voice_binding"]
            reference = (self.seed_card / self.voice_binding["reference_audio_path"]).resolve()
            if not reference.is_relative_to(self.seed_card.resolve()):
                return
            self.reference_audio_sha256 = hashlib.sha256(reference.read_bytes()).hexdigest()
            for items in payload["buckets"].values():
                for item in items:
                    if not isinstance(item, dict) or not item.get("id") or not item.get("text"):
                        continue
                    file_name = item.get("audio_path")
                    expected_hash = item.get("bundled_audio_sha256")
                    if not isinstance(file_name, str) or Path(file_name).name != file_name or not expected_hash:
                        continue
                    audio = self.seed_card / "wake-response/audio" / file_name
                    if audio.resolve().parent != (self.seed_card / "wake-response/audio").resolve():
                        continue
                    if hashlib.sha256(audio.read_bytes()).hexdigest() == expected_hash:
                        self.items.append(dict(item))
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            # 不完整或旧版种子没有可信登记时，继续走普通本机合成缓存，不能放宽资格。
            self.items = []

    def matches_default_voice(self, card_id: str) -> bool:
        """确认这张卡仍使用安装包默认角色的原始音色素材。

        Args:
            card_id: 要检查的当前角色卡编号。
        Returns:
            默认角色、音色名称、参考文字和原始参考音频都一致时返回真。
        """
        if card_id != "huohua" or not self.items or self.reference_audio_sha256 is None:
            return False
        card_directory = (self.cards_dir / card_id).resolve()
        try:
            binding = load_character_card_yaml_payload(card_directory / "card.yaml")["meta"]["voice_binding"]
            if any(binding.get(key) != self.voice_binding.get(key) for key in ("role_name", "prompt_text")):
                return False
            reference = (card_directory / binding["reference_audio_path"]).resolve()
            return (
                reference.is_relative_to(card_directory)
                and hashlib.sha256(reference.read_bytes()).hexdigest() == self.reference_audio_sha256
            )
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return False

    def is_audio_current(self, card_id: str, item: WakeResponseItem | SelectedWakeResponse) -> bool:
        """用只读种子的文字、文件名和摘要核验用户目录中的预录音频。

        Args:
            card_id: 音频所属角色卡编号。
            item: 唤醒回复条目或已选择的回复。
        Returns:
            仍属于默认音色且文件与发行白名单完全一致时返回真。
        """
        if not item.audio_path or not self.matches_default_voice(card_id):
            return False
        directory = (self.cards_dir / card_id / "wake-response/audio").resolve()
        audio_path = Path(item.audio_path)
        audio_path = (audio_path if audio_path.is_absolute() else directory / audio_path).resolve()
        if audio_path.parent != directory:
            return False
        entry = next(
            (entry for entry in self.items
             if entry["text"] == item.text and entry["audio_path"] == audio_path.name),
            None,
        )
        if entry is None:
            return False
        try:
            return hashlib.sha256(audio_path.read_bytes()).hexdigest() == entry["bundled_audio_sha256"]
        except OSError:
            return False

    def restore_missing_audio(self, card_id: str) -> None:
        """启动时仅补回旧版安装丢失的默认音频引用，不恢复用户删除的短句。

        Args:
            card_id: 要修复的默认角色卡编号。
        Returns:
            无；仅相同编号和文字且没有音频的条目会原子写回。
        """
        if not self.matches_default_voice(card_id):
            return
        config_path = self.cards_dir / card_id / "wake-response/config.json"
        try:
            payload = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(payload, dict) or not isinstance(payload.get("buckets"), dict):
            return
        changed = False
        for items in payload["buckets"].values():
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict) or item.get("audio_path") or item.get("voice_fingerprint"):
                    continue
                seed_item = next(
                    (entry for entry in self.items
                     if entry["id"] == item.get("id") and entry["text"] == item.get("text")),
                    None,
                )
                if seed_item is None:
                    continue
                file_name = seed_item["audio_path"]
                destination = self.cards_dir / card_id / "wake-response/audio" / file_name
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.resolve().parent != destination.parent.resolve():
                    continue
                # 旧安装可能留下同名孤立 WAV；按只读种子恢复，不能把被改写的文件认成原录音。
                shutil.copyfile(self.seed_card / "wake-response/audio" / file_name, destination)
                item.update(audio_path=file_name, synthesized_at=None, voice_fingerprint=None)
                changed = True
        if changed:
            temporary = config_path.with_name(f"{config_path.name}.tmp")
            temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            temporary.replace(config_path)
