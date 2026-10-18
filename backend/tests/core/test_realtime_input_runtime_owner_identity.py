from __future__ import annotations

from threading import RLock
from types import SimpleNamespace
from uuid import UUID
from unittest.mock import Mock
import sys

import pytest

from companion.core.realtime_input_runtime_owner import RealtimeInputRuntimeOwner
from companion.realtime.interaction_state import InteractionState
from companion.realtime.realtime_input_runtime import RealtimeInputRuntime
from companion.realtime.runtime_identity import RuntimeIdentityFactory


class FakeInputRuntime:
    """提供 Owner 身份分配测试所需的最小输入 Runtime。"""

    def __init__(self) -> None:
        self.input_runtime_id = ""
        self.input_runtime_epoch = 0
        self.interaction_state = InteractionState()

    def bind_runtime_identity(
        self,
        *,
        input_runtime_id: str,
        input_runtime_epoch: int,
    ) -> None:
        """保存 Owner 分配的一次性 Runtime 身份。

        Args:
            input_runtime_id: 带业务前缀的 UUID 身份。
            input_runtime_epoch: Backend 进程内单调 epoch。
        """

        self.input_runtime_id = input_runtime_id
        self.input_runtime_epoch = input_runtime_epoch

    def runtime_identity_payload(self) -> dict[str, object]:
        """返回控制首帧和状态变化共用的身份载荷。"""

        return {
            "input_runtime_id": self.input_runtime_id,
            "input_runtime_epoch": self.input_runtime_epoch,
        }

    def interaction_state_event(self) -> dict[str, object]:
        """复用真实 Runtime 的快照构造，覆盖身份和状态一起发布。

        Args:
            无。

        Returns:
            当前完整交互快照事件。
        """

        return RealtimeInputRuntime.interaction_state_event(self)


class RecordingHub:
    """记录 Owner 正式提交后的输入身份广播。"""

    def __init__(self) -> None:
        self.interaction_state = SimpleNamespace()
        self.events: list[dict[str, object]] = []

    def publisher(self, domain: str) -> SimpleNamespace:
        """只允许 Owner 使用 interaction 领域。

        Args:
            domain: Owner 选择的公共事件领域。

        Returns:
            记录公共事件的异步 Publisher。
        """

        assert domain == "interaction"
        return SimpleNamespace(publish=self._publish)

    async def _publish(self, event: dict[str, object]) -> None:
        """记录一次正式身份广播。"""

        self.events.append(event)


def test_owner_construction_defers_model_loading(monkeypatch: pytest.MonkeyPatch) -> None:
    """即使启动配置已经齐备，构造 Owner 也不能抢先加载模型或下载资源。"""
    load = Mock(side_effect=AssertionError("不能在控制面构造期间加载模型"))
    monkeypatch.setattr(RealtimeInputRuntime, "from_settings", load)
    owner = RealtimeInputRuntimeOwner(settings=SimpleNamespace(), hub=RecordingHub(), runtime=None)
    assert owner.runtime is None
    assert owner.owns_runtime is True
    load.assert_not_called()


def test_owner_prepares_source_assets_only_for_owned_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    """真实自有 Owner 转发准备回调，外部注入的 Runtime 不下载模型。"""
    prepare = Mock()
    monkeypatch.setitem(sys.modules, "companion.runtime_assets.source",
        SimpleNamespace(prepare_source_runtime_assets=prepare))
    owner = RealtimeInputRuntimeOwner(settings=SimpleNamespace(), hub=RecordingHub(), runtime=None)
    progress, is_cancelled = Mock(), Mock(return_value=False)
    owner.prepare_source_assets(progress=progress, is_cancelled=is_cancelled)
    prepare.assert_called_once_with(progress=progress, is_cancelled=is_cancelled)
    prepare.reset_mock()
    external = RealtimeInputRuntimeOwner(settings=SimpleNamespace(), hub=RecordingHub(), runtime=FakeInputRuntime())
    external.prepare_source_assets(progress=progress, is_cancelled=is_cancelled)
    prepare.assert_not_called()


