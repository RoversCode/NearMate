"""提供 Desktop、Console 共用的定时提醒查询与管理接口。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Response, status

from companion.core.api.dependencies import TimedRemindersDependency
from companion.core.api.schemas.timed_reminders import (
    CreateTimedReminderRequest,
    DismissTimedRemindersRequest,
    UpdateTimedReminderRequest,
)
from companion.timed_reminders.repository import (
    DuplicateTimedReminder,
    InvalidTimedReminder,
    TimedReminderCapacityReached,
    TimedReminderNotFound,
    TimedReminderRuntimeConflict,
    TimedReminderStateConflict,
)


router = APIRouter(tags=["timed-reminders"])


@router.get("/api/timed-reminders")
async def list_timed_reminders(
    dependencies: TimedRemindersDependency,
) -> dict[str, object]:
    """读取当前 Backend 运行期的完整提醒快照。

    Args:
        dependencies: 只暴露定时提醒服务的路由依赖。

    Returns:
        当前活跃提醒与最近二十条终态记录。
    """

    return dependencies.timed_reminder_service.snapshot().to_payload()


@router.post("/api/timed-reminders/dismiss")
async def dismiss_timed_reminders(
    request: DismissTimedRemindersRequest,
    dependencies: TimedRemindersDependency,
) -> dict[str, object]:
    """原子关闭调用方当前看见的响铃批次。

    Args:
        request: 快照运行编号和气泡当前展示的提醒编号。
        dependencies: 只暴露定时提醒服务的路由依赖。

    Returns:
        当前运行编号和本次实际关闭的提醒编号。
    """

    try:
        reminders = await dependencies.timed_reminder_service.dismiss(
            runtime_id=request.runtime_id,
            reminder_ids=tuple(request.reminder_ids),
        )
    except (InvalidTimedReminder, TimedReminderRuntimeConflict) as exception:
        raise _http_error(exception) from exception
    return {
        "runtime_id": dependencies.timed_reminder_service.snapshot().runtime_id,
        "dismissed_ids": [reminder.reminder_id for reminder in reminders],
    }


@router.post("/api/timed-reminders", status_code=status.HTTP_201_CREATED)
async def create_timed_reminder(
    request: CreateTimedReminderRequest,
    dependencies: TimedRemindersDependency,
) -> dict[str, object]:
    """创建一条当前运行期内的一次性提醒。

    Args:
        request: 提醒事项和严格二选一的时间输入。
        dependencies: 只暴露定时提醒服务的路由依赖。

    Returns:
        canonical 提醒、跨日标记与运行期限制。
    """

    try:
        result = await dependencies.timed_reminder_service.create(
            content=request.content,
            trigger_at=request.trigger_at,
            delay_seconds=request.delay_seconds,
        )
    except (InvalidTimedReminder, DuplicateTimedReminder, TimedReminderCapacityReached) as exception:
        raise _http_error(exception) from exception
    return result.to_payload()


@router.patch("/api/timed-reminders/{reminder_id}")
async def update_timed_reminder(
    reminder_id: str,
    request: UpdateTimedReminderRequest,
    dependencies: TimedRemindersDependency,
) -> dict[str, object]:
    """修改一条仍在等待触发的提醒。

    Args:
        reminder_id: 当前运行期提醒编号。
        request: 需要修改的内容或时间字段。
        dependencies: 只暴露定时提醒服务的路由依赖。

    Returns:
        修改后的 canonical 提醒事实。
    """

    try:
        result = await dependencies.timed_reminder_service.update(
            reminder_id,
            content=request.content,
            trigger_at=request.trigger_at,
            delay_seconds=request.delay_seconds,
        )
    except (
        InvalidTimedReminder,
        DuplicateTimedReminder,
        TimedReminderNotFound,
        TimedReminderStateConflict,
    ) as exception:
        raise _http_error(exception) from exception
    return result.to_payload()


@router.delete(
    "/api/timed-reminders/{reminder_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def cancel_timed_reminder(
    reminder_id: str,
    dependencies: TimedRemindersDependency,
) -> Response:
    """取消一条尚未到点的记录。

    Args:
        reminder_id: 当前运行期提醒编号。
        dependencies: 只暴露定时提醒服务的路由依赖。

    Returns:
        无正文的 204 响应。
    """

    try:
        await dependencies.timed_reminder_service.cancel(reminder_id)
    except (TimedReminderNotFound, TimedReminderStateConflict) as exception:
        raise _http_error(exception) from exception
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _http_error(exception: Exception) -> HTTPException:
    """把提醒领域错误映射成稳定 HTTP 状态。

    Args:
        exception: 路由从提醒服务捕获的领域错误。

    Returns:
        可直接抛给 FastAPI 的公开错误。
    """

    if isinstance(exception, TimedReminderNotFound):
        return HTTPException(status_code=404, detail=str(exception))
    if isinstance(
        exception,
        (TimedReminderStateConflict, TimedReminderRuntimeConflict, DuplicateTimedReminder),
    ):
        return HTTPException(status_code=409, detail=str(exception))
    if isinstance(exception, TimedReminderCapacityReached):
        return HTTPException(status_code=409, detail=str(exception))
    return HTTPException(status_code=422, detail=str(exception))
