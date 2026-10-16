"""提供本次进程内的结构化运行日志。"""

from __future__ import annotations

from fastapi import APIRouter, Query

from companion.core.api.dependencies import RuntimeLogsDependency


router = APIRouter()


@router.get("/api/console/logs")
async def list_logs(
    container: RuntimeLogsDependency,
    limit: int = Query(100, ge=1, le=500),
) -> list[dict[str, object]]:
    """按最新优先返回本次运行的结构化事件日志。"""

    return [row.to_payload() for row in container.hub.runtime_logs.list_rows(limit)]
