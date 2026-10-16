from __future__ import annotations

import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from companion.core.api.exception_handlers import install_application_error_handlers
from companion.core.api.routers import (
    activities,
    background_agent,
    background_agent_settings,
    background_agent_usage,
    bootstrap,
    character_cards,
    materials,
    playout_delivery,
    realtime,
    runtime_logs,
    runtime_profile,
    settings,
    skill_library,
    speaker_verification,
    streamvox,
    timed_reminders,
    wake_response,
)
from companion.core.application.container import AppBuildOptions, build_application_container
from companion.core.console_assets import mount_console_assets
from companion.core.dev_ports import load_dev_ports
from companion.core.application.runtime_initialization import install_runtime_initialization


def build_local_dev_origins() -> tuple[str, ...]:
    """从共享端口契约生成 Console 和 Desktop 的开发 CORS 来源。

    Returns:
        只包含两个开发前端端口及其 localhost 别名的来源元组。
    """

    ports = load_dev_ports()
    origins: list[str] = []
    for service_name in ("console", "desktop"):
        endpoint = ports[service_name]
        origins.append(f"http://{endpoint.host}:{endpoint.port}")
        if endpoint.host == "127.0.0.1":
            origins.append(f"http://localhost:{endpoint.port}")
    return tuple(origins)


LOCAL_DEV_ORIGINS = build_local_dev_origins()

API_ROUTERS = (
    realtime.router,
    activities.router,
    timed_reminders.router,
    background_agent.router,
    background_agent_settings.router,
    background_agent_usage.router,
    skill_library.router,
    materials.router,
    playout_delivery.router,
    settings.router,
    bootstrap.router,
    character_cards.router,
    speaker_verification.router,
    streamvox.router,
    wake_response.router,
    runtime_logs.router,
    runtime_profile.router,
)


def build_app(options: AppBuildOptions | None = None) -> FastAPI:
    """构建控制服务 FastAPI 应用。

    Args:
        options: 生产装配配置或测试替身；为空时使用默认设置。

    Returns:
        已注册生命周期、中间件、领域路由和控制台资源的应用实例。
    """

    container = build_application_container(options)
    app = FastAPI(lifespan=container.lifecycle)  # fastapi会调用lifecycle的call
    app.state.container = container

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        """返回控制面健康状态和本次 Backend 的启动身份。

        Returns:
            包含固定健康状态和可选启动身份的安全响应。
        """

        return {
            "status": "ok",
            "instance_id": os.environ.get("NEARMATE_RUNTIME_INSTANCE", ""),
        }

    # 授权门禁必须先于 CORS 注册：Starlette 的 add_middleware 是 insert(0)，
    # 后注册者位于最外层。让 CORS 留在最外层，门禁提前返回的 403 才会携带
    # CORS 头，否则 Console 开发态会把运行时阻断误报成跨域错误。
    install_runtime_initialization(app, container)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(LOCAL_DEV_ORIGINS),
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["content-type"],
    )
    install_application_error_handlers(app)
    for router in API_ROUTERS:
        app.include_router(router)
    mount_console_assets(app, dist_dir=container.settings.console_dist_dir)
    return app
