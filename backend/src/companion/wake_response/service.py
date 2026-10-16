from __future__ import annotations

import random
import wave
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from companion.audio.leading_silence_gate import trim_wav_leading_silence
from companion.observability.logging_config import get_backend_logger


logger = get_backend_logger(__name__)

DEFAULT_WAKE_RESPONSE_TEXT = "我在，正在听。"
WAKE_RESPONSE_BUCKETS = ("morning", "afternoon", "evening", "night")
WAKE_RESPONSE_BUCKET_LABELS = {
    "morning": "早晨",
    "afternoon": "午后",
    "evening": "晚上",
    "night": "深夜",
}


@dataclass(frozen=True)
class WakeResponseItem:
    """描述一条可编辑的唤醒回复文本及其音频资产。"""

    id: str
    text: str
    audio_path: str | None = None
    synthesized_at: float | None = None
    voice_fingerprint: str | None = None


@dataclass(frozen=True)
class WakeResponseConfig:
    """按时段保存唤醒回复短句池。"""

    buckets: OrderedDict[str, list[WakeResponseItem]] = field(default_factory=OrderedDict)


@dataclass(frozen=True)
class SelectedWakeResponse:
    """运行时选中的唤醒回复。"""

    bucket: str
    text: str
    audio_path: str | None
    voice_fingerprint: str | None = None


@dataclass(frozen=True)
class WakeResponseAudio:
    """已解析出的唤醒回复 PCM 音频。"""

    pcm16: bytes
    sample_rate: int


