"""验证组合根装配可见 Self-Reflection 所需的共享依赖。"""

import shutil
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from companion.core.application.container import AppBuildOptions, build_application_container
from companion.core.settings import Settings
from companion.llm.config import LlmSettings, ProviderCredential


def isolated_settings(tmp_path: Path) -> Settings:
    """创建不会写入开发环境的最小测试设置。"""

    cards = tmp_path / "character-cards"
    shutil.copytree(Path(__file__).resolve().parents[2] / "data" / "character-cards", cards)
    return Settings(
        bootstrap_db_path=str(tmp_path / "records.sqlite3"),
        background_agent_usage_db_path=str(tmp_path / "usage.sqlite3"),
        user_settings_path=str(tmp_path / "settings.json"),
        tool_waiting_policies_path=str(tmp_path / "tool-waiting.json"),
        character_cards_dir=str(cards),
        console_dist_dir=str(tmp_path / "missing-console"),
        livekit_api_key="",
        livekit_api_secret="",
    )


def test_container_shares_self_reflection_dependencies(tmp_path: Path) -> None:
    """Service、Runtime Event 和材料生命周期必须指向同一权威实例。"""

    container = build_application_container(AppBuildOptions(settings=isolated_settings(tmp_path)))

    assert container.runtime_event_service._self_reflection_service is container.self_reflection_service
    assert container.self_reflection_service._runtime_memory is container.runtime_memory_store
    assert container.self_reflection_service._material_lifecycle is container.material_lifecycle


def test_self_reflection_expression_sources_include_auxiliary_reply() -> None:
    """生产请求来源包含双输出域，且不再包含退役 result 表达。"""

    from companion.realtime.assistant_events import AssistantRequestSource

    assert set(AssistantRequestSource.__args__) == {
        "text_input",
        "final_asr",
        "system_notice",
        "timed_reminder_notice",
        "self_reflection_entry",
        "self_reflection_recovery",
        "self_reflection_interruption_reply",
    }


class RejoinRecordingController:
    """记录重建与重新入房调用的媒体会话控制器替身。

    Args:
        restart_result: `restart_current_session` 固定返回的重建结果。
    """

    def __init__(self, *, restart_result: bool) -> None:
        """初始化空入房记录与固定重建结果。

        Args:
            restart_result: 模拟已保存连接事实能否重建会话。
        """

        self.restart_result = restart_result
        self.ensure_calls: list[dict[str, object]] = []

    def enqueue_timed_reminder_notice(self) -> bool:
        """测试不交付独立 Reminder Notice。

        Returns:
            固定返回假。
        """

        return False

    def cancel_timed_reminder_requests(self, _request_ids: tuple[str, ...]) -> None:
        """吸收提醒请求取消。

        Args:
            _request_ids: 需要停止表达的独立提醒请求编号。
        """

    def interrupt_runtime_activities(
        self,
        _runtime_epoch: int,
    ) -> list[dict[str, object]]:
        """测试没有需要收口的旧世代活动。

        Args:
            _runtime_epoch: 已失效的助手运行时世代。

        Returns:
            固定的空材料释放事件列表。
        """

        return []

    async def restart_current_session(self) -> bool:
        """按测试预设返回能否用已保存连接事实重建。

        Returns:
            构造时给定的固定结果。
        """

        return self.restart_result

    async def ensure_session_started(self, **connection: object) -> None:
        """记录一次按预热语义的重新入房。

        Args:
            connection: 完整入房连接事实。
        """

        self.ensure_calls.append(connection)


def _settings_with_livekit(tmp_path: Path) -> Settings:
    """在隔离设置上启用 LiveKit 凭据，让组合根装配真实会话服务。

    Args:
        tmp_path: 测试临时目录。

    Returns:
        带非空 LiveKit 凭据的测试设置。
    """

    return isolated_settings(tmp_path).model_copy(
        update={"livekit_api_key": "test-key", "livekit_api_secret": "test-secret"}
    )


@pytest.mark.asyncio
async def test_restart_media_session_rejoins_room_after_connection_facts_erased(
    tmp_path: Path,
) -> None:
    """授权清理抹掉连接事实后，替换事务的媒体重建必须按预热语义重新入房。"""

    controller = RejoinRecordingController(restart_result=False)
    container = build_application_container(
        AppBuildOptions(
            settings=_settings_with_livekit(tmp_path),
            livekit_media_session_controller=controller,
        )
    )

    restarted = await container.runtime_reconfiguration.restart_livekit_media_session()

    assert restarted is True
    assert len(controller.ensure_calls) == 1
    # 房间名与身份必须来自产品固定约定，与 Desktop 汇合的房间不能漂移。
    assert controller.ensure_calls[0]["room_name"] == "companion-v1-local"
    assert (
        controller.ensure_calls[0]["identity"]
        == container.settings.livekit_media_session_identity
    )


