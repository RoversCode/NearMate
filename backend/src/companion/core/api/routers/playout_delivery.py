"""接收 Desktop 对助手语音交付的 typed 回执。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Response

from companion.core.api.dependencies import ContainerDependency
from companion.core.api.schemas.playout import PlayoutDeliveryAckRequest
from companion.realtime.playout_delivery import (
    PlayoutDeliveryConflictError,
    PlayoutDeliveryNotFoundError,
)


router = APIRouter()


@router.post("/api/assistant/playout-delivery/ack", status_code=202)
async def acknowledge_assistant_playout_delivery(
    body: PlayoutDeliveryAckRequest,
    container: ContainerDependency,
) -> Response:
    """应用 Desktop 真实 attach/play 结果，不触发重播或 Notice 变化。

    Args:
        body: 已经通过严格字段校验的 Desktop 回执。
        container: 当前 Backend 组合根依赖。

    Returns:
        表示回执已经接收的 202 空响应。
    """

    try:
        update = container.playout_delivery_registry.acknowledge(
            delivery_id=body.delivery_id,
            request_id=body.request_id,
            generation_id=body.generation_id,
            client_instance_id=body.client_instance_id,
            outcome=body.outcome,
            occurred_at=0.0,
            failure_code=body.failure_code,
            failure_message=body.failure_message,
        )
    except PlayoutDeliveryNotFoundError as exception:
        raise HTTPException(status_code=404, detail="playout delivery not found") from exception
    except PlayoutDeliveryConflictError as exception:
        raise HTTPException(status_code=409, detail=str(exception)) from exception

    if update.public_event is not None:
        await container.hub.publisher("assistant_output").publish(update.public_event)
    return Response(status_code=202)
