from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from companion.core.settings import Settings


ShortcutId = Literal["followup_toggle", "avatar_visibility", "selection_capture"]
ShortcutStatus = Literal["registered", "disabled", "internal_conflict", "unavailable", "unknown"]

SHORTCUT_IDS: tuple[ShortcutId, ...] = (
    "followup_toggle",
    "avatar_visibility",
    "selection_capture",
)


@dataclass(frozen=True)
class ShortcutStatusItem:
    """描述单个桌面全局快捷键的运行期注册状态。"""

    shortcut_id: ShortcutId
    accelerator: str
    enabled: bool
    status: ShortcutStatus
    settings_revision: int
    checked_at: str
    message: str

    def to_payload(self) -> dict[str, object]:
        """转换为控制台 API 可直接返回的字典。"""

        return {
            "shortcut_id": self.shortcut_id,
            "accelerator": self.accelerator,
            "enabled": self.enabled,
            "status": self.status,
            "settings_revision": self.settings_revision,
            "checked_at": self.checked_at,
            "message": self.message,
        }


class ShortcutStatusStore:
    """保存 desktop main 上报的快捷键注册结果。

    这份状态只描述当前运行环境，因此只保存在内存中，不写入用户设置文件。
    """

    def __init__(self) -> None:
        self._items: dict[ShortcutId, ShortcutStatusItem] = {}

    def replace_items(
        self,
        items: list[ShortcutStatusItem],
        *,
        settings_revision: int,
    ) -> bool:
        """只接收属于当前设置版本的桌面检测结果。

        Args:
            items: Desktop main 针对同一设置版本上报的快捷键状态。
            settings_revision: Backend 当前已经提交的设置版本。

        Returns:
            如果至少有一项状态比缓存更新则返回 ``True``；旧版本、未来版本
            或同一版本里更早的迟到结果都会被丢弃。
        """

        changed = False
        for item in items:
            # Desktop 的每次注册都会异步上报；旧请求可能晚于新请求抵达。
            # 先按版本过滤，避免一个迟到的状态把新快捷键误标成“可用”。
            if item.settings_revision != settings_revision:
                continue

            current_item = self._items.get(item.shortcut_id)
            if current_item is not None and not _is_shortcut_status_newer(
                current_item,
                item,
            ):
                # 同一 revision 也可能有多次检查（例如 Desktop 重连或
                # 同一批次重复上报）。checked_at 较早的结果不能覆盖较新的
                # 注册事实；同一时间戳则保守保留先到达的状态。
                continue

            self._items[item.shortcut_id] = item
            changed = True

        return changed

    def current_payload(self, *, settings: Settings, settings_revision: int) -> dict[str, object]:
        """读取当前快捷键状态快照。

        Args:
            settings: 当前用户设置，用于补齐尚未上报时的快捷键名称和启用状态。
            settings_revision: 当前设置修订号。

        Returns:
            包含三类快捷键状态的 API 载荷。
        """

        return {
            "items": [
                self._current_item_or_unknown(
                    shortcut_id,
                    settings=settings,
                    settings_revision=settings_revision,
                ).to_payload()
                for shortcut_id in SHORTCUT_IDS
            ]
        }

    def _current_item_or_unknown(
        self,
        shortcut_id: ShortcutId,
        *,
        settings: Settings,
        settings_revision: int,
    ) -> ShortcutStatusItem:
        """读取与当前 revision 对齐的状态，旧缓存统一降级为 unknown。"""

        item = self._items.get(shortcut_id)
        if item is not None and item.settings_revision == settings_revision:
            return item
        return self._unknown_item(shortcut_id, settings, settings_revision)

    def _unknown_item(
        self,
        shortcut_id: ShortcutId,
        settings: Settings,
        settings_revision: int,
    ) -> ShortcutStatusItem:
        """为尚未收到 desktop 上报的快捷键构造 unknown 状态。"""

        enabled, accelerator = shortcut_settings_from_runtime_settings(settings, shortcut_id)
        return ShortcutStatusItem(
            shortcut_id=shortcut_id,
            accelerator=accelerator,
            enabled=enabled,
            status="unknown",
            settings_revision=settings_revision,
            checked_at="",
            message="等待桌面端检测",
        )


def shortcut_settings_from_runtime_settings(settings: Settings, shortcut_id: ShortcutId) -> tuple[bool, str]:
    """从 Settings 中读取指定快捷键的启用状态和组合键。"""

    if shortcut_id == "followup_toggle":
        return settings.followup_toggle_shortcut_enabled, settings.followup_toggle_shortcut
    if shortcut_id == "avatar_visibility":
        return settings.avatar_visibility_shortcut_enabled, settings.avatar_visibility_shortcut
    return settings.selection_capture_shortcut_enabled, settings.selection_capture_shortcut


def _is_shortcut_status_newer(
    current_item: ShortcutStatusItem,
    candidate_item: ShortcutStatusItem,
) -> bool:
    """判断候选状态是否能推进单个快捷键的事实。

    同一个 ``settings_revision`` 可能经历多次 Desktop 检查。状态合同没有
    单独的序列号，因此使用 ``checked_at`` 作为同版本的单调排序键；无法解析
    的时间只允许替换空时间，不能让不可信值覆盖已经确认的结果。
    """

    if candidate_item.settings_revision != current_item.settings_revision:
        return candidate_item.settings_revision > current_item.settings_revision

    current_checked_at = _parse_checked_at(current_item.checked_at)
    candidate_checked_at = _parse_checked_at(candidate_item.checked_at)
    if current_checked_at is None or candidate_checked_at is None:
        return current_checked_at is None and candidate_checked_at is not None
    return candidate_checked_at > current_checked_at


def _parse_checked_at(value: str) -> datetime | None:
    """把状态时间归一为可比较的 UTC 时间；空值或非法值返回 ``None``。"""

    normalized = value.strip()
    if not normalized:
        return None
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
