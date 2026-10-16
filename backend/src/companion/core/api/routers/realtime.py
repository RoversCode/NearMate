from __future__ import annotations

import io
import time
import uuid
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, File, Form, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from livekit.rtc import ConnectError
from PIL import Image, UnidentifiedImageError
from pydantic import ValidationError

from companion.core.api.dependencies import RealtimeDependency
from companion.core.api.schemas import (
    EnvironmentSnapshotRequest,
    TextTurnContextRequest,
    TextTurnEditRequest,
    TextTurnPreparationFailureRequest,
    TextTurnReservationRequest,
)
from companion.core.application.turn_admission import (
    TurnAdmissionError,
    TurnSubmissionConflict,
    TurnSubmissionNotFound,
    TurnSubmissionQueueFull,
)
from companion.core.control_socket import build_control_socket_initial_events
from companion.environment.formatting import describe_captured_at
from companion.environment.models import FrozenEnvironmentSnapshot
from companion.observability.logging_config import get_backend_logger
from companion.realtime.assistant_process_client import AssistantProcessNotReadyError


router = APIRouter()
logger = get_backend_logger(__name__)
MAX_ENVIRONMENT_SCREENSHOT_BYTES = 8 * 1024 * 1024
MAX_ENVIRONMENT_SCREENSHOT_EDGE = 1600
MAX_ENVIRONMENT_METADATA_CHARS = 16 * 1024


class _RuntimeControlAdmissionDenied(Exception):
    """等待配置锁期间运行时被阻断，不能登记连接或构造业务首帧。"""


def _validate_environment_screenshot(value: bytes) -> None:
    """验证环境截图格式、解码能力和最大尺寸。"""

    try:
        with Image.open(io.BytesIO(value)) as image:
            if image.format != "PNG":
                raise HTTPException(status_code=415, detail="environment screenshot must be PNG")
            if max(image.size) > MAX_ENVIRONMENT_SCREENSHOT_EDGE:
                raise HTTPException(status_code=422, detail="environment screenshot edge exceeds 1600 px")
            image.verify()
    except HTTPException:
        raise
    except (UnidentifiedImageError, OSError, ValueError) as exception:
        raise HTTPException(status_code=422, detail="environment screenshot cannot be decoded") from exception


@router.websocket("/ws/control")
async def control_socket(websocket: WebSocket, container: RealtimeDependency) -> None:
    """建立控制 WebSocket，并补发当前初始化、Bootstrap 和对应范围的业务快照。

    Args:
        websocket: 当前客户端发起的控制连接。
        container: 包含应用状态广播器的实时路由依赖切面。

    Returns:
        连接被拒绝或客户端断开时结束。
    """

    scope = getattr(websocket, "query_params", {}).get("scope", "runtime")
    if scope not in {"status", "runtime"}:
        await websocket.close(code=4400, reason="控制连接范围无效")
        return
    await websocket.accept()
    try:
        runtime_is_ready = not container.hub.bootstrap.runtime_blocked
        if scope == "runtime" and not runtime_is_ready:
            # 先发送初始化快照，客户端才能区分资源准备和普通断线。
            for event in container.application_status.current_events():
                await websocket.send_json(event)
            await websocket.close(code=4403, reason="NearMate 运行时尚未就绪")
            return
        def initial_events_factory():
            """状态连接只取安全快照；业务连接在配置锁内重验准入后再冻结首帧。"""
            if scope == "status":
                return container.application_status.current_events()
            # 等待配置锁时可能启动新初始化；首检不能代替锁内的原子检查。
            if scope == "runtime" and (
                container.hub.bootstrap.runtime_blocked
            ):
                raise _RuntimeControlAdmissionDenied
            return build_control_socket_initial_events(
                container.hub,
                interaction_state=container.realtime_input_runtime_owner.current_interaction_state(),
                input_runtime_snapshot=(
                    container.realtime_input_runtime_owner.current_runtime_identity_event
                ),
                interaction_state_snapshot=(
                    container.realtime_input_runtime_owner.current_interaction_state_event
                ),
                activity_snapshot=container.activity_service.desktop_snapshot(),
                timed_reminder_snapshot=container.timed_reminder_service.snapshot(),
                assistant_output_status_snapshot=(
                    container.assistant_output_health_ledger.snapshot_event
                ),
                pending_interactions_snapshot=(
                    container.background_agent_interactions.pending_control_snapshot_event
                ),
                result_card_events=(
                    container.task_result_delivery_coordinator.committed_result_card_events
                ),
                turn_submissions_snapshot=container.turn_admission.snapshot_event,
                application_status_events=container.application_status.current_events,
                scope=scope,
            )
        if scope == "runtime":
            await container.hub.subscribe_control(websocket, initial_events_factory)
        else:
            await container.hub.subscribe_control(
                websocket,
                initial_events_factory,
                scope=scope,
            )
    except _RuntimeControlAdmissionDenied:
        try:
            for event in container.application_status.current_events():
                await websocket.send_json(event)
            await websocket.close(code=4403, reason="NearMate 运行时尚未就绪")
        except WebSocketDisconnect:
            return
        return
    except WebSocketDisconnect:
        return

    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        return
    finally:
        container.hub.unsubscribe_control(websocket)


