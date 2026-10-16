"""为 Desktop 合同测试提供真实设置接口和隔离的持久化目录。"""

from __future__ import annotations

import argparse
import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
from types import SimpleNamespace


def build_contract_app(state_root: Path):
    """装配真实设置路由、事务和仓储，所有写入都留在测试目录。

    Args:
        state_root: 本次测试独占的数据目录。

    Returns:
        可交给 TestClient 或 Uvicorn 的 FastAPI 应用。
    """

    repository_root = Path(__file__).resolve().parents[3]
    state_root = state_root.resolve()
    state_root.mkdir(parents=True, exist_ok=True)
    # 必须在导入 Settings 前设置路径，避免模块级默认值引用真实用户数据。
    os.environ["NEARMATE_DATA_ROOT"] = str(state_root)
    os.environ["NEARMATE_ENV_PATH"] = str(state_root / "absent-env.yaml")
    os.environ["LOCALAPPDATA"] = str(state_root / "local-appdata")
    os.environ.pop("NEARMATE_PACKAGED", None)
    sys.path.insert(0, str(repository_root / "backend" / "src"))

    from fastapi import FastAPI, Request
    from companion.background_agent.config_commit_lock import BackgroundAgentConfigCommitLock
    from companion.core.api.exception_handlers import install_application_error_handlers
    from companion.core.api.routers.settings import router
    from companion.core.application.general_settings import (
        GENERAL_SETTINGS_FIELD_NAMES,
        GeneralSettingsUpdate,
    )
    from companion.core.application.user_settings_reconfiguration import UserSettingsCoordinator
    from companion.core.broadcast import BroadcastHub
    from companion.core.settings import Settings
    from companion.realtime.shell_projector import ShellProjector

    product_settings = Settings(
        bootstrap_db_path=str(state_root / "companion.sqlite3"),
        user_settings_path=str(state_root / "user-settings.json"),
        character_cards_dir=str(state_root / "character-cards"),
        tool_waiting_policies_path=str(state_root / "tool-waiting-policies.json"),
        background_agent_runtime_dir=str(state_root / "agent-runtime"),
        background_agent_usage_db_path=str(state_root / "usage.sqlite3"),
        background_agent_workspace_root=str(state_root / "agent-workspace"),
        streamvox_model_dir=str(state_root / "streamvox" / "ckpts"),
        voice_setup_path=str(state_root / "voice-setup.json"),
        execution_config_path=str(state_root / "execution-config.json"),
    )
    hub = BroadcastHub(product_settings)

    coordinator = UserSettingsCoordinator(
        settings=product_settings,
        hub=hub,
        background_agent_config_commit_lock=BackgroundAgentConfigCommitLock(
            product_settings.user_settings_path
        ),
        configuration_mutation_lock=hub.configuration_mutation_lock,
    )
    app = FastAPI()
    app.state.container = SimpleNamespace(
        hub=hub, user_settings=coordinator, speaker_verification=None,
        runtime_reconfiguration=SimpleNamespace(runtime_initialization=None),
    )
    app.state.fixture_settings = product_settings
    app.state.fixture_posts = []
    app.state.fixture_conflict_armed = False
    install_application_error_handlers(app)
    app.include_router(router)

    sentinel_values = {
        "show_speech_bubbles": True,
        "followup_toggle_shortcut_enabled": False,
        "followup_toggle_shortcut": "CommandOrControl+Alt+F",
        "avatar_visibility_shortcut_enabled": False,
        "avatar_visibility_shortcut": "CommandOrControl+Shift+Q",
        "selection_capture_shortcut_enabled": False,
        "selection_capture_shortcut": "CommandOrControl+Shift+C",
        "selected_live2d_model_id": "Haru",
        "user_self_intro": "合同测试：请保留我的自我介绍。",
        "user_extra_notes": "合同测试：请保留补充说明。",
        "streamvox_device": "cpu",
        "audio_device_mode": "manual",
        "audio_input_device_id": "fixture-microphone",
        "audio_input_device_name": "合同测试麦克风",
    }

    def reset_fixture() -> None:
        """恢复非默认用户值，便于发现窄请求偷偷覆盖其他设置。

        Returns:
            无返回值；测试快照落盘，版本固定从 7 开始。
        """

        for field_name, value in sentinel_values.items():
            setattr(product_settings, field_name, deepcopy(value))
        hub.settings_revision = 7
        hub.projector = ShellProjector(show_speech_bubbles=True)
        hub.user_settings_store.save(hub.current_settings_payload())
        app.state.fixture_posts.clear()
        app.state.fixture_conflict_armed = False

    reset_fixture()

    @app.middleware("http")
    async def record_and_inject_conflict(request: Request, call_next):
        """记录真实 POST，并在指定一次请求前用真实事务制造并发提交。

        Args:
            request: 当前 HTTP 请求。
            call_next: 后续真实中间件和路由处理入口。

        Returns:
            原路由响应；不会把 schema 校验失败改成成功。
        """

        if request.method != "POST" or request.url.path != "/api/settings":
            return await call_next(request)
        body_bytes = await request.body()
        try:
            posted_body = json.loads(body_bytes)
        except (ValueError, UnicodeError):
            posted_body = {"invalid_json": True}
        if app.state.fixture_conflict_armed:
            app.state.fixture_conflict_armed = False
            snapshot = await coordinator.current_settings_payload()
            await coordinator.update_general_settings(
                update=GeneralSettingsUpdate(
                    **{key: snapshot[key] for key in GENERAL_SETTINGS_FIELD_NAMES}
                ),
                expected_revision=snapshot["settings_revision"],
            )
        response = await call_next(request)
        app.state.fixture_posts.append({
            "body": posted_body, "status_code": response.status_code,
            "settings_revision": hub.settings_revision,
        })
        return response

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        """说明隔离合同服务器已经接受请求。

        Returns:
            固定健康状态。
        """

        return {"status": "ok"}

    @app.post("/test/reset")
    async def reset() -> dict[str, object]:
        """把当前测试恢复成确定的非默认设置。

        Returns:
            重置后真实 Hub 生成的完整快照。
        """

        async with hub.configuration_mutation_lock:
            reset_fixture()
            return hub.current_settings_payload()

    @app.post("/test/conflict")
    async def conflict() -> dict[str, bool]:
        """让下一次设置 POST 遭遇一次真实 revision 冲突。

        Returns:
            表示下一次提交已经安排了并发更新。
        """

        app.state.fixture_conflict_armed = True
        return {"armed": True}

    @app.get("/test/evidence")
    async def evidence() -> dict[str, object]:
        """同时返回内存、磁盘和控制事件，验证持久化确实发生。

        Returns:
            隔离目录中的设置证据与实际 HTTP 提交记录。
        """

        async with hub.configuration_mutation_lock:
            return {
                "state_root": str(state_root),
                "snapshot": hub.current_settings_payload(),
                "persisted": json.loads(Path(product_settings.user_settings_path).read_text(encoding="utf-8")),
                "posts": deepcopy(app.state.fixture_posts),
                "events": [row.to_payload() for row in hub.runtime_logs.list_rows()],
            }

    return app


