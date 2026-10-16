from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import pytest

from companion.background_agent.interaction_registry import (
    BackgroundAgentInteractionRegistry,
)
from companion.bootstrap.service import BootstrapService
from companion.core.application.errors import InvalidRequest
from companion.core.application.runtime_reconfiguration import (
    RuntimeReconfigurationCoordinator,
)
from companion.core.settings import Settings
from companion.llm.config import LlmSettings, ProviderCredential
from companion.llm.connectivity import (
    ProviderConnectivityError,
    ProviderConnectivityFailure,
)
from companion.records.bootstrap_secret_store import BootstrapSecretStore


class RecordingReplacement:
    """记录 LLM 配置事务是否提交或回滚。

    Args:
        commit_failure: 可选的候选运行时提交失败。
    """

    def __init__(self, *, commit_failure: Exception | None = None) -> None:
        self.commit_failure = commit_failure
        self.state = "prepared"
        self.commit_calls = 0
        self.rollback_calls = 0

    async def commit(self) -> object:
        """提交候选运行时，或在发布前模拟失败。

        Returns:
            成功提交时返回无业务含义的占位对象。

        Raises:
            Exception: 构造时注入的提交失败。
        """

        self.commit_calls += 1
        if self.commit_failure is not None:
            raise self.commit_failure
        self.state = "committed"
        return object()

    def rollback(self) -> bool:
        """记录候选资源已经被补偿回收。

        Returns:
            第一次回滚返回真。
        """

        self.rollback_calls += 1
        self.state = "rolled_back"
        return True


@pytest.mark.asyncio
async def test_connectivity_failure_does_not_mutate_memory_sqlite_or_runtime(
    tmp_path: Path,
) -> None:
    """连通性失败必须发生在设置、SQLite 和运行时候选变更之前。"""

    coordinator, settings, store, previous_json = _coordinator(tmp_path)
    runtime_preparations = 0

    async def fail_connectivity(_candidate: LlmSettings) -> None:
        """模拟 Qwen 厂商无法完成最小 Chat 请求。

        Args:
            _candidate: 已通过静态校验的候选设置。

        Raises:
            ProviderConnectivityError: 固定的安全连通性失败。
        """

        raise ProviderConnectivityError(
            (
                ProviderConnectivityFailure(
                    provider_id="qwen",
                    display_name="Qwen",
                    failure_type="authentication_failed",
                ),
            )
        )

    async def record_runtime_preparation(**_kwargs: object) -> RecordingReplacement:
        """记录不应发生的运行时候选构造。

        Returns:
            一个不会实际提交的记录事务。
        """

        nonlocal runtime_preparations
        runtime_preparations += 1
        return RecordingReplacement()

    coordinator.provider_connectivity_checker = fail_connectivity
    coordinator._prepare_runtime_replacement = record_runtime_preparation  # type: ignore[method-assign]

    with pytest.raises(InvalidRequest, match="Qwen.*authentication_failed"):
        await coordinator.update_llm_config(**_candidate_update())

    assert settings.llm == _previous_llm_settings()
    assert store.get_bootstrap_secret("llm_config_json") == previous_json
    assert runtime_preparations == 0


@pytest.mark.asyncio
async def test_runtime_commit_failure_restores_memory_and_sqlite(
    tmp_path: Path,
) -> None:
    """候选运行时发布前失败时必须恢复旧设置和精确持久化快照。"""

    coordinator, settings, store, previous_json = _coordinator(tmp_path)
    replacement = RecordingReplacement(
        commit_failure=RuntimeError("candidate start failed")
    )
    coordinator._prepare_runtime_replacement = AsyncMock(return_value=replacement)

    with pytest.raises(RuntimeError, match="candidate start failed"):
        await coordinator.update_llm_config(**_candidate_update())

    assert settings.llm == _previous_llm_settings()
    assert store.get_bootstrap_secret("llm_config_json") == previous_json
    assert replacement.commit_calls == 1
    assert replacement.rollback_calls == 1
    assert replacement.state == "rolled_back"


@pytest.mark.asyncio
async def test_successful_update_preserves_blank_keys_and_hot_replaces_once(
    tmp_path: Path,
) -> None:
    """空 Key 必须保留旧值，成功保存后只提交一次运行时替换。"""

    coordinator, settings, store, previous_json = _coordinator(tmp_path)
    replacement = RecordingReplacement()
    checked_candidates: list[LlmSettings] = []
    transaction_steps: list[str] = []

    async def record_connectivity(candidate: LlmSettings) -> None:
        """记录静态校验后的候选设置。

        Args:
            candidate: 尚未写入内存或 SQLite 的候选设置。
        """

        checked_candidates.append(candidate)
        transaction_steps.append("connectivity_checked")

    async def prepare_after_connectivity(**_kwargs: object) -> RecordingReplacement:
        """确认运行时候选只能在连通性检查完成后构造。

        Returns:
            当前测试使用的记录替换事务。
        """

        transaction_steps.append("runtime_prepared")
        return replacement

    coordinator.provider_connectivity_checker = record_connectivity
    coordinator._prepare_runtime_replacement = prepare_after_connectivity  # type: ignore[method-assign]
    coordinator.resume_runtime_consumers = Mock()
    coordinator.hub.bootstrap.status_payload = Mock(  # type: ignore[method-assign]
        return_value={"is_initialized": True}
    )

    payload = await coordinator.update_llm_config(**_candidate_update())

    assert len(checked_candidates) == 1
    assert transaction_steps == ["connectivity_checked", "runtime_prepared"]
    assert settings.llm.credentials["deepseek"].api_key == "old-deepseek-key"
    assert settings.llm.credentials["qwen"].api_key == "new-qwen-key"
    assert settings.llm.role_bindings["character_authoring"] is None
    assert store.get_bootstrap_secret("llm_config_json") != previous_json
    assert LlmSettings.model_validate_json(
        store.get_bootstrap_secret("llm_config_json") or ""
    ) == settings.llm
    assert replacement.commit_calls == 1
    assert replacement.rollback_calls == 0
    assert replacement.state == "committed"
    coordinator.resume_runtime_consumers.assert_called_once_with()
    assert payload["schema_version"] == 2
    assert payload["credentials"]["deepseek"]["api_key_configured"] is True


