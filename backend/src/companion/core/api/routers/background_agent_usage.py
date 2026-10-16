"""提供后台工作长期 Token 用量查询接口。"""

from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, NonNegativeFloat, NonNegativeInt

from companion.background_agent.usage_service import (
    BackgroundAgentUsageUnavailableError,
)
from companion.core.api.dependencies import BackgroundAgentDependency


router = APIRouter(prefix="/api/background-agent", tags=["background-agent-usage"])


class UsageTokenMetrics(BaseModel):
    """定义所有统计维度共用的 Token 口径。"""

    model_config = ConfigDict(from_attributes=True)

    non_cached_input_tokens: NonNegativeInt
    cache_creation_input_tokens: NonNegativeInt
    cache_read_input_tokens: NonNegativeInt
    total_input_tokens: NonNegativeInt
    output_tokens: NonNegativeInt
    cache_hit_tokens: NonNegativeInt
    cache_hit_rate: float | None = Field(default=None, ge=0, le=1)


class UsageTaskMetrics(UsageTokenMetrics):
    """补充任务完整性和 SDK 参考成本覆盖情况。"""

    task_count: NonNegativeInt
    complete_task_count: NonNegativeInt
    incomplete_task_count: NonNegativeInt
    reference_cost_usd: NonNegativeFloat | None
    reference_cost_task_count: NonNegativeInt


class UsageByModel(UsageTokenMetrics):
    """描述一个模型的 Token 与任务覆盖情况。"""

    model: str = Field(min_length=1)
    task_count: NonNegativeInt
    complete_task_count: NonNegativeInt
    incomplete_task_count: NonNegativeInt


class UsageByDay(UsageTaskMetrics):
    """描述 Backend 本地日期内的后台工作用量。"""

    date: date


class BackgroundAgentUsageResponse(BaseModel):
    """定义用量页一次区间查询的完整响应。"""

    model_config = ConfigDict(from_attributes=True)

    from_date: date
    to_date: date
    summary: UsageTaskMetrics
    by_model: list[UsageByModel]
    by_day: list[UsageByDay]


@router.get("/usage", response_model=BackgroundAgentUsageResponse)
async def get_background_agent_usage(
    from_date: Annotated[date, Query()],
    to_date: Annotated[date, Query()],
    container: BackgroundAgentDependency,
) -> BackgroundAgentUsageResponse:
    """返回包含首尾日期的后台工作用量。

    Args:
        from_date: 查询包含的首日。
        to_date: 查询包含的末日。
        container: 后台工作 API 的窄化依赖切面。

    Returns:
        总览、按模型和按本地日期汇总的统计。

    Raises:
        HTTPException: 区间倒置时返回 400，仓储不可用时返回 503。
    """

    if to_date < from_date:
        raise HTTPException(status_code=400, detail="用量查询结束日期不能早于开始日期")
    try:
        report = await container.background_agent_usage_service.query(
            from_date=from_date,
            to_date=to_date,
        )
        return BackgroundAgentUsageResponse.model_validate(report)
    except BackgroundAgentUsageUnavailableError as exception:
        raise HTTPException(status_code=503, detail=str(exception)) from exception