class WakeResponseService:
    """管理唤醒回复配置、合成资产和运行时选择。"""

    def __init__(
        self,
        *,
        asset_dir: str | Path,
        asset_dir_provider: Callable[[], str | Path] | None = None,
        synthesize_wav: Callable[[str], bytes] | None = None,
        voice_fingerprint_provider: Callable[[], str | None] | None = None,
        bundled_audio_validator: Callable[[WakeResponseItem | SelectedWakeResponse], bool] | None = None,
        random_seed: int | None = None,
        now: Callable[[], float] | None = None,
    ) -> None:
        """初始化唤醒回复服务。

        Args:
            asset_dir: 唤醒回复 wav 资产目录。
            asset_dir_provider: 返回当前角色唤醒音频目录；提供后优先于固定目录。
            synthesize_wav: 把文本合成为 wav 字节的可注入函数。
            voice_fingerprint_provider: 返回当前已确认音色指纹；未就绪时返回空值。
            bundled_audio_validator: 核验无本机指纹的默认预录音频是否仍属于只读发行素材。
            random_seed: 测试用随机种子；生产传空使用系统随机源。
            now: 时间戳来源，主要用于测试保存后的合成时间。
        """

        self.asset_dir = Path(asset_dir)
        self.asset_dir_provider = asset_dir_provider
        self.synthesize_wav = synthesize_wav
        self.voice_fingerprint_provider = voice_fingerprint_provider
        self.bundled_audio_validator = bundled_audio_validator
        self.random = random.Random(random_seed)
        self.now = now or __import__("time").time

    def resolved_asset_dir(self) -> Path:
        """返回当前应该使用的角色唤醒音频目录。

        Returns:
            当前角色的受管音频目录。
        """

        if self.asset_dir_provider is None:
            return self.asset_dir
        return Path(self.asset_dir_provider())

    def normalize_config(self, payload: Mapping[str, Any] | None) -> WakeResponseConfig:
        """把外部载荷归一为完整四时段配置。

        Args:
            payload: 来自用户设置或 API 的未知配置对象。

        Returns:
            保留四个固定时段的配置对象。
        """

        raw_buckets = payload.get("buckets", {}) if isinstance(payload, Mapping) else {}
        buckets: OrderedDict[str, list[WakeResponseItem]] = OrderedDict()
        for bucket in WAKE_RESPONSE_BUCKETS:
            raw_items = raw_buckets.get(bucket, []) if isinstance(raw_buckets, Mapping) else []
            buckets[bucket] = self._normalize_items(raw_items)
        return WakeResponseConfig(buckets=buckets)

    def to_payload(
        self,
        config: WakeResponseConfig,
        *,
        include_audio_url: bool = False,
        audio_url_base: str | None = None,
    ) -> dict[str, object]:
        """把配置转换为 JSON 友好的控制台载荷。

        Args:
            config: 已归一化的唤醒回复配置。
            include_audio_url: 是否包含控制台试听 URL。
            audio_url_base: 控制台读取音频文件的接口前缀；包含试听 URL 时必须由 API 层显式传入。

        Returns:
            可直接 JSON 序列化的配置对象。
        """

        if include_audio_url and audio_url_base is None:
            raise ValueError("include_audio_url=True 时必须显式传入 audio_url_base")

        resolved_audio_url_base = audio_url_base or ""
        return {
            "buckets": {
                bucket: [
                    self._item_payload(item, include_audio_url=include_audio_url, audio_url_base=resolved_audio_url_base)
                    for item in items
                ]
                for bucket, items in config.buckets.items()
            }
        }

    def save_config(self, *, current_config: WakeResponseConfig, next_config: WakeResponseConfig) -> WakeResponseConfig:
        """保存配置前合成变更文本并清理删除项资产。"""

        asset_dir = self.resolved_asset_dir()
        asset_dir.mkdir(parents=True, exist_ok=True)
        current_items_by_id = {
            item.id: item
            for items in current_config.buckets.values()
            for item in items
        }
        next_ids = {item.id for items in next_config.buckets.values() for item in items}

        buckets: OrderedDict[str, list[WakeResponseItem]] = OrderedDict()
        for bucket, items in next_config.buckets.items():
            saved_items: list[WakeResponseItem] = []
            for item in items:
                previous_item = current_items_by_id.get(item.id)
                if self._can_reuse_audio(previous_item=previous_item, next_item=item):
                    saved_items.append(
                        WakeResponseItem(
                            id=item.id,
                            text=item.text,
                            audio_path=previous_item.audio_path,
                            synthesized_at=previous_item.synthesized_at,
                            voice_fingerprint=previous_item.voice_fingerprint,
                        )
                    )
                    continue
                saved_items.append(
                    self._synthesize_item(bucket=bucket, item=item, asset_dir=asset_dir)
                )
            buckets[bucket] = saved_items
        # 所有新音频都成功后才清理删除项，合成失败时继续保留原配置的文件。
        self._remove_deleted_assets(current_items_by_id=current_items_by_id, next_ids=next_ids)
        return WakeResponseConfig(buckets=buckets)

    def synthesize_item(
        self,
        *,
        current_config: WakeResponseConfig,
        bucket: str,
        item_id: str,
        text: str,
    ) -> WakeResponseConfig:
        """只合成并替换一条回复，其他时段和条目完全不参与本次操作。

        Args:
            current_config: 后端当前已保存的完整配置。
            bucket: 条目所在时段。
            item_id: 要创建或替换的条目编号。
            text: 要合成的当前文本。

        Returns:
            包含新音频条目的完整配置；合成失败时不会返回部分结果。
        """

        if bucket not in WAKE_RESPONSE_BUCKETS:
            raise KeyError("wake response bucket not found")
        target = WakeResponseItem(id=item_id, text=text.strip())
        if not target.text:
            raise ValueError("wake response text must not be blank")
        # 条目编号全局唯一；时段不匹配时拒绝请求，避免迟到请求制造重复条目。
        existing_bucket = next(
            (
                current_bucket
                for current_bucket, items in current_config.buckets.items()
                if any(item.id == item_id for item in items)
            ),
            None,
        )
        if existing_bucket is not None and existing_bucket != bucket:
            raise KeyError("wake response item bucket mismatch")
        generated_item = self._synthesize_item(bucket=bucket, item=target)
        next_buckets: OrderedDict[str, list[WakeResponseItem]] = OrderedDict()
        found = False
        for current_bucket, items in current_config.buckets.items():
            if current_bucket != bucket:
                next_buckets[current_bucket] = list(items)
                continue
            replaced_items: list[WakeResponseItem] = []
            for item in items:
                if item.id == item_id:
                    replaced_items.append(generated_item)
                    found = True
                else:
                    replaced_items.append(item)
            if not found:
                replaced_items.append(generated_item)
            next_buckets[current_bucket] = replaced_items
        return WakeResponseConfig(buckets=next_buckets)

    def select_for_time(self, config: WakeResponseConfig, *, hour: int | None = None) -> SelectedWakeResponse | None:
        """按当前时段选择一条已合成唤醒回复。"""

        bucket = bucket_for_hour(hour if hour is not None else __import__("datetime").datetime.now().hour)
        playable_items = [
            item
            for item in config.buckets.get(bucket, [])
            if self.is_audio_current(item)
        ]
        if playable_items:
            item = self.random.choice(playable_items)
            return SelectedWakeResponse(bucket=bucket, text=item.text, audio_path=item.audio_path,
                                        voice_fingerprint=item.voice_fingerprint)
        return SelectedWakeResponse(bucket=bucket, text=DEFAULT_WAKE_RESPONSE_TEXT, audio_path=None)

    def read_audio(self, selected: SelectedWakeResponse) -> WakeResponseAudio | None:
        """读取选中回复的 wav 音频。"""

        if not self.is_audio_current(selected):
            return None
        audio_path = self.resolve_audio_path(selected.audio_path)
        if not audio_path.exists():
            return None
        with wave.open(str(audio_path), "rb") as wav_file:
            sample_rate = wav_file.getframerate()
            channels = wav_file.getnchannels()
            sample_width = wav_file.getsampwidth()
            pcm16 = wav_file.readframes(wav_file.getnframes())
        if channels != 1 or sample_width != 2:
            return None
        return WakeResponseAudio(pcm16=pcm16, sample_rate=sample_rate)

    def _normalize_items(self, raw_items: Any) -> list[WakeResponseItem]:
        """归一化单个时段内的短句列表。"""

        if not isinstance(raw_items, list):
            return []
        items: list[WakeResponseItem] = []
        for raw_item in raw_items:
            if isinstance(raw_item, str):
                text = raw_item.strip()
                if text:
                    items.append(WakeResponseItem(id=uuid4().hex, text=text))
                continue
            if not isinstance(raw_item, Mapping):
                continue
            text = str(raw_item.get("text", "")).strip()
            if not text:
                continue
            item_id = str(raw_item.get("id", "")).strip() or uuid4().hex
            audio_path = str(raw_item.get("audio_path") or "").strip() or None
            synthesized_at = raw_item.get("synthesized_at")
            voice_fingerprint = raw_item.get("voice_fingerprint")
            items.append(
                WakeResponseItem(
                    id=item_id,
                    text=text,
                    audio_path=audio_path,
                    synthesized_at=synthesized_at if isinstance(synthesized_at, (int, float)) else None,
                    voice_fingerprint=voice_fingerprint if isinstance(voice_fingerprint, str) else None,
                )
            )
        return items

    def _synthesize_item(
        self,
        *,
        bucket: str,
        item: WakeResponseItem,
        asset_dir: Path | None = None,
    ) -> WakeResponseItem:
        """合成单条文本并写入 wav 资产。"""

        fingerprint = self.voice_fingerprint_provider() if self.voice_fingerprint_provider else None
        if not fingerprint or self.synthesize_wav is None:
            raise RuntimeError("请先完成当前角色语音配置，再合成唤醒回复")
        raw_wav_bytes = self.synthesize_wav(item.text)
        # 合成可能跨越一次配置切换，旧音色结果不能挂在新指纹下面。
        if self.voice_fingerprint_provider() != fingerprint:
            raise RuntimeError("语音配置在合成期间发生变化，请再次点击合成")
        wav_bytes = trim_wav_leading_silence(raw_wav_bytes)
        # 每次合成写新文件，已经选中的旧回复不会在播放前被另一段文字覆盖。
        # 一次保存事务固定目标目录；角色切换发生在合成期间时，结果仍写回本次操作的角色目录。
        audio_path = (asset_dir or self.resolved_asset_dir()) / f"wake_{bucket}_{uuid4().hex}.wav"
        audio_path.write_bytes(wav_bytes)
        logger.info(
            "唤醒回复音频已合成并裁剪 bucket=%s item_id=%s text=%s raw_wav_bytes=%s trimmed_wav_bytes=%s audio_path=%s",
            bucket,
            item.id,
            item.text,
            len(raw_wav_bytes),
            len(wav_bytes),
            audio_path,
        )
        return WakeResponseItem(
            id=item.id,
            text=item.text,
            audio_path=str(audio_path),
            synthesized_at=self.now(),
            voice_fingerprint=fingerprint,
        )

    def _can_reuse_audio(self, *, previous_item: WakeResponseItem | None, next_item: WakeResponseItem) -> bool:
        """判断旧音频是否仍能复用。"""

        if previous_item is None:
            return False
        if previous_item.text != next_item.text:
            return False
        if not previous_item.audio_path:
            return False
        return self.is_audio_current(previous_item)

    def is_audio_current(self, item: WakeResponseItem | SelectedWakeResponse) -> bool:
        """只认可当前本机合成缓存或经过只读种子白名单核验的默认录音。

        Args:
            item: 待复用、试听或播放的唤醒回复。
        Returns:
            本机语音已就绪且音频资格有效时返回真；旧缓存、越界路径和未就绪语音返回假。
        """

        if not item.audio_path or self.voice_fingerprint_provider is None:
            return False
        current_fingerprint = self.voice_fingerprint_provider()
        if not current_fingerprint:
            return False
        audio_path = self.resolve_audio_path(item.audio_path)
        if audio_path.parent != self.resolved_asset_dir().resolve() or not audio_path.is_file():
            return False
        if item.voice_fingerprint:
            return item.voice_fingerprint == current_fingerprint
        # 预录音频不是本机合成结果；只有只读发行白名单可以为无指纹音频提供资格。
        return self.bundled_audio_validator is not None and self.bundled_audio_validator(item)

    def resolve_audio_path(self, audio_path: str) -> Path:
        """把便携文件名解析到当前角色的受管唤醒音频目录。

        Args:
            audio_path: 发行文件名或既有本机缓存绝对路径。
        Returns:
            解析后的绝对路径；调用方仍须核验目录边界和音频资格。
        """
        path = Path(audio_path)
        return (path if path.is_absolute() else self.resolved_asset_dir() / path).resolve()

    def remove_item(self, config: WakeResponseConfig, item_id: str) -> WakeResponseConfig:
        """删除指定短句和它的受管音频，不触发其他短句的合成。

        Args:
            config: 当前完整唤醒回复配置。
            item_id: 要删除的短句编号。
        Returns:
            删除后的配置；编号不存在时抛出 KeyError。
        """

        current_items = {item.id: item for items in config.buckets.values() for item in items}
        if item_id not in current_items:
            raise KeyError("wake response item not found")
        next_ids = set(current_items) - {item_id}
        self._remove_deleted_assets(current_items_by_id=current_items, next_ids=next_ids)
        return WakeResponseConfig(buckets=OrderedDict(
            (bucket, [item for item in items if item.id != item_id])
            for bucket, items in config.buckets.items()
        ))

    def _remove_deleted_assets(self, *, current_items_by_id: dict[str, WakeResponseItem], next_ids: set[str]) -> None:
        """删除配置中已移除文本对应的本地音频。"""

        for item_id, item in current_items_by_id.items():
            if item_id in next_ids or not item.audio_path:
                continue
            audio_path = self.resolve_audio_path(item.audio_path)
            if audio_path.exists() and audio_path.resolve().parent == self.resolved_asset_dir().resolve():
                audio_path.unlink()

    def _audio_url(self, item: WakeResponseItem, *, audio_url_base: str) -> str | None:
        """转换为控制台可试听的资产 URL。"""

        if not self.is_audio_current(item):
            return None
        return f"{audio_url_base.rstrip('/')}/{Path(item.audio_path).name}"

    def _item_payload(self, item: WakeResponseItem, *, include_audio_url: bool, audio_url_base: str) -> dict[str, object]:
        """转换单条配置，避免把派生 URL 写入持久化设置。"""

        payload: dict[str, object] = {
            "id": item.id,
            "text": item.text,
            "audio_path": item.audio_path,
            "synthesized_at": item.synthesized_at,
            "voice_fingerprint": item.voice_fingerprint,
        }
        if include_audio_url:
            payload["audio_url"] = self._audio_url(item, audio_url_base=audio_url_base)
            if payload["audio_url"] is None:
                payload.update(audio_path=None, synthesized_at=None, voice_fingerprint=None)
        return payload


def bucket_for_hour(hour: int) -> str:
    """把 0-23 点映射到唤醒回复时段。"""

    normalized_hour = hour % 24
    if 5 <= normalized_hour < 11:
        return "morning"
    if 11 <= normalized_hour < 18:
        return "afternoon"
    if 18 <= normalized_hour < 23:
        return "evening"
    return "night"
