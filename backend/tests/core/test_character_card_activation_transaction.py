"""验证角色卡激活通过设置事务发布派生字幕配置。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock

import pytest

from companion.character_cards.store import CharacterCardStoreError
from companion.core.api.routers.character_cards import (
    activate_character_card,
    capture_character_card_voice_from_runtime,
    character_card_payload,
    list_character_cards,
)
from companion.core.settings import Settings


class StubCharacterCard:
    """提供激活路由需要的最小角色卡接口。

    Args:
        card_id: 角色卡稳定编号。
        wakewords: 激活后应应用的唤醒词。
    """

    def __init__(self, card_id: str, wakewords: tuple[str, ...]) -> None:
        self.meta = SimpleNamespace(
            card_id=card_id,
            wakeword=SimpleNamespace(keywords=list(wakewords)),
        )

    def model_dump(self, *, mode: str) -> dict[str, object]:
        """返回角色卡响应测试需要的最小 JSON。

        Args:
            mode: 路由要求的 Pydantic 序列化模式。

        Returns:
            只包含角色卡编号的安全对象。
        """

        assert mode == "json"
        return {"meta": {"card_id": self.meta.card_id}}


class StubCharacterCardStore:
    """记录当前激活角色并提供两张固定测试卡。"""

    def __init__(self) -> None:
        """初始化旧角色和待激活角色。"""

        self.cards = {
            "old-card": StubCharacterCard("old-card", ("旧唤醒词",)),
            "new-card": StubCharacterCard("new-card", ("新唤醒词",)),
        }
        self.active_card_id = "old-card"

    def load_card(self, card_id: str) -> StubCharacterCard:
        """按编号读取测试角色卡。

        Args:
            card_id: 目标角色卡编号。

        Returns:
            对应的测试角色卡。

        Raises:
            CharacterCardStoreError: 编号不存在时抛出。
        """

        try:
            return self.cards[card_id]
        except KeyError as exception:
            raise CharacterCardStoreError(f"角色卡不存在：{card_id}") from exception

    def load_active_card(self) -> StubCharacterCard:
        """返回当前激活的测试角色卡。

        Returns:
            当前激活角色卡。
        """

        return self.load_card(self.active_card_id)

    def load_active_card_id(self) -> str:
        """返回当前激活角色卡编号。

        Returns:
            当前激活角色卡编号。
        """

        return self.active_card_id

    def set_active_card_id(self, card_id: str) -> None:
        """把存在的角色卡设为当前角色。

        Args:
            card_id: 目标角色卡编号。
        """

        self.load_card(card_id)
        self.active_card_id = card_id


class StubWakewordRuntime:
    """记录激活和回滚阶段应用的唤醒词。"""

    def __init__(self) -> None:
        """初始化空调用记录。"""

        self.keyword_updates: list[tuple[str, ...]] = []

    def replace_wakeword_keywords(
        self,
        *,
        settings: Settings,
        realtime_audio_config: object,
        keywords: tuple[str, ...],
    ) -> None:
        """记录一次唤醒词运行态替换。

        Args:
            settings: 本次替换对应的设置快照。
            realtime_audio_config: 实时音频固定配置。
            keywords: 准备应用的角色唤醒词。
        """

        assert isinstance(settings, Settings)
        assert realtime_audio_config is not None
        self.keyword_updates.append(keywords)


class RecordingSettingsCoordinator:
    """执行路由传入的事务回调并记录空字段提交。

    Args:
        settings: 测试共享设置。
        fail_after_prepare: 是否在准备完成后模拟持久化失败。
    """

    def __init__(self, settings: Settings, *, fail_after_prepare: bool) -> None:
        """保存设置和失败开关。

        Args:
            settings: 测试共享设置。
            fail_after_prepare: 是否触发失败补偿。
        """

        self.settings = settings
        self.fail_after_prepare = fail_after_prepare
        self.prepared_patch: dict[str, object] | None = None

    async def commit_user_settings(
        self,
        *,
        prepare_update: Callable[[Settings], Mapping[str, object]],
        rollback_external: Callable[[Settings], None],
    ) -> dict[str, object]:
        """模拟统一事务的准备、失败补偿和提交结果。

        Args:
            prepare_update: 锁内准备角色激活并返回字段 patch 的回调。
            rollback_external: 提交失败时恢复角色资产与运行态的回调。

        Returns:
            成功时返回最小设置快照。

        Raises:
            OSError: 测试要求模拟设置持久化失败时抛出。
        """

        previous_settings = self.settings.model_copy(deep=True)
        self.prepared_patch = dict(prepare_update(self.settings))
        if self.fail_after_prepare:
            rollback_external(previous_settings)
            raise OSError("settings persistence failed")
        return {"settings_revision": 1}


def _activation_container(
    *,
    fail_after_prepare: bool,
) -> tuple[Any, StubCharacterCardStore, StubWakewordRuntime, RecordingSettingsCoordinator]:
    """构造角色激活路由需要的最小依赖集合。

    Args:
        fail_after_prepare: 是否在角色切换后模拟提交失败。

    Returns:
        路由容器、角色仓储、唤醒词运行时和事务记录器。
    """

    settings = Settings()
    cards = StubCharacterCardStore()
    wakeword_runtime = StubWakewordRuntime()
    coordinator = RecordingSettingsCoordinator(
        settings,
        fail_after_prepare=fail_after_prepare,
    )
    wake_response_service = SimpleNamespace(asset_dir=Path("old-wake-response"))
    hub = SimpleNamespace(
        character_cards=cards,
        character_card_assets=SimpleNamespace(
            wake_response_audio_dir=lambda card_id: Path(f"wake/{card_id}")
        ),
        wake_response_service=wake_response_service,
    )
    container = SimpleNamespace(
        hub=hub,
        user_settings=coordinator,
        realtime_input_runtime_owner=SimpleNamespace(runtime=wakeword_runtime),
        realtime_audio_config=object(),
        character_voice=SimpleNamespace(
            status=lambda _card: {"voice_match_status": "matched"}
        ),
    )
    return cast(Any, container), cards, wakeword_runtime, coordinator


@pytest.mark.asyncio
async def test_character_activation_commits_empty_patch_for_derived_settings() -> None:
    """角色变化没有直接字段 patch，也必须经过统一设置提交。"""

    container, cards, wakeword_runtime, coordinator = _activation_container(
        fail_after_prepare=False
    )

    response = await activate_character_card("new-card", container)

    assert coordinator.prepared_patch == {}
    assert cards.active_card_id == "new-card"
    assert wakeword_runtime.keyword_updates == [("新唤醒词",)]
    assert response["active_card_id"] == "new-card"
    assert response["settings"]["settings_revision"] == 1


@pytest.mark.asyncio
async def test_character_activation_keeps_success_when_post_commit_voice_status_fails() -> None:
    """提交成功后的语音状态读取失败不能把角色激活伪装成 HTTP 500。"""

    container, cards, _wakeword_runtime, _coordinator = _activation_container(
        fail_after_prepare=False
    )
    container.character_voice.status = Mock(
        side_effect=RuntimeError("StreamVox status request timed out")
    )

    response = await activate_character_card("new-card", container)

    assert cards.active_card_id == "new-card"
    assert response["active_card_id"] == "new-card"
    assert response["voice_match_status"] == "runtime_unavailable"


def test_character_card_payload_falls_back_when_active_marker_is_temporarily_unreadable() -> None:
    """附属 active.json 读取失败不能把已保存角色卡响应变成异常。"""

    card = StubCharacterCard("new-card", ("新唤醒词",))

    def fail_active_card_id() -> str:
        raise OSError("active marker is temporarily locked")

    container = SimpleNamespace(
        hub=SimpleNamespace(
            character_cards=SimpleNamespace(load_active_card_id=fail_active_card_id),
        ),
    )

    response = character_card_payload(
        container=cast(Any, container),
        card=cast(Any, card),
        effect_message="角色卡已保存",
    )

    assert response["active_card_id"] == "new-card"
    assert "settings" not in response


@pytest.mark.asyncio
async def test_character_card_list_survives_unreadable_active_marker() -> None:
    """角色卡列表读取 active.json 失败时仍应返回可编辑卡片。"""

    card = StubCharacterCard("new-card", ("新唤醒词",))

    def fail_active_card_id() -> str:
        raise OSError("active marker is temporarily locked")

    container = SimpleNamespace(
        hub=SimpleNamespace(
            character_cards=SimpleNamespace(
                list_cards=lambda: [card],
                load_active_card_id=fail_active_card_id,
            ),
        ),
    )

    response = await list_character_cards(cast(Any, container))

    assert response["cards"] == [{"meta": {"card_id": "new-card"}}]
    assert response["active_card_id"] == ""


@pytest.mark.asyncio
async def test_character_activation_rolls_back_assets_when_settings_commit_fails() -> None:
    """设置落盘失败时必须恢复旧角色、旧音频目录和旧唤醒词。"""

    container, cards, wakeword_runtime, _coordinator = _activation_container(
        fail_after_prepare=True
    )

    with pytest.raises(OSError, match="settings persistence failed"):
        await activate_character_card("new-card", container)

    assert cards.active_card_id == "old-card"
    assert container.hub.wake_response_service.asset_dir == Path(
        "old-wake-response"
    )
    assert wakeword_runtime.keyword_updates == [
        ("新唤醒词",),
        ("旧唤醒词",),
    ]


@pytest.mark.asyncio
async def test_runtime_voice_capture_commits_empty_patch_and_returns_captured_card() -> None:
    """运行时捕获必须推进共享 settings revision，并返回捕获后的角色卡。"""

    container, _cards, _wakeword_runtime, coordinator = _activation_container(
        fail_after_prepare=False
    )
    captured_card = StubCharacterCard("old-card", ("旧唤醒词",))
    container.character_voice = SimpleNamespace(
        prepare_capture_from_runtime=lambda _card: SimpleNamespace(card=captured_card),
        rollback_capture_from_runtime=lambda _capture: pytest.fail("不应回滚成功事务"),
        status=lambda _card: {"voice_match_status": "matched"},
    )

    response = await capture_character_card_voice_from_runtime("old-card", container)

    assert coordinator.prepared_patch == {}
    assert response["card"] == {"meta": {"card_id": "old-card"}}
    assert response["voice_match_status"] == "matched"
    assert response["settings"]["settings_revision"] == 1


@pytest.mark.asyncio
async def test_runtime_voice_capture_rolls_back_card_when_settings_commit_fails() -> None:
    """设置 revision 落盘失败时必须调用捕获补偿，避免留下半提交角色资产。"""

    container, _cards, _wakeword_runtime, _coordinator = _activation_container(
        fail_after_prepare=True
    )
    captured_card = StubCharacterCard("old-card", ("旧唤醒词",))
    rollback_capture = Mock()
    container.character_voice = SimpleNamespace(
        prepare_capture_from_runtime=lambda _card: SimpleNamespace(card=captured_card),
        rollback_capture_from_runtime=rollback_capture,
        status=lambda _card: {"voice_match_status": "matched"},
    )

    with pytest.raises(OSError, match="settings persistence failed"):
        await capture_character_card_voice_from_runtime("old-card", container)

    rollback_capture.assert_called_once()