@pytest.mark.asyncio
async def test_restart_media_session_keeps_saved_connection_fast_path(
    tmp_path: Path,
) -> None:
    """已保存连接事实可重建时，不得再经过会话服务重新签发。"""

    controller = RejoinRecordingController(restart_result=True)
    container = build_application_container(
        AppBuildOptions(
            settings=_settings_with_livekit(tmp_path),
            livekit_media_session_controller=controller,
        )
    )

    restarted = await container.runtime_reconfiguration.restart_livekit_media_session()

    assert restarted is True
    assert controller.ensure_calls == []


@pytest.mark.asyncio
async def test_container_wires_runtime_consumer_resume_port(tmp_path: Path) -> None:
    """真实组合根必须把提醒交付和调度器接到迟到就绪恢复端口。"""

    container = build_application_container(
        AppBuildOptions(settings=isolated_settings(tmp_path))
    )

    resume_runtime_consumers = container.runtime_reconfiguration.resume_runtime_consumers
    assert resume_runtime_consumers is not None

    resume_runtime_consumers()
    try:
        assert container.timed_reminder_delivery._worker is not None
        assert container.timed_reminder_scheduler._task is not None
    finally:
        await container.timed_reminder_scheduler.shutdown()
        await container.timed_reminder_delivery.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("commit_fails", [False, True])
async def test_llm_key_commit_refreshes_status_even_when_readiness_is_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    commit_fails: bool,
) -> None:
    """真实组合根只在 Key 提交成功后重发安全状态，避免余额一直使用旧账户。

    Args:
        tmp_path: 隔离 SQLite 和角色卡的临时目录。
        monkeypatch: 替换联网检查和运行时候选，不访问厂商或启动子进程。
        commit_fails: 是否在候选运行时提交前模拟失败。

    Returns:
        无；验证提交、回滚、事件脱敏和正常去重边界。
    """

    container = build_application_container(
        AppBuildOptions(settings=isolated_settings(tmp_path))
    )
    previous_settings = LlmSettings(
        credentials={
            "deepseek": ProviderCredential(
                base_url="https://api.deepseek.com/v1", api_key="previous-deepseek-key"
            ),
            "qwen": ProviderCredential(
                base_url="https://workspace.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
                api_key="test-qwen-key",
            ),
        }
    )
    container.hub.bootstrap.save_llm_settings(previous_settings)
    store = container.hub.bootstrap.secret_store
    previous_json = store.get_bootstrap_secret("llm_config_json")
    previous_events = container.application_status.current_events()
    socket = SimpleNamespace(send_json=AsyncMock())
    await container.hub.subscribe_control(socket, lambda: [], scope="runtime")
    # 模拟应用启动时已经发布相同就绪状态，避免测试被首次广播掩盖。
    await container.application_status.publish_changed()
    socket.send_json.reset_mock()

    replacement = SimpleNamespace(state="prepared", rollback=Mock())

    async def commit_replacement() -> None:
        """模拟候选运行时提交点，失败时留给协调器回滚。"""

        if commit_fails:
            raise RuntimeError("candidate commit failed")
        replacement.state = "committed"

    replacement.commit = commit_replacement
    coordinator = container.runtime_reconfiguration
    monkeypatch.setattr(coordinator, "provider_connectivity_checker", AsyncMock())
    monkeypatch.setattr(coordinator, "_prepare_runtime_replacement", AsyncMock(return_value=replacement))
    monkeypatch.setattr(coordinator, "resume_runtime_consumers", Mock())
    candidate = previous_settings.model_copy(deep=True)
    candidate.credentials["deepseek"].api_key = "current-deepseek-key"

    if commit_fails:
        with pytest.raises(RuntimeError, match="candidate commit failed"):
            await coordinator.update_llm_config(
                credentials=candidate.credentials, role_bindings=candidate.role_bindings
            )
        socket.send_json.assert_not_awaited()
        replacement.rollback.assert_called_once_with()
        assert container.settings.llm == previous_settings
        assert store.get_bootstrap_secret("llm_config_json") == previous_json
        return

    await coordinator.update_llm_config(
        credentials=candidate.credentials, role_bindings=candidate.role_bindings
    )

    assert container.settings.llm == candidate
    assert LlmSettings.model_validate_json(
        store.get_bootstrap_secret("llm_config_json") or ""
    ) == candidate
    assert container.application_status.current_events() == previous_events
    emitted = [call.args[0] for call in socket.send_json.await_args_list]
    assert emitted == previous_events
    assert "previous-deepseek-key" not in repr(emitted)
    assert "current-deepseek-key" not in repr(emitted)
    # 强制刷新只用于显式事务提交，普通状态广播仍按快照去重。
    socket.send_json.reset_mock()
    await container.application_status.publish_changed()
    socket.send_json.assert_not_awaited()
