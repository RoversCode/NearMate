"""提供本次运行 Activity 查询与取消接口。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Response

from companion.activities.runtime.identity import canonical_activity_uuid
from companion.activities.runtime.registry import ActivityNotFound, ActivityStateConflict
from companion.activities.runtime.service import ActivityRuntimeUnavailable
from companion.core.api.dependencies import ActivitiesDependency


router = APIRouter()


@router.get("/api/console/activities")
async def list_activities(container: ActivitiesDependency) -> dict[str, object]:
    """返回当前活动和本次运行最多两百条终态记录。"""

    return container.activity_service.console_snapshot().to_payload()


@router.get("/api/console/activities/{activity_id}")
async def get_activity(
    activity_id: str,
    container: ActivitiesDependency,
) -> dict[str, object]:
    """返回一项 Activity 的公开详情。"""

    try:
        canonical_id = _require_activity_uuid(activity_id)
        return container.activity_service.detail(canonical_id).to_payload()
    except ActivityNotFound as exception:
        raise HTTPException(status_code=404, detail="activity not found") from exception


@router.post("/api/activities/{activity_id}/cancel", status_code=202)
async def cancel_activity(
    activity_id: str,
    container: ActivitiesDependency,
) -> Response:
    """请求助手子进程真实取消一项活动。"""

    try:
        await container.activity_service.cancel(_require_activity_uuid(activity_id))
    except ActivityNotFound as exception:
        raise HTTPException(status_code=404, detail="activity not found") from exception
    except ActivityStateConflict as exception:
        raise HTTPException(status_code=409, detail="activity cannot be cancelled") from exception
    except ActivityRuntimeUnavailable as exception:
        raise HTTPException(status_code=503, detail="assistant runtime unavailable") from exception
    return Response(status_code=202)


def _require_activity_uuid(activity_id: str) -> str:
    """校验 HTTP 路径中的公共 Activity UUID。

    Args:
        activity_id: 路由解析得到的候选活动编号。

    Returns:
        符合 RFC 变体 v1-v8 的标准 UUID 文本。

    Raises:
        HTTPException: 编号的版本或变体不符合公共契约。
    """

    canonical_id = canonical_activity_uuid(activity_id)
    if canonical_id is None:
        raise HTTPException(status_code=422, detail="invalid activity id")
    return canonical_id