def serve(state_root: Path, port: int) -> None:
    """在本机随机端口启动测试服务，并通过标准输出公布地址。

    Args:
        state_root: 本轮独占的测试数据目录。
        port: 指定端口；0 表示让系统选择空闲端口。

    Returns:
        无返回值；stdin 收到 EOF 后关闭服务。
    """

    import uvicorn

    app = build_contract_app(state_root)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", port))
    listener.listen(128)
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))

    async def run_server() -> None:
        """等 Uvicorn 真正就绪后输出一条可解析的 ready JSON。

        Returns:
            服务关闭后返回，无其他业务状态修改。
        """

        task = asyncio.create_task(server.serve(sockets=[listener]))
        while not server.started:
            if task.done():
                await task
                return
            await asyncio.sleep(0.01)
        print(json.dumps({"base_url": f"http://127.0.0.1:{listener.getsockname()[1]}"}), flush=True)
        await task

    def close_on_stdin_eof() -> None:
        """父测试关闭 stdin 后结束当前服务器，避免残留监听进程。

        Returns:
            无返回值。
        """

        for _line in sys.stdin:
            pass
        server.should_exit = True

    threading.Thread(target=close_on_stdin_eof, daemon=True).start()
    try:
        asyncio.run(run_server())
    finally:
        listener.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-root", type=Path)
    parser.add_argument("--port", type=int, default=0)
    arguments = parser.parse_args()
    if arguments.state_root is not None:
        serve(arguments.state_root, arguments.port)
    else:
        with tempfile.TemporaryDirectory(prefix="nearmate-settings-contract-") as temporary_root:
            serve(Path(temporary_root), arguments.port)
