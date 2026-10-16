from __future__ import annotations

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from companion.activities.runtime.models import ActivityApplyResult, ActivityProjection
from companion.background_agent.usage_repository import BackgroundAgentUsageRepository
from companion.background_agent.usage_service import BackgroundAgentUsageService
from companion.core.api.dependencies import get_background_agent_dependencies
from companion.core.api.routers import background_agent_usage
from companion.realtime.assistant_events import RuntimeEvent
from backend.tests.runtime_public_event_support import runtime_event_router


@pytest.mark.asyncio
async def test_runtime_terminal_usage_reaches_api_without_public_leak(
    tmp_path: Path,
) -> None:
    """真实覆盖终态剥离、异步落库和日期 API 的完整链路。"""

    service = BackgroundAgentUsageService(
        BackgroundAgentUsageRepository(tmp_path / "usage.sqlite3")
    )
    await service.startup()
    public_activity_payloads: list[dict[str, object]] = []
    router = runtime_event_router(
        publish=_ignore_broadcast,
        publish_ephemeral=_ignore_broadcast,
        realtime_input_runtime=SimpleNamespace(),
        background_agent_usage_handler=service.record_terminal_event,
        activity_apply_handler=lambda _event_type, payload, _runtime_epoch: (
            public_activity_payloads.append(payload)
            or ActivityApplyResult("changed", ActivityProjection(revision=1))
        ),
        activity_broadcast_handler=_ignore_projection,
    )
    terminal_at_ms = int(datetime(2026, 8, 2, 12).timestamp() * 1000)

    await router.dispatch(_terminal_event(terminal_at_ms))

    app = FastAPI()
    app.include_router(background_agent_usage.router)
    app.dependency_overrides[get_background_agent_dependencies] = lambda: SimpleNamespace(
        background_agent_usage_service=service,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get(
            "/api/background-agent/usage",
            params={"from_date": "2026-08-02", "to_date": "2026-08-02"},
        )
    await service.shutdown()

    assert response.status_code == 200
    assert response.json()["summary"]["total_input_tokens"] == 35
    assert len(public_activity_payloads) == 1
    assert "internal_usage" not in public_activity_payloads[0]
    assert "internal_usage" not in response.text


async def _ignore_broadcast(_event: dict[str, object]) -> None:
    """吸收与集成测试无关的广播。"""


async def _ignore_projection(_projection: ActivityProjection) -> None:
    """吸收与集成测试无关的 Activity 广播。"""


def _terminal_event(terminal_at_ms: int) -> RuntimeEvent:
    """构造带有私有用量的后台工作成功终态。"""

    return RuntimeEvent(
        type="background_agent.completed",
        source="assistant_runtime",
        runtime_epoch=1,
        payload={
            "activity_id": "11111111-1111-4111-8111-111111111111",
            "interaction_session_id": "session-a",
            "goal": "公开目标",
            "created_at": terminal_at_ms,
            "result": "公开结果",
            "internal_usage": {
                "is_complete": True,
                "reference_cost_usd": 0.25,
                "by_model": [
                    {
                        "model": "deepseek-v4-pro",
                        "non_cached_input_tokens": 10,
                        "cache_creation_input_tokens": 5,
                        "cache_read_input_tokens": 20,
                        "output_tokens": 7,
                    }
                ],
            },
        },
    )
