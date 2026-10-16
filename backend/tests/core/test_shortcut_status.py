from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from companion.background_agent.config_commit_lock import BackgroundAgentConfigCommitLock
from companion.core.application.user_settings_reconfiguration import UserSettingsCoordinator
from companion.core.settings import Settings
from companion.core.shortcut_status import ShortcutStatusItem, ShortcutStatusStore


def _status_item(
    settings_revision: int,
    shortcut_id: str = "followup_toggle",
    *,
    checked_at: str = "2026-10-07T18:00:00+08:00",
    status: str = "registered",
) -> ShortcutStatusItem:
    """构造测试用的快捷键注册结果。"""

    return ShortcutStatusItem(
        shortcut_id=shortcut_id,  # type: ignore[arg-type]
        accelerator="CommandOrControl+Alt+F",
        enabled=True,
        status=status,  # type: ignore[arg-type]
        settings_revision=settings_revision,
        checked_at=checked_at,
        message="快捷键可用",
    )


def test_shortcut_status_store_hides_item_from_another_revision(tmp_path: Path) -> None:
    """设置版本变化后，旧注册结果必须降级为待检测。"""

    settings = Settings(user_settings_path=str(tmp_path / "user-settings.json"))
    store = ShortcutStatusStore()
    store.replace_items([_status_item(4)], settings_revision=4)

    payload = store.current_payload(settings=settings, settings_revision=5)

    followup_item = next(item for item in payload["items"] if item["shortcut_id"] == "followup_toggle")
    assert followup_item["settings_revision"] == 5
    assert followup_item["status"] == "unknown"
    assert followup_item["message"] == "等待桌面端检测"


def test_shortcut_status_store_rejects_older_same_revision_result(tmp_path: Path) -> None:
    """同一 revision 的迟到旧检查不能覆盖较新的桌面状态。"""

    settings = Settings(user_settings_path=str(tmp_path / "user-settings.json"))
    store = ShortcutStatusStore()
    store.replace_items(
        [_status_item(4, checked_at="2026-10-07T18:01:00+08:00", status="unavailable")],
        settings_revision=4,
    )

    changed = store.replace_items(
        [_status_item(4, checked_at="2026-10-07T18:00:00+08:00", status="registered")],
        settings_revision=4,
    )

    assert changed is False
    payload = store.current_payload(settings=settings, settings_revision=4)
    followup_item = next(item for item in payload["items"] if item["shortcut_id"] == "followup_toggle")
    assert followup_item["status"] == "unavailable"
    assert followup_item["checked_at"] == "2026-10-07T18:01:00+08:00"


def test_shortcut_status_store_orders_duplicate_ids_within_one_report(tmp_path: Path) -> None:
    """同一批次重复编号时也按 checked_at 选择较新的结果，而不是盲信列表顺序。"""

    settings = Settings(user_settings_path=str(tmp_path / "user-settings.json"))
    store = ShortcutStatusStore()
    changed = store.replace_items(
        [
            _status_item(4, checked_at="2026-10-07T18:01:00+08:00", status="unavailable"),
            _status_item(4, checked_at="2026-10-07T18:00:00+08:00", status="registered"),
        ],
        settings_revision=4,
    )

    assert changed is True
    payload = store.current_payload(settings=settings, settings_revision=4)
    followup_item = next(item for item in payload["items"] if item["shortcut_id"] == "followup_toggle")
    assert followup_item["status"] == "unavailable"


@pytest.mark.asyncio
async def test_coordinator_ignores_stale_shortcut_status_report(tmp_path: Path) -> None:
    """Coordinator 不应广播或缓存迟到的旧 revision 状态。"""

    settings = Settings(user_settings_path=str(tmp_path / "user-settings.json"))
    status_store = ShortcutStatusStore()
    published_events: list[dict[str, object]] = []

    class SettingsPublisher:
        async def publish(self, event: dict[str, object]) -> None:
            published_events.append(event)

    hub = SimpleNamespace(
        settings_revision=5,
        shortcut_status_store=status_store,
        publisher=lambda domain: SettingsPublisher() if domain == "settings" else None,
    )
    coordinator = UserSettingsCoordinator(
        settings=settings,
        hub=hub,
        background_agent_config_commit_lock=BackgroundAgentConfigCommitLock(
            tmp_path / "user-settings.json"
        ),
        configuration_mutation_lock=asyncio.Lock(),
    )

    await coordinator.report_shortcut_status(
        items=[_status_item(4)],
        settings_revision=4,
    )
    assert published_events == []
    stale_payload = await coordinator.current_shortcut_status_payload()
    assert all(item["status"] == "unknown" for item in stale_payload["items"])

    await coordinator.report_shortcut_status(
        items=[_status_item(5)],
        settings_revision=5,
    )
    assert len(published_events) == 1
    current_item = next(
        item
        for item in published_events[0]["payload"]["items"]
        if item["shortcut_id"] == "followup_toggle"
    )
    assert current_item["settings_revision"] == 5
    assert current_item["status"] == "registered"


@pytest.mark.asyncio
async def test_coordinator_does_not_rebroadcast_older_same_revision_status(
    tmp_path: Path,
) -> None:
    """同 revision 的迟到旧状态不应制造一次新的 Console 广播。"""

    settings = Settings(user_settings_path=str(tmp_path / "user-settings.json"))
    status_store = ShortcutStatusStore()
    published_events: list[dict[str, object]] = []

    class SettingsPublisher:
        async def publish(self, event: dict[str, object]) -> None:
            published_events.append(event)

    hub = SimpleNamespace(
        settings_revision=5,
        shortcut_status_store=status_store,
        publisher=lambda domain: SettingsPublisher() if domain == "settings" else None,
    )
    coordinator = UserSettingsCoordinator(
        settings=settings,
        hub=hub,
        background_agent_config_commit_lock=BackgroundAgentConfigCommitLock(
            tmp_path / "user-settings.json"
        ),
        configuration_mutation_lock=asyncio.Lock(),
    )

    await coordinator.report_shortcut_status(
        items=[
            _status_item(
                5,
                checked_at="2026-10-07T18:01:00+08:00",
                status="unavailable",
            )
        ],
        settings_revision=5,
    )
    await coordinator.report_shortcut_status(
        items=[
            _status_item(
                5,
                checked_at="2026-10-07T18:00:00+08:00",
                status="registered",
            )
        ],
        settings_revision=5,
    )

    assert len(published_events) == 1
    current_item = next(
        item
        for item in published_events[0]["payload"]["items"]  # type: ignore[index]
        if item["shortcut_id"] == "followup_toggle"
    )
    assert current_item["status"] == "unavailable"
