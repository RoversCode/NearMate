from __future__ import annotations

import json
import os
import wave
from io import BytesIO
from pathlib import Path
from dataclasses import dataclass
from typing import Any

import numpy as np

from companion.character_cards.models import CharacterCardDefinition, CharacterCardStoreError
from companion.wake_response.bundled import BundledWakeResponseLibrary


@dataclass(frozen=True, slots=True)
class VoiceReferenceAudioSnapshot:
    """保存一次角色卡参考音频目录快照，供失败事务完整恢复。

    Args:
        files: 参考音频文件名和原始字节内容。文件名只包含 ``reference.*``
            的单层名称，避免回滚时写出角色卡目录。
    """

    files: tuple[tuple[str, bytes], ...]


class CharacterCardExperienceAssets:
    """管理角色卡目录内的体验包资产。"""

    def __init__(self, cards_dir: str | Path, *, seed_dir: str | Path | None = None) -> None:
        """创建角色体验包资产服务。

        Args:
            cards_dir: 角色卡根目录。
            seed_dir: 安装包只读默认素材目录，用于核验预录唤醒回复。

        Returns:
            无；初始化角色资产目录与默认录音白名单。
        """

        self.cards_dir = Path(cards_dir)
        self.bundled_wake_responses = BundledWakeResponseLibrary(
            self.cards_dir, Path(seed_dir) if seed_dir is not None else None
        )

    def card_dir(self, card_id: str) -> Path:
        """返回角色卡目录。

        Args:
            card_id: 角色卡编号。

        Returns:
            角色卡目录路径。
        """

        normalized_card_id = self._normalize_card_id(card_id)
        return self.cards_dir / normalized_card_id

    def wake_response_config_path(self, card_id: str) -> Path:
        """返回角色卡唤醒回复配置文件路径。"""

        return self.card_dir(card_id) / "wake-response" / "config.json"

    def wake_response_audio_dir(self, card_id: str) -> Path:
        """返回角色卡唤醒回复音频目录。"""

        return self.card_dir(card_id) / "wake-response" / "audio"

    def load_wake_response(self, card_id: str) -> dict[str, Any]:
        """读取角色卡唤醒回复配置。

        Args:
            card_id: 角色卡编号。

        Returns:
            配置对象；文件不存在时返回空对象。
        """

        config_path = self.wake_response_config_path(card_id)
        if not config_path.exists():
            return {}
        try:
            payload = json.loads(config_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exception:
            raise CharacterCardStoreError(f"角色卡唤醒回复配置不是合法 JSON：{config_path}") from exception
        if not isinstance(payload, dict):
            raise CharacterCardStoreError(f"角色卡唤醒回复配置根节点必须是对象：{config_path}")
        return payload

    def save_wake_response(self, card_id: str, payload: dict[str, Any]) -> None:
        """保存角色卡唤醒回复配置。

        Args:
            card_id: 角色卡编号。
            payload: 已归一化的唤醒回复配置。
        """

        config_path = self.wake_response_config_path(card_id)
        config_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = config_path.with_name(f"{config_path.name}.tmp")
        temporary_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary_path.replace(config_path)

    def resolve_voice_reference_audio(self, card: CharacterCardDefinition) -> Path | None:
        """解析角色卡语音参考音频路径。

        Args:
            card: 角色卡定义。

        Returns:
            已存在的参考音频绝对路径；缺失时返回 None。
        """

        return self._resolve_declared_asset(card.meta.card_id, card.meta.voice_binding.reference_audio_path)

    def read_voice_prompt_text(self, card: CharacterCardDefinition) -> str | None:
        """读取角色卡语音 prompt 文本。

        Args:
            card: 角色卡定义。

        Returns:
            非空 prompt 文本；内容为空时返回 None。
        """

        text = card.meta.voice_binding.prompt_text.strip()
        return text or None

    def has_voice_assets(self, card: CharacterCardDefinition) -> bool:
        """判断角色卡是否具备注册 StreamVox role 的资产。"""

        return self.resolve_voice_reference_audio(card) is not None and self.read_voice_prompt_text(card) is not None

    def save_uploaded_voice_reference_audio(
        self,
        *,
        card_id: str,
        file_name: str,
        content: bytes,
    ) -> str:
        """保存控制台上传的角色卡参考音频。

        Args:
            card_id: 角色卡编号。
            file_name: 浏览器上传文件名，仅用于推断扩展名。
            content: 上传的音频二进制内容。

        Returns:
            写入角色卡 YAML 的卡内相对路径。
        """

        if not content:
            raise CharacterCardStoreError("角色卡语音参考音频不能为空")

        suffix = Path(file_name).suffix.lower()
        if suffix not in {".wav", ".mp3", ".flac", ".m4a", ".ogg", ".webm"}:
            raise CharacterCardStoreError("角色卡语音参考音频格式不支持")

        voice_dir = self.card_dir(card_id) / "voice"
        voice_dir.mkdir(parents=True, exist_ok=True)

        # 同一角色卡只保留一个 reference.*，避免旧扩展名资产继续被误读。
        for old_reference in voice_dir.glob("reference.*"):
            if old_reference.is_file():
                old_reference.unlink()

        audio_path = voice_dir / f"reference{suffix}"
        self._atomic_write_bytes(audio_path, content)
        return f"voice/reference{suffix}"

    def save_captured_voice_reference_audio(
        self,
        *,
        card_id: str,
        audio: np.ndarray,
        sample_rate: int,
    ) -> str:
        """保存从 StreamVox 捕获的角色卡音色参考音频。

        Args:
            card_id: 角色卡编号。
            audio: StreamVox PromptData 中目标采样率下的参考音频。
            sample_rate: 参考音频采样率。

        Returns:
            写入角色卡 YAML 的参考音频路径。
        """

        if sample_rate <= 0:
            raise CharacterCardStoreError("角色卡语音采样率无效")

        voice_dir = self.card_dir(card_id) / "voice"
        voice_dir.mkdir(parents=True, exist_ok=True)
        audio_path = voice_dir / "reference.wav"
        self._atomic_write_bytes(audio_path, _float_audio_to_wav_bytes(audio=audio, sample_rate=sample_rate))
        return "voice/reference.wav"

    def snapshot_voice_reference_audio(self, card_id: str) -> VoiceReferenceAudioSnapshot:
        """读取当前角色卡所有参考音频文件，供后续失败回滚使用。

        Args:
            card_id: 角色卡编号。

        Returns:
            当前 ``voice/reference.*`` 文件的名称和字节快照；目录不存在时为空快照。
        """

        voice_dir = self.card_dir(card_id) / "voice"
        if not voice_dir.exists():
            return VoiceReferenceAudioSnapshot(files=())

        files: list[tuple[str, bytes]] = []
        for path in sorted(voice_dir.glob("reference.*")):
            if path.is_file():
                files.append((path.name, path.read_bytes()))
        return VoiceReferenceAudioSnapshot(files=tuple(files))

    def restore_voice_reference_audio(
        self,
        card_id: str,
        snapshot: VoiceReferenceAudioSnapshot,
    ) -> None:
        """把角色卡参考音频目录恢复到给定快照。

        Args:
            card_id: 角色卡编号。
            snapshot: 事务开始前保存的参考音频快照。

        Returns:
            无返回值；恢复失败时让调用方感知异常并记录补偿失败。
        """

        voice_dir = self.card_dir(card_id) / "voice"
        voice_dir.mkdir(parents=True, exist_ok=True)
        for path in voice_dir.glob("reference.*"):
            if path.is_file():
                path.unlink()
        for file_name, content in snapshot.files:
            # 快照由本类生成，但仍做单层校验，避免未来调用方把路径穿越带进回滚。
            if Path(file_name).name != file_name or not file_name.startswith("reference."):
                raise CharacterCardStoreError("角色卡语音回滚文件名非法")
            self._atomic_write_bytes(voice_dir / file_name, content)

    def _resolve_declared_asset(self, card_id: str, relative_path: str | None) -> Path | None:
        """把角色卡声明的相对资产路径解析为卡内文件。"""

        if relative_path is None:
            return None
        card_dir = self.card_dir(card_id).resolve()
        resolved_path = (self.card_dir(card_id) / relative_path).resolve()
        if card_dir not in resolved_path.parents:
            raise CharacterCardStoreError("角色卡资产路径越界")
        if not resolved_path.is_file():
            return None
        return resolved_path

    def _normalize_card_id(self, card_id: str) -> str:
        """裁剪并校验角色卡编号。"""

        normalized_card_id = card_id.strip()
        if not normalized_card_id:
            raise CharacterCardStoreError("角色卡编号不能为空")
        if any(character in normalized_card_id for character in ("/", "\\", "..")):
            raise CharacterCardStoreError("角色卡编号包含非法路径字符")
        return normalized_card_id

    def _atomic_write_bytes(self, path: Path, content: bytes) -> None:
        """原子写入二进制资产。"""

        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = path.with_name(f"{path.name}.tmp")
        temporary_path.write_bytes(content)
        os.replace(temporary_path, path)


def _float_audio_to_wav_bytes(*, audio: np.ndarray, sample_rate: int) -> bytes:
    """把 StreamVox prompt 浮点音频保存为单声道 PCM16 WAV。"""

    array = np.asarray(audio, dtype=np.float32).reshape(-1)
    if array.size == 0:
        raise CharacterCardStoreError("角色卡语音参考音频不能为空")
    clipped_audio = np.clip(array, -1.0, 1.0)
    pcm16 = (clipped_audio * 32767.0).astype(np.int16).tobytes()
    buffer = BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(int(sample_rate))
        wav_file.writeframes(pcm16)
    return buffer.getvalue()
