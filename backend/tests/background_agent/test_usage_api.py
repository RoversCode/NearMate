from __future__ import annotations

from datetime import date
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from companion.background_agent.usage_service import (
    BackgroundAgentUsageUnavailableError,
)
from companion.core.api.dependencies import get_background_agent_dependencies
from companion.core.api.routers import background_agent_usage


class StubUsageService:
    """返回固定用量报告并记录查询边界。"""

    def __init__(self, *, unavailable: bool = False) -> None:
        """创建可选不可用状态的查询替身。"""

        self.unavailable = unavailable
        self.queried_range: tuple[date, date] | None = None

    async def query(
        self,
        *,
        from_date: date,
        to_date: date,
    ) -> dict[str, object]:
        """返回最小合法空报告，或模拟仓储不可用。"""

        if self.unavailable:
            raise BackgroundAgentUsageUnavailableError("后台工作用量仓储当前不可用")
        self.queried_range = (from_date, to_date)
        return {
            "from_date": from_date,
            "to_date": to_date,
            "summary": _empty_task_metrics(),
            "by_model": [],
            "by_day": [],
        }


def test_usage_api_uses_inclusive_date_range() -> None:
    """API 原样传递包含两端的固化日期并返回统一空态。"""

    service = StubUsageService()
    client = _client(service)

    response = client.get(
        "/api/background-agent/usage?from_date=2026-08-01&to_date=2026-08-02"
    )

    assert response.status_code == 200
    assert service.queried_range == (date(2026, 8, 1), date(2026, 8, 2))
    assert response.json()["from_date"] == "2026-08-01"
    assert response.json()["summary"]["cache_hit_rate"] is None


def test_usage_api_rejects_inverted_date_range() -> None:
    """结束日期早于开始日期时返回明确的客户端错误。"""

    response = _client(StubUsageService()).get(
        "/api/background-agent/usage?from_date=2026-08-03&to_date=2026-08-02"
    )

    assert response.status_code == 400


def test_usage_api_returns_503_when_store_is_unavailable() -> None:
    """查询仓储不可用必须区别于合法的空统计。"""

    response = _client(StubUsageService(unavailable=True)).get(
        "/api/background-agent/usage?from_date=2026-08-01&to_date=2026-08-02"
    )

    assert response.status_code == 503
    assert response.json()["detail"] == "后台工作用量仓储当前不可用"


def _client(service: StubUsageService) -> TestClient:
    """构造只挂载后台工作用量路由的测试客户端。"""

    app = FastAPI()
    app.include_router(background_agent_usage.router)
    app.dependency_overrides[get_background_agent_dependencies] = lambda: SimpleNamespace(
        background_agent_usage_service=service,
    )
    return TestClient(app)


def _empty_task_metrics() -> dict[str, object]:
    """构造接口响应模型允许的空任务统计。"""

    return {
        "task_count": 0,
        "complete_task_count": 0,
        "incomplete_task_count": 0,
        "non_cached_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "total_input_tokens": 0,
        "output_tokens": 0,
        "cache_hit_tokens": 0,
        "cache_hit_rate": None,
        "reference_cost_usd": None,
        "reference_cost_task_count": 0,
    }