def build_owner(monkeypatch: pytest.MonkeyPatch) -> tuple[RealtimeInputRuntimeOwner, RecordingHub]:
    """构造不启动 ASR/VAD 线程的轻量 Runtime Owner。

    Args:
        monkeypatch: pytest 提供的运行期替换工具。

    Returns:
        可构造候选的 Owner 和广播记录器。
    """

    generated_uuids = iter(
        [
            UUID("00000000-0000-4000-8000-000000000002"),
            UUID("00000000-0000-4000-8000-000000000003"),
        ]
    )
    monkeypatch.setattr(
        RealtimeInputRuntime,
        "from_settings",
        staticmethod(lambda _settings, *, wakeword_keywords: FakeInputRuntime()),
    )
    hub = RecordingHub()
    owner = object.__new__(RealtimeInputRuntimeOwner)
    owner.settings = SimpleNamespace()
    owner.hub = hub
    owner._runtime_identity_factory = RuntimeIdentityFactory(
        uuid_factory=lambda: next(generated_uuids)
    )
    owner._identity_lock = RLock()
    owner._shutdown_lock = RLock()
    owner._pending_shutdown_runtimes = {}
    owner._next_input_runtime_epoch = 2
    owner.owns_runtime = True
    old_runtime = FakeInputRuntime()
    old_runtime.bind_runtime_identity(
        input_runtime_id="input-runtime-00000000-0000-4000-8000-000000000001",
        input_runtime_epoch=1,
    )
    owner.runtime = old_runtime
    owner.attach_wake_response_runtime = lambda runtime: runtime  # type: ignore[method-assign]
    owner.active_wakeword_keywords = lambda: ()  # type: ignore[method-assign]
    return owner, hub


@pytest.mark.asyncio
async def test_candidate_rollback_keeps_epoch_hole_and_commit_broadcasts_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """回滚候选不能公开身份或回收 epoch，下一次提交只广播新身份。"""

    owner, hub = build_owner(monkeypatch)
    rolled_back_candidate = owner.build_candidate()

    assert rolled_back_candidate is not None
    assert rolled_back_candidate.input_runtime_epoch == 2
    rolled_back_replacement = owner.publish_candidate(rolled_back_candidate)
    assert rolled_back_replacement is not None
    owner.restore_replacement(rolled_back_replacement)
    assert hub.events == []

    committed_candidate = owner.build_candidate()
    assert committed_candidate is not None
    assert committed_candidate.input_runtime_epoch == 3
    owner.publish_candidate(committed_candidate)
    await owner.broadcast_current_runtime_identity()

    assert hub.events == [
        {
            "type": "input.runtime_changed",
            "payload": {
                "input_runtime_id": (
                    "input-runtime-00000000-0000-4000-8000-000000000003"
                ),
                "input_runtime_epoch": 3,
            },
        },
        committed_candidate.interaction_state_event(),
    ]


def test_publish_rejects_candidate_without_process_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """发布入口必须拒绝绕过 Owner 身份分配的外部候选。"""

    owner, _hub = build_owner(monkeypatch)

    with pytest.raises(RuntimeError, match="发布前必须完成身份分配"):
        owner.publish_candidate(FakeInputRuntime())  # type: ignore[arg-type]


def test_attach_wake_response_runtime_tracks_active_character_directory() -> None:
    """输入 Runtime 的唤醒服务必须跟随当前角色目录，而不是启动时目录。"""
    class WakeService:
        """记录 Owner 注入的动态目录提供器。"""

        asset_dir_provider = None
        voice_fingerprint_provider = None

    active_card = {"id": "old-card"}
    wake_service = WakeService()
    hub = SimpleNamespace(
        wake_response_service=wake_service,
        bootstrap=SimpleNamespace(
            status_payload=lambda: {"voice": {"fingerprint": "f" * 64}}
        ),
        character_cards=SimpleNamespace(load_active_card_id=lambda: active_card["id"]),
        character_card_assets=SimpleNamespace(
            wake_response_audio_dir=lambda card_id: f"wake/{card_id}"
        ),
    )
    owner = object.__new__(RealtimeInputRuntimeOwner)
    owner.hub = hub
    runtime = SimpleNamespace()

    owner.attach_wake_response_runtime(runtime)  # type: ignore[arg-type]
    assert runtime.wake_response_service is wake_service
    assert wake_service.asset_dir_provider() == "wake/old-card"
    assert wake_service.voice_fingerprint_provider() == "f" * 64
    active_card["id"] = "new-card"
    assert wake_service.asset_dir_provider() == "wake/new-card"


