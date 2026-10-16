from __future__ import annotations

import uuid

from fastapi import APIRouter, Form, HTTPException, Response, WebSocket, WebSocketDisconnect

from companion.audio.pcm16_utils import (
    REQUIRED_SPEAKER_VERIFICATION_SPEECH_CHUNK_COUNT,
    REQUIRED_SPEAKER_VERIFICATION_SPEECH_SECONDS,
    SPEAKER_VERIFICATION_CHUNK_BYTES,
    combine_latest_speaker_verification_chunks,
    validate_pcm16_bytes,
)
from companion.core.api.dependencies import SpeakerVerificationDependency
from companion.core.api.schemas import SpeakerVerificationProfileRequest
from companion.core.application.errors import StateConflict
from companion.core.settings import Settings
from companion.core.speaker_verification_assets import (
    SpeakerVerificationAssetSnapshot,
)
from companion.observability.logging_config import get_backend_logger


logger = get_backend_logger(__name__)
router = APIRouter()


@router.websocket("/api/speaker-verification/recording-stream")
async def speaker_verification_recording_stream(
    websocket: WebSocket,
    container: SpeakerVerificationDependency,
) -> None:
    """为控制台录音过程提供后端 VAD 有效人声累计。"""

    await websocket.accept()
    vad_provider = getattr(container.realtime_input_runtime_owner.runtime, "vad_provider", None)
    if vad_provider is None:
        await websocket.send_json({"type": "error", "message": "实时语音 VAD 尚未初始化"})
        await websocket.close()
        return

    recording_id = uuid.uuid4().hex
    speech_chunks: list[bytes] = []
    pending_pcm16 = b""
    required_chunks = REQUIRED_SPEAKER_VERIFICATION_SPEECH_CHUNK_COUNT
    required_seconds = REQUIRED_SPEAKER_VERIFICATION_SPEECH_SECONDS
    try:
        while len(speech_chunks) < required_chunks:
            pcm16 = await websocket.receive_bytes()
            try:
                validate_pcm16_bytes(pcm16)
            except ValueError as exception:
                await websocket.send_json({"type": "error", "message": str(exception)})
                continue

            pending_pcm16 += pcm16
            while len(pending_pcm16) >= SPEAKER_VERIFICATION_CHUNK_BYTES and len(speech_chunks) < required_chunks:
                vad_chunk = pending_pcm16[:SPEAKER_VERIFICATION_CHUNK_BYTES]
                pending_pcm16 = pending_pcm16[SPEAKER_VERIFICATION_CHUNK_BYTES:]
                # 只累计后端 VAD 认可的人声，静音和环境噪声不推进录音门槛。
                if vad_provider.is_speech_chunk(vad_chunk):
                    speech_chunks.append(vad_chunk)
            speech_seconds = round(
                len(speech_chunks) * (SPEAKER_VERIFICATION_CHUNK_BYTES / 32000.0),
                3,
            )
            payload: dict[str, object] = {
                "type": "completed" if len(speech_chunks) >= required_chunks else "speech_progress",
                "speech_seconds": min(speech_seconds, required_seconds),
                "required_speech_seconds": required_seconds,
            }
            if payload["type"] == "completed":
                container.hub.speaker_recording_sessions[recording_id] = (
                    combine_latest_speaker_verification_chunks(speech_chunks)
                )
                payload["recording_id"] = recording_id
            await websocket.send_json(payload)
    except WebSocketDisconnect:
        return


@router.get("/api/speaker-verification/profile")
async def get_speaker_verification_profile(
    container: SpeakerVerificationDependency,
) -> dict[str, object]:
    """读取说话人确认白名单配置状态。"""

    return container.speaker_verification.current_profile()


@router.post("/api/speaker-verification/profile", status_code=202)
async def update_speaker_verification_profile(
    payload: SpeakerVerificationProfileRequest,
    container: SpeakerVerificationDependency,
) -> dict[str, object]:
    """保存说话人确认开关。"""

    provider_rebuild_attempted = False
    committed_settings: dict[str, object] | None = None

    def prepare_profile_update(current_settings: Settings) -> dict[str, object]:
        """在提交锁内校验样本并准备 provider。

        Args:
            current_settings: 当前已提交的最新设置。

        Returns:
            说话人确认领域拥有的设置字段 patch。

        Raises:
            ValueError: 用户在没有样本时要求开启确认。
        """

        nonlocal provider_rebuild_attempted
        if (
            payload.enabled
            and not container.speaker_verification.current_whitelist_audio_paths()
        ):
            raise ValueError("至少录制 1 段声音后才能开启说话人确认")
        provider_rebuild_attempted = True
        return container.speaker_verification.prepare_settings_patch(
            current_settings,
            enabled=payload.enabled,
        )

    def rollback_profile_update(previous_settings: Settings) -> None:
        """恢复提交前的说话人确认 provider。

        Args:
            previous_settings: 本事务开始前的完整设置。

        Returns:
            无返回值。
        """

        if provider_rebuild_attempted:
            container.speaker_verification.rebuild_provider(previous_settings)

    try:
        committed_settings = await container.user_settings.commit_user_settings(
            prepare_update=prepare_profile_update,
            rollback_external=rollback_profile_update,
        )
    except ValueError as exception:
        raise HTTPException(status_code=400, detail=str(exception)) from exception
    except StateConflict:
        raise
    except Exception as exception:
        logger.exception("说话人确认 provider 热替换失败")
        raise HTTPException(status_code=500, detail=str(exception)) from exception
    response_payload = {
        **container.speaker_verification.current_profile(),
    }
    if committed_settings is not None:
        # 说话人确认开关属于统一用户设置；返回同一提交点快照，避免
        # Console 在后续刷新失败时继续携带旧 settings_revision。
        response_payload["settings"] = committed_settings
    return response_payload