@pytest.mark.asyncio
async def test_invalid_binding_shape_is_reported_as_invalid_request(
    tmp_path: Path,
) -> None:
    """Pydantic 职责绑定校验失败必须留在应用错误边界，不能泄漏成 500。"""

    coordinator, settings, store, previous_json = _coordinator(tmp_path)
    connectivity_calls = 0

    async def record_connectivity(_candidate: LlmSettings) -> None:
        """记录无效结构不应进入的网络连通性检查。

        Args:
            _candidate: 已通过 schema 校验的候选设置。
        """

        nonlocal connectivity_calls
        connectivity_calls += 1

    coordinator.provider_connectivity_checker = record_connectivity
    invalid_update = _candidate_update()
    invalid_update["role_bindings"]["foreground"] = None

    with pytest.raises(InvalidRequest, match="必需 LLM 职责不可关闭"):
        await coordinator.update_llm_config(**invalid_update)

    assert settings.llm == _previous_llm_settings()
    assert store.get_bootstrap_secret("llm_config_json") == previous_json
    assert connectivity_calls == 0


def _coordinator(
    tmp_path: Path,
) -> tuple[
    RuntimeReconfigurationCoordinator,
    Settings,
    BootstrapSecretStore,
    str,
]:
    """构造使用真实设置与 SQLite、替换端口可注入的协调器。

    Args:
        tmp_path: Pytest 临时目录。

    Returns:
        协调器、内存设置、持久化仓储和旧 LLM JSON。
    """

    settings = Settings(
        bootstrap_db_path=str(tmp_path / "bootstrap.sqlite3"),
        user_settings_path=str(tmp_path / "user-settings.json"),
    )
    settings.llm = _previous_llm_settings()
    store = BootstrapSecretStore(settings.bootstrap_db_path)
    store.initialize()
    bootstrap = BootstrapService(secret_store=store, settings=settings)
    bootstrap.save_llm_settings(settings.llm)
    previous_json = store.get_bootstrap_secret("llm_config_json") or ""

    async def no_media_session_to_restart() -> bool:
        """表示事务测试没有活跃媒体会话。

        Returns:
            始终返回假。
        """

        return False

    async def connectivity_succeeds(_candidate: LlmSettings) -> None:
        """让非连通性事务测试无需访问真实 Provider。

        Args:
            _candidate: 已通过静态校验的候选设置。
        """

    coordinator = RuntimeReconfigurationCoordinator(
        settings=settings,
        hub=cast(Any, SimpleNamespace(bootstrap=bootstrap)),
        realtime_input_runtime_owner=cast(Any, object()),
        assistant_process_supervisor=cast(Any, object()),
        activity_service=cast(Any, object()),
        background_agent_interactions=BackgroundAgentInteractionRegistry(),
        interrupt_runtime_activities=lambda _runtime_epoch: [],
        release_runtime_memory_compaction=lambda _runtime_epoch: False,
        restart_livekit_media_session=no_media_session_to_restart,
        provider_connectivity_checker=connectivity_succeeds,
    )
    return coordinator, settings, store, previous_json


def _previous_llm_settings() -> LlmSettings:
    """构造保存前正在运行的合法 LLM 配置。

    Returns:
        使用两家旧 Key 且启用角色创作的设置。
    """

    return LlmSettings.model_validate(
        {
            "schema_version": 2,
            "credentials": {
                "deepseek": {
                    "base_url": "https://api.deepseek.com/v1",
                    "api_key": "old-deepseek-key",
                },
                "qwen": {
                    "base_url": (
                        "https://workspace.cn-beijing.maas.aliyuncs.com/"
                        "compatible-mode/v1"
                    ),
                    "api_key": "old-qwen-key",
                },
            },
            "role_bindings": {
                "foreground": "deepseek:deepseek-flash:chat",
                "summary": "deepseek:deepseek-flash:chat",
                "cognitive": "qwen:qwen3.6-plus:chat",
                "character_authoring": "deepseek:deepseek-flash:chat",
            },
        }
    )


def _candidate_update() -> dict[str, Any]:
    """构造带空 DeepSeek Key 的配置更新参数。

    Returns:
        可以直接展开给 `update_llm_config` 的凭据与职责绑定。
    """

    return {
        "credentials": {
            "deepseek": ProviderCredential(
                base_url="https://api.deepseek.com/v1/",
                api_key="",
            ),
            "qwen": ProviderCredential(
                base_url="workspace",
                api_key="new-qwen-key",
            ),
        },
        "role_bindings": {
            "summary": "deepseek:deepseek-v4-pro:chat",
            "summary": "deepseek:deepseek-flash:chat",
            "cognitive": "qwen:qwen3.6-plus:chat",
            "character_authoring": None,
        },
    }