@router.post("/api/environment/current", status_code=202)
async def update_current_environment(
    container: RealtimeDependency,
    metadata: Annotated[str, Form()],
    screenshot: Annotated[UploadFile | None, File()] = None,
) -> dict[str, object]:
    """接纳 Desktop 直接上传的内存环境快照。"""

    if len(metadata) > MAX_ENVIRONMENT_METADATA_CHARS:
        raise HTTPException(status_code=413, detail="environment metadata exceeds 16 KiB")
    try:
        payload = EnvironmentSnapshotRequest.model_validate_json(metadata)
    except ValidationError as exception:
        raise HTTPException(status_code=422, detail="invalid environment metadata") from exception

    screenshot_bytes: bytes | None = None
    screenshot_mime_type: str | None = None
    if screenshot is not None:
        if screenshot.content_type != "image/png":
            raise HTTPException(status_code=415, detail="environment screenshot must be PNG")
        screenshot_bytes = await screenshot.read(MAX_ENVIRONMENT_SCREENSHOT_BYTES + 1)
        if len(screenshot_bytes) > MAX_ENVIRONMENT_SCREENSHOT_BYTES:
            raise HTTPException(status_code=413, detail="environment screenshot exceeds 8 MiB")
        if not screenshot_bytes:
            raise HTTPException(status_code=422, detail="environment screenshot is empty")
        _validate_environment_screenshot(screenshot_bytes)
        screenshot_mime_type = "image/png"

    snapshot = container.hub.environment.update(
        FrozenEnvironmentSnapshot(
            snapshot_id=f"environment_{uuid.uuid4().hex}",
            request_id=payload.request_id,
            turn_id=payload.turn_id,
            reason=payload.reason,
            captured_at=describe_captured_at(payload.captured_at),
            active_window_title=payload.active_window_title,
            screenshot_mime_type=screenshot_mime_type,
            screenshot_bytes=screenshot_bytes,
            capture_error=payload.capture_error,
        )
    )
    print(f"/api/environment/current给的snapshot_id {snapshot.snapshot_id}")
    return {"snapshot_id": snapshot.snapshot_id}


@router.post("/api/turn-submissions", status_code=202)
async def reserve_text_turn(
    payload: TextTurnReservationRequest,
    container: RealtimeDependency,
) -> dict[str, object]:
    """立即 reservation 一条文本，不等待现场环境捕获完成。"""

    _require_assistant_output(container)
    try:
        snapshot = await container.turn_admission.reserve_text(
            submission_id=str(payload.submission_id),
            message_text=payload.message_text,
        )
    except TurnAdmissionError as exception:
        raise _turn_admission_http_error(exception) from exception
    return {"accepted": True, "turn_submissions": snapshot}


@router.put("/api/turn-submissions/{submission_id}/context", status_code=202)
async def commit_text_turn_context(
    submission_id: UUID,
    payload: TextTurnContextRequest,
    container: RealtimeDependency,
) -> dict[str, object]:
    """提交文本 reservation 对应的环境上下文并尝试派发。"""

    _require_assistant_output(container)
    try:
        snapshot = await container.turn_admission.commit_context(
            submission_id=str(submission_id),
            environment_snapshot_id=payload.environment_snapshot_id,
        )
    except TurnAdmissionError as exception:
        raise _turn_admission_http_error(exception) from exception
    return {"accepted": True, "turn_submissions": snapshot}