@router.post("/api/speaker-verification/samples", status_code=202)
async def create_speaker_verification_sample(
    container: SpeakerVerificationDependency,
    recording_id: str = Form(...),
    replace_sample_id: str | None = Form(default=None),
) -> dict[str, object]:
    """保存控制台录制的说话人白名单音频。"""

    asset_snapshot: SpeakerVerificationAssetSnapshot | None = None
    recording_content: bytes | None = None
    provider_rebuild_attempted = False
    committed_settings: dict[str, object] | None = None

    def prepare_sample_create(current_settings: Settings) -> dict[str, object]:
        """保存录音资产并准备匹配最新样本的 provider。

        Args:
            current_settings: 当前已提交的最新设置。

        Returns:
            说话人确认领域拥有的设置字段 patch。

        Raises:
            HTTPException: 录音会话不存在时抛出。
        """

        nonlocal asset_snapshot, recording_content, provider_rebuild_attempted
        asset_snapshot = container.hub.speaker_verification_assets.create_snapshot()
        recording_content = container.hub.speaker_recording_sessions.pop(
            recording_id,
            None,
        )
        if recording_content is None:
            raise HTTPException(status_code=404, detail="说话人录音会话不存在")
        container.hub.speaker_verification_assets.save_sample(
            content=recording_content,
            replace_sample_id=replace_sample_id,
        )
        provider_rebuild_attempted = True
        return container.speaker_verification.prepare_settings_patch(
            current_settings,
            enabled=current_settings.speaker_verification_enabled,
        )

    def rollback_sample_create(previous_settings: Settings) -> None:
        """恢复录音资产、录音会话和旧 provider。

        Args:
            previous_settings: 本事务开始前的完整设置。

        Returns:
            无返回值。
        """

        if asset_snapshot is not None:
            container.hub.speaker_verification_assets.restore_snapshot(
                asset_snapshot
            )
        if recording_content is not None:
            container.hub.speaker_recording_sessions.setdefault(
                recording_id,
                recording_content,
            )
        if provider_rebuild_attempted:
            container.speaker_verification.rebuild_provider(previous_settings)

    try:
        committed_settings = await container.user_settings.commit_user_settings(
            prepare_update=prepare_sample_create,
            rollback_external=rollback_sample_create,
        )
        response_payload = {
            **container.speaker_verification.current_profile(),
        }
        if committed_settings is not None:
            response_payload["settings"] = committed_settings
        return response_payload
    except ValueError as exception:
        raise HTTPException(status_code=400, detail=str(exception)) from exception
    except HTTPException:
        raise
    except StateConflict:
        raise
    except Exception as exception:
        logger.exception("说话人白名单录音保存失败")
        raise HTTPException(status_code=500, detail=str(exception)) from exception


@router.delete("/api/speaker-verification/samples/{sample_id}", status_code=202)
async def delete_speaker_verification_sample(
    sample_id: str,
    container: SpeakerVerificationDependency,
) -> dict[str, object]:
    """删除指定说话人白名单录音。"""

    asset_snapshot: SpeakerVerificationAssetSnapshot | None = None
    provider_rebuild_attempted = False
    committed_settings: dict[str, object] | None = None

    def prepare_sample_delete(current_settings: Settings) -> dict[str, object]:
        """删除目标样本并准备剩余白名单对应的 provider。

        Args:
            current_settings: 当前已提交的最新设置。

        Returns:
            说话人确认领域拥有的设置字段 patch。

        Raises:
            HTTPException: 样本不存在时抛出。
        """

        nonlocal asset_snapshot, provider_rebuild_attempted
        asset_snapshot = container.hub.speaker_verification_assets.create_snapshot()
        if not container.hub.speaker_verification_assets.delete_sample(sample_id):
            raise HTTPException(status_code=404, detail="说话人白名单录音不存在")
        provider_rebuild_attempted = True
        return container.speaker_verification.prepare_settings_patch(
            current_settings,
            enabled=current_settings.speaker_verification_enabled,
        )

    def rollback_sample_delete(previous_settings: Settings) -> None:
        """恢复删除前的样本资产和 provider。

        Args:
            previous_settings: 本事务开始前的完整设置。

        Returns:
            无返回值。
        """

        if asset_snapshot is not None:
            container.hub.speaker_verification_assets.restore_snapshot(
                asset_snapshot
            )
        if provider_rebuild_attempted:
            container.speaker_verification.rebuild_provider(previous_settings)

    try:
        committed_settings = await container.user_settings.commit_user_settings(
            prepare_update=prepare_sample_delete,
            rollback_external=rollback_sample_delete,
        )
    except HTTPException:
        raise
    except StateConflict:
        raise
    except Exception as exception:
        logger.exception("说话人确认 provider 删除后热替换失败")
        raise HTTPException(status_code=500, detail=str(exception)) from exception
    response_payload = {
        **container.speaker_verification.current_profile(),
    }
    if committed_settings is not None:
        response_payload["settings"] = committed_settings
    return response_payload


@router.get("/api/speaker-verification/samples/{sample_id}/audio")
async def get_speaker_verification_sample_audio(
    sample_id: str,
    container: SpeakerVerificationDependency,
) -> Response:
    """读取指定白名单录音的音频内容。"""

    sample = container.hub.speaker_verification_assets.find_sample(sample_id)
    if sample is None or not sample.audio_path.exists():
        raise HTTPException(status_code=404, detail="说话人白名单录音不存在")
    return Response(content=sample.audio_path.read_bytes(), media_type="audio/wav")
