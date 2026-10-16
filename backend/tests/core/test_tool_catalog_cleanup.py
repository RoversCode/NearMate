from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from companion.core.api.dependencies import get_settings_dependencies
from companion.core.api.routers import settings
from companion.core.broadcast import BroadcastHub
from companion.core.local_clock import format_local_datetime_text
from companion.foreground_tools.activity_control.policy import (
    CancellableActivityContext,
)
from companion.foreground_tools.activity_correction.policy import (
    CorrectableActivityContext,
)
from companion.foreground_tools.catalog.service import ForegroundToolService
from companion.foreground_tools.catalog.waiting_policy import ToolWaitingPolicyStore
from companion.foreground_tools.external.weather_tool import WeatherForegroundTool


def test_foreground_catalog_keeps_background_work_entry_stable() -> None:
    """配置缺失不能让普通用户轮的稳定后台能力从目录消失。"""

    service = ForegroundToolService()

    schema_names = {
        schema["function"]["name"]
        for schema in service.tool_schemas()
    }

    assert "start_background_work" in schema_names
    assert "task_result" not in schema_names


def test_foreground_catalog_only_emits_object_parameter_schemas() -> None:
    """验证发送给 Foreground 模型的每个函数都使用对象参数根节点。

    Returns:
        无。
    """

    schemas = ForegroundToolService(
        weather_tools=WeatherForegroundTool(service=None),
    ).tool_schemas(
        has_pending_work_interaction=True,
        correctable_activities=(
            CorrectableActivityContext(
                activity_id="cognitive",
                source="cognitive",
                interaction_session_id="session-a",
                state="running",
                phase="reasoning",
                goal="核查资料",
                cognitive_tool_types=("lookup_current_fact",),
            ),
            CorrectableActivityContext(
                activity_id="background",
                source="background_agent",
                interaction_session_id="session-a",
                state="running",
                phase="reasoning",
                goal="整理资料",
            ),
        ),
        cancellable_activities=(
            CancellableActivityContext(
                activity_id="cancellable",
                source="background_agent",
                interaction_session_id="session-a",
                state="running",
                phase="reasoning",
                goal="处理资料",
            ),
        ),
        timed_reminders=(
            {
                "reminder_id": "scheduled",
                "can_modify": True,
                "can_cancel": True,
                "can_dismiss": False,
            },
            {
                "reminder_id": "ringing",
                "can_modify": False,
                "can_cancel": False,
                "can_dismiss": True,
            },
        )
    )

    invalid_tool_names = [
        schema["function"]["name"]
        for schema in schemas
        if schema["function"]["parameters"].get("type") != "object"
    ]

    assert invalid_tool_names == []


def test_foreground_catalog_has_no_datetime_tool() -> None:
    """日期时间工具必须从 schema、目录和执行入口共同移除。"""

    service = ForegroundToolService(
        weather_tools=WeatherForegroundTool(service=None),
    )

    schema_names = {
        schema["function"]["name"]
        for schema in service.tool_schemas()
    }
    catalog_names = {metadata.name for metadata in service.tool_catalog()}

    assert "get_datetime" not in schema_names
    assert "get_datetime" not in catalog_names
    assert service.execute("get_datetime", {}) == {
        "ok": False,
        "text": "不支持的 foreground 工具：get_datetime。",
    }


def test_local_clock_still_formats_prompt_time_without_tool() -> None:
    """移除时间工具后，本地时间格式化仍应独立服务于提示词。"""

    current_datetime = datetime(2026, 7, 16, 19, 5, tzinfo=timezone.utc)

    assert format_local_datetime_text(current_datetime) == (
        "2026年7月16日 星期四 晚上19时05分00秒 UTC+00:00"
    )


def test_waiting_policy_api_only_exposes_inline_families(tmp_path: Path) -> None:
    """等待设置 API 应隐藏后台工具族及其历史覆盖配置。"""

    store = ToolWaitingPolicyStore(tmp_path / "waiting.json")
    store.save_family_policy("task_result", enabled=True, phrases=["旧材料等待句"])
    store.save_family_policy("cognitive", enabled=True, phrases=["旧认知等待句"])
    store.save_tool_override("task_result", enabled=True, phrases=["旧工具等待句"])
    client = _build_waiting_policy_client(store)

    response = client.get("/api/tool-waiting-policies")

    assert response.status_code == 200
    payload = response.json()
    assert [family["family_id"] for family in payload["families"]] == ["weather"]
    exposed_tool_names = {
        tool["name"]
        for family in payload["families"]
        for tool in family["tools"]
    }
    assert "task_result" not in exposed_tool_names
    assert "lookup_current_fact" not in exposed_tool_names
    assert "get_datetime" not in exposed_tool_names
    assert payload["user_config"] == {"families": {}, "tools": {}}


def test_waiting_policy_api_rejects_background_family_and_tool(tmp_path: Path) -> None:
    """后台工具等待策略不能通过设置 API 重新写入。"""

    store = ToolWaitingPolicyStore(tmp_path / "waiting.json")
    client = _build_waiting_policy_client(store)

    family_response = client.put(
        "/api/tool-waiting-policies/families/task_result",
        json={"enabled": True, "phrases": ["等待"]},
    )
    tool_response = client.put(
        "/api/tool-waiting-policies/tools/task_result",
        json={
            "inherit_family": False,
            "enabled": True,
            "phrases": ["等待"],
        },
    )

    assert family_response.status_code == 404
    assert tool_response.status_code == 404
    assert store.load().to_payload() == {"families": {}, "tools": {}}


def _build_waiting_policy_client(store: ToolWaitingPolicyStore) -> TestClient:
    """构造只装配等待策略路由的测试客户端。

    Args:
        store: 测试使用的等待策略仓储。

    Returns:
        使用真实目录过滤逻辑的 FastAPI 测试客户端。
    """

    hub = object.__new__(BroadcastHub)
    hub.tool_waiting_policy_store = store
    container = SimpleNamespace(hub=hub)
    app = FastAPI()
    app.include_router(settings.router)
    app.dependency_overrides[get_settings_dependencies] = lambda: container
    return TestClient(app)
