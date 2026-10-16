"""提供后台工作权限按钮的明确决定入口。"""

from __future__ import annotations

import asyncio
from typing import Literal

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, ConfigDict

from companion.background_agent.interaction_registry import (
    InteractionAnswerValidationError,
    InteractionRequestConflictError,
    InteractionRequestNotFoundError,
)
from companion.core.api.dependencies import BackgroundAgentDependency
from companion.observability.logging_config import get_backend_logger


router = APIRouter()
logger = get_backend_logger(__name__)


class PermissionDecisionBody(BaseModel):
    """描述用户通过鼠标作出的权限决定。

    Args:
        outcome: 明确允许或拒绝当前权限请求。
    """

    model_config = ConfigDict(extra="forbid")

    outcome: Literal["allow", "deny"]


@router.post(
    "/api/background-agent/interactions/{request_id}/permission-decision",
    status_code=202,
)
async def submit_permission_decision(
    request_id: str,
    body: PermissionDecisionBody,
    container: BackgroundAgentDependency,
) -> Response:
    """提交一次对当前 Activity 生效的权限决定。

    Args:
        request_id: HTTP 路径中的后台工作交互请求编号。
        body: 已通过 Pydantic 校验的明确允许或拒绝决定。
        container: 当前 Backend 组合根依赖。

    Returns:
        权限决定已进入后台控制队列的 202 空响应。
    """

    normalized_request_id = request_id.strip()
    if not normalized_request_id:
        raise HTTPException(status_code=422, detail="invalid interaction request id")
    try:
        submitted = (
            container.background_agent_interaction_service.submit_permission_decision(
                request_id=normalized_request_id,
                outcome=body.outcome,
            )
        )
    except InteractionRequestNotFoundError as exception:
        raise HTTPException(
            status_code=404,
            detail="interaction request not found",
        ) from exception
    except InteractionRequestConflictError as exception:
        raise HTTPException(status_code=409, detail=str(exception)) from exception
    except InteractionAnswerValidationError as exception:
        raise HTTPException(status_code=422, detail=str(exception)) from exception
    except RuntimeError as exception:
        raise HTTPException(
            status_code=503,
            detail="assistant runtime unavailable",
        ) from exception

    # 只有权限决定已经越过 Runtime 提交边界，才允许停止对应旧播报。此时卡片决定
    # 仍不等于 Gate 已生效；允许短句要继续等待后续 permission_granted 权威事实。
    try:
        container.livekit_media_session_controller.handle_permission_card_decision(
            submitted,
            body.outcome,
        )
    except Exception:
        # 原 Activity 已经收到决定；媒体早停只是本地体验优化，失败不能把已经成立的
        # 权限提交改写成 HTTP 失败，也不能诱导用户重复点击。
        logger.exception(
            "权限决定已提交但对应播报早停失败 request_id=%s",
            normalized_request_id,
        )

    # WebSocket 只负责让在线界面尽快收起按钮。最多等待半秒，超时或广播失败都不能
    # 回滚已提交决定，也不能改变 202 响应。
    try:
        await asyncio.wait_for(
            container.hub.publisher("activity").publish_ephemeral(
                {
                    "type": "background_agent.interaction_submitted",
                    "payload": {
                        "request_id": normalized_request_id,
                        "activity_id": submitted.activity_id,
                    },
                }
            ),
            timeout=0.5,
        )
    except TimeoutError:
        logger.warning(
            "权限决定已提交但 processing 状态广播超时 request_id=%s",
            normalized_request_id,
        )
    except Exception:
        logger.exception(
            "权限决定已提交但 processing 状态广播失败 request_id=%s",
            normalized_request_id,
        )
    return Response(status_code=202)