@router.patch("/api/turn-submissions/{submission_id}")
async def edit_text_turn(
    submission_id: UUID,
    payload: TextTurnEditRequest,
    container: RealtimeDependency,
) -> dict[str, object]:
    """原位修改一条尚未派发的文本。"""

    try:
        snapshot = await container.turn_admission.edit_text(
            submission_id=str(submission_id),
            message_text=payload.message_text,
        )
    except TurnAdmissionError as exception:
        raise _turn_admission_http_error(exception) from exception
    return {"accepted": True, "turn_submissions": snapshot}


@router.delete("/api/turn-submissions/{submission_id}")
async def delete_text_turn(
    submission_id: UUID,
    container: RealtimeDependency,
) -> dict[str, object]:
    """删除一条尚未派发的文本并释放材料。"""

    try:
        snapshot = await container.turn_admission.delete_text(str(submission_id))
    except TurnAdmissionError as exception:
        raise _turn_admission_http_error(exception) from exception
    return {"accepted": True, "turn_submissions": snapshot}


@router.post("/api/turn-submissions/{submission_id}/resume", status_code=202)
async def resume_text_turn(
    submission_id: UUID,
    container: RealtimeDependency,
) -> dict[str, object]:
    """恢复一条因有效语音而暂停的文本。"""

    try:
        snapshot = await container.turn_admission.resume_text(str(submission_id))
    except TurnAdmissionError as exception:
        raise _turn_admission_http_error(exception) from exception
    return {"accepted": True, "turn_submissions": snapshot}


@router.post("/api/turn-submissions/{submission_id}/retry", status_code=202)
async def retry_text_turn(
    submission_id: UUID,
    container: RealtimeDependency,
) -> dict[str, object]:
    """把失败文本恢复为等待 Desktop 重新捕获环境的 preparing。"""

    try:
        snapshot = await container.turn_admission.retry_text(str(submission_id))
    except TurnAdmissionError as exception:
        raise _turn_admission_http_error(exception) from exception
    return {"accepted": True, "turn_submissions": snapshot}


@router.post("/api/turn-submissions/{submission_id}/preparation-failed")
async def fail_text_turn_preparation(
    submission_id: UUID,
    _payload: TextTurnPreparationFailureRequest,
    container: RealtimeDependency,
) -> dict[str, object]:
    """记录 Desktop 环境捕获失败，允许用户稍后重试。"""

    try:
        snapshot = await container.turn_admission.fail_preparation(
            submission_id=str(submission_id),
        )
    except TurnAdmissionError as exception:
        raise _turn_admission_http_error(exception) from exception
    return {"accepted": True, "turn_submissions": snapshot}


def _require_assistant_output(container: RealtimeDependency) -> None:
    """校验文本 reservation 当前具备助手输出资格。

    Args:
        container: 当前应用依赖容器。

    Raises:
        HTTPException: 输出关闭、Bootstrap 尚未完成或当前 Bridge 不可用。
    """

    if not container.assistant_process_supervisor.output_enabled:
        raise HTTPException(
            status_code=409,
            detail="assistant output is disabled in realtime input-only mode",
        )
    if container.assistant_process_supervisor.owns_process:
        bootstrap_status = container.hub.bootstrap.status_payload()
        if not bootstrap_status["is_initialized"]:
            raise HTTPException(status_code=409, detail="bootstrap is not initialized")

    output_available = container.livekit_media_session_controller.assistant_output_available()
    assistant_runtime_ready = getattr(
        container.assistant_process_supervisor,
        "is_ready",
        True,
    )
    if not assistant_runtime_ready or not output_available:
        current_health = container.assistant_output_health_ledger.snapshot()
        unavailable_code = (
            "output_unavailable"
            if not assistant_runtime_ready
            else (
                "request_identity_conflict"
                if current_health.code == "request_identity_conflict"
                else "output_unavailable"
            )
        )
        container.assistant_output_health_ledger.mark_unavailable(
            unavailable_code
        )
        raise HTTPException(
            status_code=409,
            detail="assistant output is currently unavailable",
        )
    container.assistant_output_health_ledger.mark_available()


