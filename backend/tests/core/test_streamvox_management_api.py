"""验证 StreamVox 管理接口的硬阻塞 RPC 不会冻结 Backend 事件循环。"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
from fastapi import FastAPI

from companion.background_agent.config_commit_lock import BackgroundAgentConfigCommitLock
from companion.core.api.dependencies import get_streamvox_dependencies
from companion.core.api.exception_handlers import install_application_error_handlers
from companion.core.api.routers import streamvox
from companion.core.application.user_settings_reconfiguration import UserSettingsCoordinator
from companion.core.settings import Settings
from companion.realtime.assistant_process_client import AssistantProcessNotReadyError


class BlockingStreamVoxRuntime:
    """用线程事件模拟会硬阻塞的跨进程 StreamVox 管理 RPC。

    Args:
        runtime_state: RPC 放行后返回的固定运行状态。
    """

    def __init__(self, runtime_state: dict[str, object]) -> None:
        """初始化 RPC 阻塞边界与调用记录。

        Args:
            runtime_state: 模拟子进程当前上报的 StreamVox 运行状态。
        """

        self._runtime_state = runtime_state
        self.rpc_started = threading.Event()
        self.release_rpc = threading.Event()
        self.rpc_threads: list[threading.Thread] = []

    def get_streamvox_runtime_state(self, *, timeout: float = 5.0) -> dict[str, object]:
        """模拟底层 Queue.get 的硬阻塞等待。

        Args:
            timeout: 生产实现使用的等待秒数；测试只透传不消费。

        Returns:
            测试放行后返回固定运行状态。

        Raises:
            RuntimeError: 测试没有及时放行 RPC 时抛出，避免用例永久悬挂。
        """

        _ = timeout
        self.rpc_threads.append(threading.current_thread())
        self.rpc_started.set()
        if not self.release_rpc.wait(timeout=10.0):
            raise RuntimeError("测试未及时放行 StreamVox RPC")
        return dict(self._runtime_state)


class NotReadyStreamVoxRuntime:
    """模拟尚未完成 StreamVox 装配的助手运行时。"""

    def get_streamvox_runtime_state(self, *, timeout: float = 5.0) -> dict[str, object]:
        """返回可恢复的运行时未就绪错误。"""

        _ = timeout
        raise AssistantProcessNotReadyError("assistant runtime is not ready")


def _runtime_state_payload() -> dict[str, object]:
    """返回一份可被 StreamVoxRuntimeConfiguration 完整解析的运行状态。"""

    return {
        "model": "model-a",
        "device": "cpu",
        "llama_backend": None,
        "role_name": "role-a",
        "streamvox_seed": 42,
        "inference_options": {},
        "onnx_provider": None,
    }


def _build_app(container: SimpleNamespace) -> FastAPI:
    """构造只挂载 StreamVox 路由并注入测试容器的应用。

    Args:
        container: 提供 StreamVox 路由所需依赖切面的测试替身。

    Returns:
        可直接交给 ASGI 传输层的 FastAPI 应用。
    """

    app = FastAPI()
    install_application_error_handlers(app)
    app.include_router(streamvox.router)
    app.dependency_overrides[get_streamvox_dependencies] = lambda: container
    return app


async def _assert_loop_stays_responsive_while_rpc_blocked(
    runtime: BlockingStreamVoxRuntime,
) -> None:
    """在 RPC 仍被测试扣留时证明事件循环仍可推进其它协程。

    Args:
        runtime: 当前正被请求占用的阻塞 RPC 替身。
    """

    probe_completed = asyncio.Event()

    async def probe() -> None:
        """连续让出多次执行权；事件循环被冻结时该探针不可能完成。"""

        for _ in range(5):
            await asyncio.sleep(0.01)
        probe_completed.set()

    asyncio.create_task(probe())
    started = await asyncio.to_thread(runtime.rpc_started.wait, 2.0)
    assert started
    # RPC 尚未放行；若 handler 在事件循环线程里硬阻塞，探针一秒内不可能跑完。
    await asyncio.wait_for(probe_completed.wait(), timeout=1.0)
    assert not runtime.release_rpc.is_set()


@pytest.mark.asyncio
async def test_runtime_state_handler_keeps_event_loop_responsive_during_blocking_rpc() -> None:
    """读取运行状态的 RPC 必须在线程池等待，不能冻结控制面事件循环。"""

    runtime = BlockingStreamVoxRuntime(_runtime_state_payload())
    container = SimpleNamespace(
        assistant_process_supervisor=SimpleNamespace(
            require_for_streamvox_management=lambda: runtime,
        ),
    )
    app = _build_app(container)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        slow_request = asyncio.create_task(client.get("/api/streamvox/runtime"))
        await _assert_loop_stays_responsive_while_rpc_blocked(runtime)
        runtime.release_rpc.set()
        response = await slow_request

    assert response.status_code == 200
    assert response.json() == _runtime_state_payload()
    # RPC 必须在非事件循环线程执行，否则等待期间控制广播和授权监控会被一起冻结。
    assert runtime.rpc_threads
    assert all(
        rpc_thread is not threading.main_thread()
        for rpc_thread in runtime.rpc_threads
    )


@pytest.mark.asyncio
async def test_runtime_state_handler_maps_startup_race_to_service_unavailable() -> None:
    """运行时装配窗口返回 503，不应伪装成通用 500。"""

    runtime = NotReadyStreamVoxRuntime()
    container = SimpleNamespace(
        assistant_process_supervisor=SimpleNamespace(
            require_for_streamvox_management=lambda: runtime,
        ),
    )
    app = _build_app(container)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.get("/api/streamvox/runtime")

    assert response.status_code == 503
    assert response.json() == {"detail": "assistant runtime is not ready"}


@pytest.mark.asyncio
async def test_config_commit_keeps_event_loop_responsive_during_blocking_rpc(
    tmp_path: Path,
) -> None:
    """设置事务内的 StreamVox 切换准备同样必须离开事件循环线程。"""

    runtime = BlockingStreamVoxRuntime(_runtime_state_payload())
    settings = Settings(user_settings_path=str(tmp_path / "user-settings.json"))
    saved_snapshots: list[dict[str, object]] = []

    class SettingsStore:
        """记录事务提交的设置快照，不真正写盘。"""

        def save(self, snapshot: dict[str, object]) -> None:
            """保存本次提交快照。

            Args:
                snapshot: 协调器生成的完整用户设置载荷。
            """

            saved_snapshots.append(snapshot)

    class Hub:
        """提供设置事务需要的最小持久化与广播端口。"""

        settings_revision = 3
        user_settings_store = SettingsStore()

        def current_settings_payload(
            self,
            *,
            settings_revision: int | None = None,
        ) -> dict[str, object]:
            """按测试设置对象生成完整快照。

            Args:
                settings_revision: 本次提交的下一版本号。

            Returns:
                包含全部用户设置字段的快照。
            """

            payload = {
                field_name: getattr(settings, field_name)
                for field_name in (
                    "show_speech_bubbles",
                    "followup_toggle_shortcut_enabled",
                    "followup_toggle_shortcut",
                    "avatar_visibility_shortcut_enabled",
                    "avatar_visibility_shortcut",
                    "selection_capture_shortcut_enabled",
                    "selection_capture_shortcut",
                    "selected_live2d_model_id",
                    "user_self_intro",
                    "user_extra_notes",
                    "streamvox_device",
                    "streamvox_llama_backend",
                    "speaker_verification_enabled",
                    "speaker_whitelist_audio_paths",
                    "audio_input_device_id",
                    "audio_input_device_name",
                    "audio_output_reference_device_id",
                    "audio_output_reference_device_name",
                    "audio_device_mode",
                )
            }
            payload["background_agent"] = settings.background_agent.model_dump(
                mode="json"
            )
            payload["settings_revision"] = (
                settings_revision if settings_revision is not None else self.settings_revision
            )
            return payload

        def publisher(self, domain: str) -> "Hub":
            """返回同时充当 Publisher 的测试 Hub。

            Args:
                domain: 协调器申请的事件领域；设置事务只允许 settings。

            Returns:
                当前 Hub 自身。
            """

            assert domain == "settings"
            return self

        async def publish(self, _event: dict[str, object]) -> None:
            """吸收 settings.changed 广播。"""

    coordinator = UserSettingsCoordinator(
        settings=settings,
        hub=cast(Any, Hub()),
        background_agent_config_commit_lock=BackgroundAgentConfigCommitLock(
            tmp_path / "user-settings.json"
        ),
    )
    container = SimpleNamespace(
        assistant_process_supervisor=SimpleNamespace(
            require_for_streamvox_management=lambda: runtime,
        ),
        user_settings=coordinator,
    )
    app = _build_app(container)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        slow_request = asyncio.create_task(
            client.post(
                "/api/streamvox/config",
                json={
                    "model": "model-a",
                    "device": "cpu",
                    "llama_backend": None,
                    "role_name": "role-a",
                    "streamvox_seed": 42,
                    "inference_options": {},
                },
            )
        )
        await _assert_loop_stays_responsive_while_rpc_blocked(runtime)
        runtime.release_rpc.set()
        response = await slow_request

    assert response.status_code == 202
    assert settings.streamvox_device == "cpu"
    assert coordinator.hub.settings_revision == 4
    assert saved_snapshots[-1]["settings_revision"] == 4
    assert response.json()["settings"]["settings_revision"] == 4
    assert runtime.rpc_threads
    assert all(
        rpc_thread is not threading.main_thread()
        for rpc_thread in runtime.rpc_threads
    )


@pytest.mark.asyncio
async def test_config_response_omits_optional_settings_when_legacy_coordinator_returns_none() -> None:
    """旧组合根未返回设置快照时，StreamVox 响应不能携带非法 null 字段。"""

    runtime = BlockingStreamVoxRuntime(_runtime_state_payload())

    class LegacyCoordinator:
        async def commit_user_settings(self, **kwargs: object) -> None:
            prepare_update = kwargs["prepare_update"]
            assert callable(prepare_update)
            prepare_update(Settings())
            return None

    container = SimpleNamespace(
        assistant_process_supervisor=SimpleNamespace(
            require_for_streamvox_management=lambda: runtime,
        ),
        user_settings=LegacyCoordinator(),
    )
    runtime.release_rpc.set()
    response = await streamvox.update_streamvox_config(
        streamvox.StreamVoxConfigRequest(
            model="model-a",
            device="cpu",
            llama_backend=None,
            role_name="role-a",
            streamvox_seed=42,
            inference_options={},
        ),
        container,
    )

    assert "settings" not in response