def test_attach_wake_response_runtime_uses_internal_voice_status_when_public_projection_redacts_fingerprint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """生产态唤醒缓存资格必须读取内部 TTS 状态，而不是脱敏 Bootstrap 快照。"""

    monkeypatch.setenv("NEARMATE_PACKAGED", "1")

    class WakeService:
        """记录 Owner 注入的音频资格提供器。"""

        asset_dir_provider = None
        voice_fingerprint_provider = None

    wake_service = WakeService()
    hub = SimpleNamespace(
        wake_response_service=wake_service,
        bootstrap=SimpleNamespace(
            # 公开投影故意不带 fingerprint；这是控制端真实看到的形状。
            status_payload=lambda: {
                "voice": {
                    "provider": "local_streamvox",
                    "is_ready": True,
                    "phase": "ready",
                    "has_confirmed_voice": True,
                }
            }
        ),
        character_cards=SimpleNamespace(load_active_card_id=lambda: "huohua"),
        character_card_assets=SimpleNamespace(
            wake_response_audio_dir=lambda _card_id: "wake/huohua"
        ),
    )
    owner = object.__new__(RealtimeInputRuntimeOwner)
    owner.hub = hub
    owner._voice_status_provider = lambda: {
        "provider": "local_streamvox",
        "is_ready": True,
        "phase": "ready",
        "fingerprint": "f" * 64,
    }

    owner.attach_wake_response_runtime(SimpleNamespace())  # type: ignore[arg-type]

    assert wake_service.voice_fingerprint_provider() == "f" * 64


def test_packaged_runtime_does_not_fallback_to_development_fingerprint_when_internal_voice_is_unready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """安装态内部语音未确认时必须拒绝缓存，不能用开发指纹绕过语音门禁。"""

    monkeypatch.setenv("NEARMATE_PACKAGED", "1")
    owner = object.__new__(RealtimeInputRuntimeOwner)
    owner.hub = SimpleNamespace(
        bootstrap=SimpleNamespace(
            status_payload=lambda: {
                "voice": {
                    "provider": "local_streamvox",
                    "is_ready": False,
                    "phase": "repair_required",
                    "has_confirmed_voice": True,
                    # 模拟旧版公开快照残留的指纹，验证内部未就绪状态优先。
                    "fingerprint": "legacy-fingerprint",
                }
            }
        ),
        character_cards=SimpleNamespace(load_active_card=lambda: object()),
        character_card_assets=SimpleNamespace(resolve_voice_reference_audio=lambda _card: None),
    )
    owner._voice_status_provider = lambda: {
        "provider": "local_streamvox",
        "is_ready": False,
        "phase": "repair_required",
        "fingerprint": None,
    }

    assert owner._active_voice_fingerprint() is None


def test_development_runtime_keeps_fingerprint_fallback_when_internal_voice_is_unready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """源码态内部语音尚未落盘时，仍保留原有开发指纹兜底。"""

    monkeypatch.delenv("NEARMATE_PACKAGED", raising=False)
    monkeypatch.setattr(
        "companion.core.realtime_input_runtime_owner.development_voice_fingerprint",
        lambda **_kwargs: "development-fingerprint",
    )
    owner = object.__new__(RealtimeInputRuntimeOwner)
    owner.hub = SimpleNamespace(
        bootstrap=SimpleNamespace(
            status_payload=lambda: {
                "voice": {
                    "is_ready": False,
                    "fingerprint": "legacy-public-fingerprint",
                }
            }
        ),
        character_cards=SimpleNamespace(load_active_card=lambda: object()),
        character_card_assets=SimpleNamespace(resolve_voice_reference_audio=lambda _card: None),
    )
    owner._voice_status_provider = lambda: {
        "is_ready": False,
        "fingerprint": None,
    }

    assert owner._active_voice_fingerprint() == "development-fingerprint"
