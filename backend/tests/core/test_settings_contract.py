from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from companion.character_cards.store import CharacterCardStoreError
from companion.core.api.dependencies import get_settings_dependencies
from companion.core.api.routers import settings
from companion.core.api.schemas.settings import (
    AudioDeviceSettingsRequest,
    SettingsRequest,
    ShortcutStatusItemRequest,
    ShortcutStatusReportRequest,
)
from companion.core.broadcast import BroadcastHub
from companion.core.settings import Settings


@pytest.mark.parametrize(
    "card_error",
    [
        CharacterCardStoreError("角色卡结构无效"),
        OSError("角色卡文件暂时被占用"),
        UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"),
    ],
)
def test_settings_snapshot_survives_unreadable_active_character_card(
    card_error: Exception,
) -> None:
    """角色卡资产异常不能让 Console 的普通设置全部变成 500。"""

    settings_model = Settings()

    def load_active_card() -> object:
        raise card_error

    hub = SimpleNamespace(
        settings=settings_model,
        settings_revision=4,
        character_cards=SimpleNamespace(load_active_card=load_active_card),
    )

    snapshot = BroadcastHub.current_settings_payload(hub)

    assert snapshot["settings_revision"] == 4
    assert snapshot["active_character_caption_timing"] is None
    assert snapshot["user_self_intro"] == settings_model.user_self_intro


@pytest.mark.asyncio
async def test_audio_device_settings_route_returns_committed_settings_snapshot() -> None:
    """音频设备保存必须把提交事务生成的最新设置快照交给 Console。"""

    committed_snapshot = {
        "settings_revision": 8,
        "audio_device_mode": "manual",
        "audio_input_device_id": "microphone-2",
        "audio_input_device_name": "USB 麦克风",
        "audio_output_reference_device_id": "speaker-1",
        "audio_output_reference_device_name": "扬声器回环",
    }
    published_events: list[dict[str, object]] = []

    class AudioPublisher:
        async def publish(self, event: dict[str, object]) -> None:
            published_events.append(event)

    class UserSettings:
        async def commit_user_settings(self, **kwargs: object) -> dict[str, object]:
            after_commit = kwargs["after_commit"]
            assert callable(after_commit)
            await after_commit(committed_snapshot)
            return committed_snapshot

    container = SimpleNamespace(
        hub=SimpleNamespace(
            publisher=lambda domain: (
                AudioPublisher() if domain == "audio_devices" else None
            )
        ),
        user_settings=UserSettings(),
    )

    response = await settings.update_audio_device_settings(
        AudioDeviceSettingsRequest(
            mode="manual",
            microphone_id="microphone-2",
            microphone_name="USB 麦克风",
            speaker_loopback_id="speaker-1",
            speaker_loopback_name="扬声器回环",
        ),
        container,
    )

    assert response["accepted"] is True
    assert response["request_id"].startswith("audio-device-")
    assert response["settings"] is committed_snapshot
    assert published_events[0]["payload"]["selection"] == {
        "mode": "manual",
        "microphone_id": "microphone-2",
        "microphone_name": "USB 麦克风",
        "speaker_loopback_id": "speaker-1",
        "speaker_loopback_name": "扬声器回环",
    }


@pytest.mark.parametrize(
    "unsupported_field",
    [
        "speak_task_completion",
        "speak_permission_requests",
        "streamvox_device",
        "streamvox_llama_backend",
        "speaker_verification_enabled",
        "speaker_whitelist_audio_paths",
        "audio_device_mode",
        "audio_input_device_id",
        "audio_input_device_name",
        "audio_output_reference_device_id",
        "audio_output_reference_device_name",
    ],
)
def test_settings_api_rejects_fields_owned_by_other_endpoints(
    unsupported_field: str,
) -> None:
    """普通设置 patch 必须拒绝退役字段和其他领域拥有的字段。"""

    app = FastAPI()
    app.include_router(settings.router)
    app.dependency_overrides[get_settings_dependencies] = lambda: object()
    client = TestClient(app)

    response = client.post(
        "/api/settings",
        json={
            "expected_revision": 0,
            "show_speech_bubbles": True,
            "selected_live2d_model_id": "Haru",
            unsupported_field: True,
        },
    )

    assert response.status_code == 422
    assert any(
        issue["type"] == "extra_forbidden"
        and issue["loc"][-1] == unsupported_field
        for issue in response.json()["detail"]
    )


def test_settings_request_rejects_revision_above_safe_integer_bound() -> None:
    """普通设置乐观锁版本不能超出 JavaScript 安全整数范围。"""

    with pytest.raises(ValueError):
        SettingsRequest(
            expected_revision=2**53,
            show_speech_bubbles=True,
            selected_live2d_model_id="Haru",
        )


def test_shortcut_status_rejects_revision_above_safe_integer_bound() -> None:
    """快捷键报告的根版本和单项版本都必须处于同一安全范围。"""

    valid_item = {
        "shortcut_id": "followup_toggle",
        "accelerator": "CommandOrControl+Alt+Space",
        "enabled": True,
        "status": "registered",
        "settings_revision": 0,
        "checked_at": "2026-08-12T10:00:00+08:00",
        "message": "快捷键可用",
    }
    with pytest.raises(ValueError):
        ShortcutStatusReportRequest(
            settings_revision=2**53,
            items=[ShortcutStatusItemRequest(**valid_item)],
        )
    with pytest.raises(ValueError):
        ShortcutStatusItemRequest(
            **{
                **valid_item,
                "settings_revision": 2**53,
            }
        )
