from __future__ import annotations

from companion.contracts.events import load_event_catalog


def test_shared_event_catalog_has_no_retired_worker_partition() -> None:
    """验证当前共享协议可加载且只保留 UI 与核心进程分区。"""

    catalog = load_event_catalog()

    assert set(catalog.__dataclass_fields__) == {"ui_to_core", "core_to_ui"}
    assert catalog.ui_to_core
    assert catalog.core_to_ui
    assert all(
        "worker" not in definition.name
        for definition in (*catalog.ui_to_core, *catalog.core_to_ui)
    )


def test_shared_event_catalog_assigns_existing_control_events_to_one_domain() -> None:
    """现有音频、追问、助手失败和快捷键事件必须保留正确公共领域。"""

    catalog = load_event_catalog()
    domains_by_name = {
        definition.name: definition.domain for definition in catalog.core_to_ui
    }

    assert domains_by_name["followup.state_changed"] == "interaction"
    assert domains_by_name["interaction.state_changed"] == "interaction"
    assert domains_by_name["assistant.request_failed"] == "assistant_output"
    assert domains_by_name["settings.shortcut_status_changed"] == "settings"
    assert domains_by_name["audio.devices_change_requested"] == "audio_devices"
    assert domains_by_name["audio.devices_apply_result"] == "audio_devices"