def _turn_admission_http_error(exception: TurnAdmissionError) -> HTTPException:
    """把 Turn 准入领域错误映射为稳定 HTTP 语义。

    Args:
        exception: Sequencer 返回的明确状态错误。

    Returns:
        404、409 或 429 HTTP 异常。
    """

    if isinstance(exception, TurnSubmissionNotFound):
        return HTTPException(status_code=404, detail=str(exception))
    if isinstance(exception, TurnSubmissionQueueFull):
        return HTTPException(status_code=429, detail=str(exception))
    if isinstance(exception, TurnSubmissionConflict):
        return HTTPException(status_code=409, detail=str(exception))
    return HTTPException(status_code=409, detail=str(exception))


@router.get("/api/livekit/session", response_model=None)
async def create_livekit_session(container: RealtimeDependency) -> dict[str, object] | JSONResponse:
    """连接媒体服务并签发桌面会话，失败时说明是否可以重试。

    Args:
        container: 当前实时交互服务依赖。

    Returns:
        成功时返回入房凭据；依赖不可用时返回带原因码的 503 响应。
    """

    if not container.hub.bootstrap.status_payload()["is_initialized"]:
        raise HTTPException(status_code=409, detail="bootstrap is not initialized")
    if container.livekit_session_service is None:
        return JSONResponse(status_code=503, content={
            "detail": "语音通信服务未配置，请重新安装完整的 NearMate 安装包；使用外部服务时请检查通信配置。",
            "code": "livekit_not_configured",
            "retryable": False,
        })
    try:
        session = await container.livekit_session_service.create_desktop_session()
    except AssistantProcessNotReadyError as exception:
        logger.warning("媒体会话等待助手就绪：%s", type(exception).__name__)
        return JSONResponse(status_code=503, content={
            "detail": "助手正在准备，请稍候，NearMate 会自动重试连接。",
            "code": "assistant_not_ready",
            "retryable": True,
        })
    except (ConnectError, OSError) as exception:
        # SDK 异常可能夹带连接地址和凭据，界面和日志只保留固定说明及异常类别。
        logger.warning("媒体服务连接失败：%s", type(exception).__name__)
        return JSONResponse(status_code=503, content={
            "detail": "语音通信服务暂时无法连接，NearMate 会自动重试；若持续失败，请重启软件并查看诊断日志。",
            "code": "livekit_connection_failed",
            "retryable": True,
        })
    except RuntimeError as exception:
        if "RealtimeInputRuntime 尚未初始化" in str(exception):
            # 配置门禁已通过但运行时正在交接时仍可恢复，不能误报成未授权并停止重试。
            return JSONResponse(status_code=503, content={
                "detail": "语音运行时正在准备，请稍候，NearMate 会自动重试连接。",
                "code": "assistant_not_ready",
                "retryable": True,
            })
        raise
    return session.to_payload()


@router.post("/api/followup/toggle", status_code=202)
async def toggle_followup_state(container: RealtimeDependency) -> dict[str, object]:
    """切换跟进模式，并在回执前停止当前角色表达。

    Args:
        container: 当前输入状态、媒体取消入口与公共事件发布器。

    Returns:
        新跟进状态、同一版本的交互快照、已取消请求和用于判断新唤醒的单调时间。
    """

    issued_at = time.monotonic()
    realtime_input_runtime = (
        container.realtime_input_runtime_owner.require_for_turn_state()
    )
    accepted, followup_state, events, rejected_reason = (
        realtime_input_runtime.apply_followup_toggle(now=issued_at)
    )
    if not accepted:
        raise HTTPException(
            status_code=409,
            detail=rejected_reason or "followup toggle rejected",
        )
    try:
        # 本地静音只覆盖网络往返窗口；Backend 必须收口生成和待播语音，
        # 否则旧 TTS 会在下一轮放开共享音轨时再次漏出来。
        interrupted_request_ids = container.livekit_media_session_controller.interrupt_current_output(
            issued_at=issued_at,
        )
    except Exception as exception:
        logger.exception("跟进快捷键停止角色语音失败")
        raise HTTPException(status_code=503, detail="停止角色语音失败，请重试。") from exception
    finally:
        # 跟进状态已经改变，即使音频取消失败也要发布真实状态。
        for event in events:
            await container.hub.publisher("interaction").publish(event)
    return {
        "accepted": True,
        "followup_state": followup_state,
        "interaction_state": next(
            event["payload"] for event in events if event["type"] == "interaction.state_changed"
        ),
        "interrupted_request_ids": list(interrupted_request_ids),
        "speech_interrupted_at": issued_at,
    }
