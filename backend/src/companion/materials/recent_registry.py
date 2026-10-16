"""维护 NearMate 当前仍记得的有界本地材料路径能力。"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from threading import RLock
from pathlib import Path

from companion.materials.models import (
    MaterialAttachment,
    MaterialKind,
    MaterialMediaClass,
    MaterialNodeKind,
    normalize_material_path,
)


MAX_RECENT_MATERIAL_COUNT = 8


@dataclass(frozen=True, slots=True)
class RecentMaterial:
    """描述一项近期可再次交给 Background 的实时本地路径。

    Args:
        path: 用户正式提供的本地词法路径。
        display_name: 给用户和 Foreground 展示的名称。
        material_kind: 材料最初进入系统时的采集来源类型。
        node_kind: 用户提供时该路径是普通文件还是目录。
        media_class: 材料入口判定的媒体大类。
        last_used_sequence: 注册表内部用于 LRU 淘汰的递增序号。
    """

    path: str
    display_name: str
    material_kind: MaterialKind
    node_kind: MaterialNodeKind
    media_class: MaterialMediaClass
    last_used_sequence: int
    source_size: int | None = None
    source_mtime_ns: int | None = None

    def to_foreground_payload(self) -> dict[str, str]:
        """转换为 Foreground 可逐字选择的隐藏路径能力。

        Returns:
            只包含展示信息和本地路径的 Foreground 契约字段。
        """

        payload = {
            "display_name": self.display_name,
            "material_kind": self.material_kind,
            "node_kind": self.node_kind,
            "media_class": self.media_class,
            "path": self.path,
        }
        if self.source_size is not None:
            payload["source_size"] = str(self.source_size)
        if self.source_mtime_ns is not None:
            payload["source_mtime_ns"] = str(self.source_mtime_ns)
        return payload


class RecentMaterialRegistry:
    """线程安全地保存最多八项近期材料，并按最近使用顺序淘汰。"""

    def __init__(self, *, capacity: int = MAX_RECENT_MATERIAL_COUNT) -> None:
        """初始化空注册表。

        Args:
            capacity: 允许保留的最大路径数，生产环境固定为八，测试可缩小。

        Raises:
            ValueError: 容量不是正整数。
        """

        if not isinstance(capacity, int) or isinstance(capacity, bool) or capacity <= 0:
            raise ValueError("近期材料容量必须是正整数")
        self._capacity = capacity
        self._records: dict[str, RecentMaterial] = {}
        self._last_used_sequence = 0
        self._state_lock = RLock()

    def remember(self, attachments: Iterable[MaterialAttachment]) -> bool:
        """登记已经随正式用户输入成立的用户自持材料。

        受管资产会由 Desktop 在材料上下文释放后删除，因此不能成为持续路径能力。
        同一路径再次正式提供时更新展示元数据并移动到 LRU 队尾。

        Args:
            attachments: 已经提交成功的完整附件集合。

        Returns:
            至少新增或刷新一项记录时返回真。
        """

        changed = False
        with self._state_lock:
            for attachment in attachments:
                if attachment.managed_asset_id is not None:
                    continue
                normalized_path = normalize_material_path(attachment.path)
                if not normalized_path:
                    continue
                self._last_used_sequence += 1
                self._records.pop(normalized_path, None)
                source_size: int | None = None
                source_mtime_ns: int | None = None
                try:
                    source_stat = Path(attachment.path).stat()
                    source_size = source_stat.st_size
                    source_mtime_ns = source_stat.st_mtime_ns
                except OSError:
                    pass
                self._records[normalized_path] = RecentMaterial(
                    path=attachment.path,
                    display_name=attachment.display_name,
                    material_kind=attachment.kind,
                    node_kind=attachment.node_kind,
                    media_class=attachment.media_class,
                    last_used_sequence=self._last_used_sequence,
                    source_size=source_size,
                    source_mtime_ns=source_mtime_ns,
                )
                changed = True
                while len(self._records) > self._capacity:
                    self._records.pop(next(iter(self._records)))
        return changed

    def touch(self, paths: list[str]) -> bool:
        """刷新仍然存在的近期路径，不重新创建已经忘记的记录。

        Args:
            paths: Background 已成功校验并接受的本地路径。

        Returns:
            至少刷新一项现存记录时返回真。
        """

        changed = False
        with self._state_lock:
            for path in paths:
                normalized_path = normalize_material_path(path)
                existing = self._records.pop(normalized_path, None)
                if existing is None:
                    continue
                self._last_used_sequence += 1
                self._records[normalized_path] = RecentMaterial(
                    path=existing.path,
                    display_name=existing.display_name,
                    material_kind=existing.material_kind,
                    node_kind=existing.node_kind,
                    media_class=existing.media_class,
                    last_used_sequence=self._last_used_sequence,
                    source_size=existing.source_size,
                    source_mtime_ns=existing.source_mtime_ns,
                )
                changed = True
        return changed

    def forget(self, path: str) -> bool:
        """忘记一个路径，使它不能再用于未来的新任务。

        Args:
            path: 用户要求忘记的本地路径。

        Returns:
            路径原本存在并已删除时返回真。
        """

        normalized_path = normalize_material_path(path)
        if not normalized_path:
            return False
        with self._state_lock:
            return self._records.pop(normalized_path, None) is not None

    def clear(self) -> bool:
        """清空全部近期路径能力。

        Returns:
            清空前至少存在一项记录时返回真。
        """

        with self._state_lock:
            changed = bool(self._records)
            self._records.clear()
            return changed

    def snapshot(self) -> tuple[RecentMaterial, ...]:
        """按从旧到新的 LRU 顺序冻结当前全部记录。

        Returns:
            不会随注册表后续变化而改变的近期材料元组。
        """

        with self._state_lock:
            return tuple(self._records.values())
