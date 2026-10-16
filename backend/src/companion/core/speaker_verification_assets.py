from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from companion.audio.pcm16_utils import pcm16_to_wav_bytes


MAX_SPEAKER_VERIFICATION_SAMPLE_COUNT = 10


@dataclass(frozen=True)
class SpeakerVerificationSample:
    """描述一段控制台录制的说话人白名单音频。

    参数：
        sample_id: 录音槽位的稳定编号。
        audio_path: 后端保存的音频文件绝对路径。
        created_at: 元数据写入时的文件系统时间戳。
    """

    sample_id: str
    audio_path: Path
    created_at: float

    def to_payload(self) -> dict[str, object]:
        """转换为控制台可消费的 JSON 载荷。

        返回：
            包含编号、试听地址和创建时间的字典。
        """

        return {
            "id": self.sample_id,
            "audio_url": f"/api/speaker-verification/samples/{self.sample_id}/audio",
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class SpeakerVerificationAssetSnapshot:
    """描述一次资产变更前的文件快照。

    参数：
        metadata: 变更前的元数据列表。
        files: 变更前每个音频文件名和二进制内容。
    """

    metadata: list[dict[str, Any]]
    files: dict[str, bytes]


class SpeakerVerificationAssetStore:
    """管理控制台说话人白名单录音资产。"""

    def __init__(self, root: str | Path) -> None:
        """创建说话人录音资产仓储。

        参数：
            root: 录音文件和元数据保存目录。
        """

        self.root = Path(root)
        self.metadata_path = self.root / "samples.json"

    def list_samples(self) -> list[SpeakerVerificationSample]:
        """读取当前全部录音样本。

        返回：
            按槽位编号排序的录音样本列表。
        """

        return [
            SpeakerVerificationSample(
                sample_id=item["id"],
                audio_path=(self.root / item["filename"]).resolve(),
                created_at=float(item["created_at"]),
            )
            for item in self._load_metadata()
        ]

    def save_sample(
        self,
        *,
        content: bytes,
        replace_sample_id: str | None = None,
    ) -> SpeakerVerificationSample:
        """保存或替换一段白名单录音。

        参数：
            content: 后端 VAD 裁剪后的 PCM16 有效人声字节。
            replace_sample_id: 可选的待替换槽位编号。

        返回：
            保存后的录音样本。
        """

        if not content:
            raise ValueError("说话人白名单录音不能为空")

        metadata = self._load_metadata()
        target_index = self._find_sample_index(metadata, replace_sample_id) if replace_sample_id else None
        if target_index is None and len(metadata) >= MAX_SPEAKER_VERIFICATION_SAMPLE_COUNT:
            raise ValueError("最多只能保存 10 段说话人白名单录音")

        self.root.mkdir(parents=True, exist_ok=True)
        sample_id = replace_sample_id if replace_sample_id is not None else self._next_sample_id(metadata)
        filename = f"{sample_id}.wav"
        audio_path = self.root / filename
        audio_path.write_bytes(pcm16_to_wav_bytes(content))

        created_at = audio_path.stat().st_mtime
        next_item = {"id": sample_id, "filename": filename, "created_at": created_at}
        if target_index is None:
            metadata.append(next_item)
        else:
            old_filename = metadata[target_index]["filename"]
            if old_filename != filename:
                old_path = self.root / old_filename
                if old_path.exists():
                    old_path.unlink()
            metadata[target_index] = next_item

        self._save_metadata(metadata)
        return SpeakerVerificationSample(sample_id=sample_id, audio_path=audio_path.resolve(), created_at=created_at)

    def delete_sample(self, sample_id: str) -> bool:
        """删除指定录音样本。

        参数：
            sample_id: 要删除的录音槽位编号。

        返回：
            删除成功返回 True，样本不存在返回 False。
        """

        metadata = self._load_metadata()
        target_index = next((index for index, item in enumerate(metadata) if item["id"] == sample_id), None)
        if target_index is None:
            return False

        item = metadata.pop(target_index)
        audio_path = self.root / item["filename"]
        if audio_path.exists():
            audio_path.unlink()
        self._save_metadata(metadata)
        return True

    def find_sample(self, sample_id: str) -> SpeakerVerificationSample | None:
        """按编号查找录音样本。

        参数：
            sample_id: 录音槽位编号。

        返回：
            找到时返回样本对象，否则返回 None。
        """

        return next((sample for sample in self.list_samples() if sample.sample_id == sample_id), None)

    def create_snapshot(self) -> SpeakerVerificationAssetSnapshot:
        """创建当前录音资产快照。

        返回：
            可用于失败回滚的元数据和音频文件内容。
        """

        metadata = self._load_metadata()
        files: dict[str, bytes] = {}
        for item in metadata:
            audio_path = self.root / item["filename"]
            if audio_path.exists():
                files[item["filename"]] = audio_path.read_bytes()
        return SpeakerVerificationAssetSnapshot(metadata=metadata, files=files)

    def restore_snapshot(self, snapshot: SpeakerVerificationAssetSnapshot) -> None:
        """恢复到指定录音资产快照。

        参数：
            snapshot: create_snapshot 返回的资产快照。
        """

        if self.root.exists():
            shutil.rmtree(self.root)
        self.root.mkdir(parents=True, exist_ok=True)
        for filename, content in snapshot.files.items():
            (self.root / filename).write_bytes(content)
        self._save_metadata(snapshot.metadata)

    def _load_metadata(self) -> list[dict[str, Any]]:
        """读取并清洗元数据列表。"""

        if not self.metadata_path.exists():
            return []

        raw_payload = json.loads(self.metadata_path.read_text(encoding="utf-8"))
        if not isinstance(raw_payload, list):
            raise ValueError(f"说话人白名单元数据必须是列表：{self.metadata_path}")

        metadata: list[dict[str, Any]] = []
        for item in raw_payload:
            if not isinstance(item, dict):
                continue
            sample_id = item.get("id")
            filename = item.get("filename")
            created_at = item.get("created_at")
            if not isinstance(sample_id, str) or not isinstance(filename, str):
                continue
            if not isinstance(created_at, int | float):
                continue
            metadata.append({"id": sample_id, "filename": filename, "created_at": float(created_at)})
        return sorted(metadata, key=lambda item: item["id"])

    def _save_metadata(self, metadata: list[dict[str, Any]]) -> None:
        """原子写入录音元数据。"""

        self.root.mkdir(parents=True, exist_ok=True)
        serialized_metadata = json.dumps(metadata, ensure_ascii=False, indent=2)
        temporary_path = self.metadata_path.with_name(f"{self.metadata_path.name}.tmp")
        temporary_path.write_text(f"{serialized_metadata}\n", encoding="utf-8")
        os.replace(temporary_path, self.metadata_path)

    @staticmethod
    def _find_sample_index(metadata: list[dict[str, Any]], sample_id: str | None) -> int | None:
        """查找样本在元数据列表中的位置。"""

        if sample_id is None:
            return None
        for index, item in enumerate(metadata):
            if item["id"] == sample_id:
                return index
        raise ValueError("说话人白名单录音不存在")

    @staticmethod
    def _next_sample_id(metadata: list[dict[str, Any]]) -> str:
        """生成最小可用样本槽位编号。"""

        used_ids = {item["id"] for item in metadata}
        for index in range(1, MAX_SPEAKER_VERIFICATION_SAMPLE_COUNT + 1):
            sample_id = f"sample-{index}"
            if sample_id not in used_ids:
                return sample_id
        raise ValueError("最多只能保存 10 段说话人白名单录音")
