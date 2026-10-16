from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, File, HTTPException, Response, UploadFile
from starlette.concurrency import run_in_threadpool

from companion.character_cards.assets import MAX_CHARACTER_CARD_COVER_BYTES
from companion.character_cards.models import CharacterCardDefinition
from companion.character_cards.store import CharacterCardStoreError, SHARED_WORLD_CANON
from companion.core.api.dependencies import CharacterCardsApiDependencies, CharacterCardsDependency
from companion.core.api.schemas import CharacterCardCloneRequest, StreamVoxExampleGenerationRequest
from companion.core.application.errors import StateConflict
from companion.core.character_voice_service import (
    CharacterVoiceApplicationResult,
    CharacterVoiceCaptureResult,
    CharacterVoiceCalibrationResult,
)
from companion.core.settings import Settings
from companion.llm.client_factory import LlmRoleDisabledError
from companion.observability.logging_config import get_backend_logger
from companion.realtime.assistant_process_client import AssistantProcessNotReadyError


logger = get_backend_logger(__name__)
router = APIRouter()


def _runtime_unavailable_voice_status(card: CharacterCardDefinition) -> dict[str, object]:
    """构造 StreamVox 暂时不可读时仍可展示的最小状态。

    Args:
        card: 当前角色卡，用来保留 Console 需要展示的期望绑定。

    Returns:
        标记为 ``runtime_unavailable`` 的稳定状态载荷。
    """

    voice_binding = getattr(card.meta, "voice_binding", None)
    expected = (
        voice_binding.model_dump(mode="json")
        if callable(getattr(voice_binding, "model_dump", None))
        else {}
    )
    return {
        "voice_match_status": "runtime_unavailable",
        "runtime": None,
        "expected": expected,
    }


async def _read_voice_status(
    container: CharacterCardsApiDependencies,
    card: CharacterCardDefinition,
) -> dict[str, object]:
    """在线程池中读取语音状态，并把附属查询故障降级成稳定载荷。

    角色卡写入和设置 revision 已经在前面的事务中完成；这里的 StreamVox
    状态只用于给 Console 做展示。RPC 是同步阻塞调用，必须离开 FastAPI
    事件循环；即使注入的状态服务在热替换或超时期间抛出异常，也不能把
    已经成功提交的角色卡改报成 HTTP 500。

    Args:
        container: 角色卡领域依赖。
        card: 已经提交或即将展示的角色卡。

    Returns:
        完整语音状态；状态读取失败时返回 ``runtime_unavailable``。
    """

    try:
        status_payload = await run_in_threadpool(container.character_voice.status, card)
    except Exception as exception:  # noqa: BLE001
        logger.warning(
            "读取角色语音状态失败，降级为 runtime_unavailable：card_id=%s "
            "error_type=%s error=%s",
            card.meta.card_id,
            type(exception).__name__,
            exception,
        )
        return _runtime_unavailable_voice_status(card)

    if not isinstance(status_payload, dict):
        logger.warning(
            "角色语音状态响应不是对象，降级为 runtime_unavailable：card_id=%s",
            card.meta.card_id,
        )
        return _runtime_unavailable_voice_status(card)
    status = status_payload.get("voice_match_status")
    if not isinstance(status, str) or not status.strip():
        logger.warning(
            "角色语音状态缺少合法 voice_match_status，降级为 runtime_unavailable："
            "card_id=%s",
            card.meta.card_id,
        )
        return _runtime_unavailable_voice_status(card)
    return status_payload


async def _safe_voice_match_status(
    container: CharacterCardsApiDependencies,
    card: CharacterCardDefinition,
) -> str:
    """读取提交后语音状态，并返回 Console 可展示的匹配标记。

    Args:
        container: 角色卡领域依赖。
        card: 已经提交或即将展示的角色卡。

    Returns:
        语音匹配状态；状态读取失败时返回 ``runtime_unavailable``。
    """

    status_payload = await _read_voice_status(container, card)
    return str(status_payload["voice_match_status"])


def _safe_active_card_id(
    container: CharacterCardsApiDependencies,
    card: CharacterCardDefinition | None,
) -> str:
    """读取当前角色卡编号，资产暂时不可读时使用响应中的目标卡兜底。

    Args:
        container: 角色卡领域依赖。
        card: 本次响应中已经成功读取的角色卡；可以为空。

    Returns:
        当前激活卡编号；无法再次读取激活标记时返回目标卡编号或空字符串。
    """

    try:
        active_card_id = container.hub.character_cards.load_active_card_id()
    except Exception as exception:  # noqa: BLE001
        # 角色卡写入已经完成后，active.json 可能短暂被外部进程占用或损坏。
        # 这里的编号只是响应投影，不能把一次成功的 mutation 重新报成 500。
        fallback_card_id = getattr(getattr(card, "meta", None), "card_id", "")
        logger.warning(
            "读取当前激活角色卡编号失败，使用响应卡编号兜底：error_type=%s error=%s",
            type(exception).__name__,
            exception,
        )
        return str(fallback_card_id) if isinstance(fallback_card_id, str) else ""

    if isinstance(active_card_id, str):
        return active_card_id

    fallback_card_id = getattr(getattr(card, "meta", None), "card_id", "")
    return str(fallback_card_id) if isinstance(fallback_card_id, str) else ""


def character_card_payload(
    *,
    container: CharacterCardsApiDependencies,
    card: CharacterCardDefinition | None,
    effect_message: str | None = None,
    validation_messages: list[str] | None = None,
    voice_match_status: str | None = None,
    settings: dict[str, object] | None = None,
) -> dict[str, object]:
    """构造角色卡控制台响应。

    Args:
        container: 角色卡领域依赖，用于读取当前激活卡编号。
        card: 要返回的角色卡；读取失败时可以为空。
        effect_message: 本次操作对用户的简短说明。
        validation_messages: 角色卡校验提示。
        voice_match_status: 当前 StreamVox 语音匹配状态。
        settings: 若本次操作推进了统一设置事务，则附带提交点的完整设置快照。

    Returns:
        可直接交给 Console 的角色卡响应。
    """

    payload: dict[str, object] = {
        "card": card.model_dump(mode="json") if card is not None else None,
        "active_card_id": _safe_active_card_id(container, card),
        "shared_world_canon": SHARED_WORLD_CANON,
        "validation_messages": validation_messages or [],
        "effect_message": effect_message,
        "voice_match_status": voice_match_status,
    }
    if settings is not None:
        # 角色卡专用写接口与普通设置共享 revision；返回提交点快照可避免
        # Console 在附属 GET 失败后继续携带旧版本。
        payload["settings"] = settings
    return payload


@router.get("/api/character-cards")
async def list_character_cards(container: CharacterCardsDependency) -> dict[str, object]:
    """读取全部可用角色卡。"""

    try:
        cards = container.hub.character_cards.list_cards()
    except CharacterCardStoreError as exception:
        raise HTTPException(status_code=500, detail=str(exception)) from exception
    return {
        "cards": [card.model_dump(mode="json") for card in cards],
        # active.json 是独立的投影文件；它暂时不可读时仍然返回角色卡列表，
        # 让 Console 可以继续编辑和修复资产。
        "active_card_id": _safe_active_card_id(container, None),
        "shared_world_canon": SHARED_WORLD_CANON,
    }


@router.get("/api/character-cards/active")
async def get_active_character_card(container: CharacterCardsDependency) -> dict[str, object]:
    """读取当前激活角色卡。"""

    try:
        card = container.hub.character_cards.load_active_card()
    except CharacterCardStoreError as exception:
        raise HTTPException(status_code=500, detail=str(exception)) from exception
    return character_card_payload(container=container, card=card)


@router.put("/api/character-cards/{card_id}", status_code=202)
async def update_character_card(
    card_id: str,
    payload: CharacterCardDefinition,
    container: CharacterCardsDependency,
) -> dict[str, object]:
    """保存一张角色卡。"""

    if payload.meta.card_id != card_id.strip():
        raise HTTPException(status_code=400, detail="角色卡编号与路径不一致")
    try:
        container.hub.character_cards.save_card(payload)
    except CharacterCardStoreError as exception:
        raise HTTPException(status_code=400, detail=str(exception)) from exception
    return character_card_payload(container=container, card=payload, effect_message="新会话生效")


@router.post("/api/character-cards/{card_id}/voice/reference-audio", status_code=202)
async def upload_character_card_voice_reference_audio(
    card_id: str,
    container: CharacterCardsDependency,
    audio_file: UploadFile = File(...),
) -> dict[str, object]:
    """上传角色卡语音参考音频资产。

    Args:
        card_id: 路由中的角色卡编号。
        audio_file: 用户选择的参考音频文件。

    Returns:
        写入当前角色卡草稿所需的卡内相对路径。
    """

    try:
        card = container.hub.character_cards.load_card(card_id)
        reference_audio_path = container.hub.character_card_assets.save_uploaded_voice_reference_audio(
            card_id=card.meta.card_id,
            file_name=audio_file.filename or "reference.wav",
            content=await audio_file.read(),
        )
    except CharacterCardStoreError as exception:
        raise HTTPException(status_code=400, detail=str(exception)) from exception
    return {
        "reference_audio_path": reference_audio_path,
        "effect_message": "参考音频已上传，保存当前卡后生效",
    }


@router.post("/api/character-cards/{card_id}/streamvox-examples/generate")
async def generate_character_card_streamvox_examples(
    card_id: str,
    payload: StreamVoxExampleGenerationRequest,
    container: CharacterCardsDependency,
) -> dict[str, object]:
    """根据角色卡草稿生成指定 StreamVox 模型示例。"""

    try:
        example_set = container.streamvox_examples.generate_examples(
            path_card_id=card_id,
            card=payload.card,
            target_model=payload.target_model,
        )
    except CharacterCardStoreError as exception:
        raise HTTPException(status_code=404, detail=str(exception)) from exception
    except LlmRoleDisabledError as exception:
        raise HTTPException(status_code=503, detail=str(exception)) from exception
    except ValueError as exception:
        raise HTTPException(status_code=400, detail=str(exception)) from exception
    except RuntimeError as exception:
        raise HTTPException(status_code=500, detail=str(exception)) from exception
    return example_set.model_dump(mode="json")


@router.post("/api/character-cards/{card_id}/activate", status_code=202)
async def activate_character_card(
    card_id: str,
    container: CharacterCardsDependency,
) -> dict[str, object]:
    """切换当前角色卡，并在所有运行时副作用成功后提交新快照。

    Args:
        card_id: 要激活的角色卡稳定编号。
        container: 当前角色卡领域依赖。

    Returns:
        已激活角色卡的公开详情和语音匹配状态。

    Raises:
        HTTPException: 角色卡不存在、运行时切换失败或提交没有产生结果时抛出。
    """

    card: CharacterCardDefinition | None = None
    previous_active_card: CharacterCardDefinition | None = None
    previous_wake_response_asset_dir: Path | None = None
    wakeword_replace_attempted = False
    committed_settings: dict[str, object] | None = None
    replace_wakeword_keywords = getattr(
        container.realtime_input_runtime_owner.runtime,
        "replace_wakeword_keywords",
        None,
    )

    def prepare_character_activation(
        current_settings: Settings,
    ) -> dict[str, object]:
        """切换角色资产和唤醒词，并触发新的完整设置快照。

        激活角色会改变完整设置快照中派生的字幕速度。即使没有直接修改
        Settings 字段，也必须通过统一事务推进 revision 并广播新快照。

        Args:
            current_settings: 统一提交锁保护下的最新设置。

        Returns:
            空字段 patch；角色变化由角色卡资产单独持久化。
        """

        nonlocal card, previous_active_card, previous_wake_response_asset_dir
        nonlocal wakeword_replace_attempted
        previous_active_card = container.hub.character_cards.load_active_card()
        previous_wake_response_asset_dir = (
            container.hub.wake_response_service.asset_dir
        )
        card = container.hub.character_cards.load_card(card_id)
        container.hub.character_cards.set_active_card_id(card.meta.card_id)
        container.hub.wake_response_service.asset_dir = (
            container.hub.character_card_assets.wake_response_audio_dir(
                card.meta.card_id
            )
        )
        if callable(replace_wakeword_keywords):
            # 子进程调用抛错时也可能已经应用了一部分状态，因此调用前就记录
            # 补偿资格，失败事务会用旧角色的关键词恢复运行时。
            wakeword_replace_attempted = True
            replace_wakeword_keywords(
                settings=current_settings,
                realtime_audio_config=container.realtime_audio_config,
                keywords=tuple(card.meta.wakeword.keywords),
            )
        return {}

    def rollback_character_activation(previous_settings: Settings) -> None:
        """恢复提交失败前的激活角色、唤醒音频目录和唤醒词。

        Args:
            previous_settings: 本事务开始前的完整设置。

        Returns:
            无返回值。
        """

        if previous_active_card is None:
            return
        container.hub.character_cards.set_active_card_id(
            previous_active_card.meta.card_id
        )
        if previous_wake_response_asset_dir is not None:
            container.hub.wake_response_service.asset_dir = (
                previous_wake_response_asset_dir
            )
        if wakeword_replace_attempted and callable(replace_wakeword_keywords):
            replace_wakeword_keywords(
                settings=previous_settings,
                realtime_audio_config=container.realtime_audio_config,
                keywords=tuple(previous_active_card.meta.wakeword.keywords),
            )

    try:
        committed_settings = await container.user_settings.commit_user_settings(
            prepare_update=prepare_character_activation,
            rollback_external=rollback_character_activation,
        )
    except CharacterCardStoreError as exception:
        raise HTTPException(status_code=404, detail=str(exception)) from exception
    except StateConflict as exception:
        raise HTTPException(status_code=409, detail=exception.detail) from exception
    except AssistantProcessNotReadyError as exception:
        raise HTTPException(status_code=503, detail=str(exception)) from exception
    except RuntimeError as exception:
        raise HTTPException(status_code=500, detail=str(exception)) from exception
    if card is None:
        raise HTTPException(status_code=500, detail="角色卡激活没有返回结果")
    return character_card_payload(
        container=container,
        card=card,
        effect_message="角色卡已轻激活，语音匹配状态已更新",
        voice_match_status=await _safe_voice_match_status(container, card),
        settings=committed_settings,
    )


@router.get("/api/character-cards/{card_id}/voice-status")
async def get_character_card_voice_status(
    card_id: str,
    container: CharacterCardsDependency,
) -> dict[str, object]:
    """读取角色卡语音绑定与当前 StreamVox runtime 的匹配状态。"""

    try:
        card = container.hub.character_cards.load_card(card_id)
    except CharacterCardStoreError as exception:
        raise HTTPException(status_code=404, detail=str(exception)) from exception
    return await _read_voice_status(container, card)


@router.post("/api/character-cards/{card_id}/voice/apply", status_code=202)
async def apply_character_card_voice(
    card_id: str,
    container: CharacterCardsDependency,
) -> dict[str, object]:
    """应用角色卡期望的 StreamVox 模型和音色。"""

    application: CharacterVoiceApplicationResult | None = None
    committed_settings: dict[str, object] | None = None

    def prepare_character_voice_update(
        current_settings: Settings,
    ) -> dict[str, object]:
        """应用最新角色卡并返回 StreamVox 设置 patch。

        Args:
            current_settings: 统一提交锁保护下的最新设置。

        Returns:
            StreamVox 实际设备与 llama.cpp 后端字段。
        """

        nonlocal application
        card = container.hub.character_cards.load_card(card_id)
        application = container.character_voice.apply(
            card,
            current_settings=current_settings,
        )
        return application.settings_patch

    def rollback_character_voice_update(_previous_settings: Settings) -> None:
        """恢复未提交的角色语音运行状态。

        Args:
            _previous_settings: 统一事务提供的旧设置；补偿信息已经包含运行快照。

        Returns:
            无返回值。
        """

        if application is not None:
            container.character_voice.rollback_application(application)

    try:
        committed_settings = await container.user_settings.commit_user_settings(
            prepare_update=prepare_character_voice_update,
            rollback_external=rollback_character_voice_update,
        )
    except CharacterCardStoreError as exception:
        raise HTTPException(status_code=404, detail=str(exception)) from exception
    except StateConflict as exception:
        raise HTTPException(status_code=409, detail=exception.detail) from exception
    except AssistantProcessNotReadyError as exception:
        raise HTTPException(status_code=503, detail=str(exception)) from exception
    except RuntimeError as exception:
        raise HTTPException(status_code=500, detail=str(exception)) from exception
    if application is None:
        raise HTTPException(status_code=500, detail="角色语音应用没有返回结果")
    response_payload = {
        **application.payload,
    }
    if committed_settings is not None:
        # 语音应用推进了统一 settings_revision；把同一提交点快照返回给 Console。
        response_payload["settings"] = committed_settings
    return response_payload


@router.post("/api/character-cards/{card_id}/voice-caption-calibration", status_code=202)
async def calibrate_character_card_voice_caption(
    card_id: str,
    container: CharacterCardsDependency,
) -> dict[str, object]:
    """测算当前角色音色的桌宠字幕语速并写回角色卡。"""

    calibration: CharacterVoiceCalibrationResult | None = None
    committed_settings: dict[str, object] | None = None

    def prepare_caption_calibration(
        current_settings: Settings,
    ) -> dict[str, object]:
        """应用最新角色语音、测算字幕语速并返回设置 patch。

        Args:
            current_settings: 统一提交锁保护下的最新设置。

        Returns:
            StreamVox 实际设备与 llama.cpp 后端字段。
        """

        nonlocal calibration
        card = container.hub.character_cards.load_card(card_id)
        calibration = container.character_voice.calibrate_caption_timing(
            card,
            current_settings=current_settings,
        )
        return calibration.application.settings_patch

    def rollback_caption_calibration(_previous_settings: Settings) -> None:
        """恢复未提交校准对应的运行时和角色卡。

        Args:
            _previous_settings: 统一事务提供的旧设置；补偿信息已单独保存。

        Returns:
            无返回值。
        """

        if calibration is not None:
            container.character_voice.rollback_application(
                calibration.application
            )

    try:
        committed_settings = await container.user_settings.commit_user_settings(
            prepare_update=prepare_caption_calibration,
            rollback_external=rollback_caption_calibration,
        )
    except CharacterCardStoreError as exception:
        raise HTTPException(status_code=404, detail=str(exception)) from exception
    except HTTPException:
        raise
    except StateConflict as exception:
        raise HTTPException(status_code=409, detail=exception.detail) from exception
    except AssistantProcessNotReadyError as exception:
        raise HTTPException(status_code=503, detail=str(exception)) from exception
    except ValueError as exception:
        raise HTTPException(status_code=409, detail=str(exception)) from exception
    except RuntimeError as exception:
        raise HTTPException(status_code=500, detail=str(exception)) from exception
    if calibration is None:
        raise HTTPException(status_code=500, detail="字幕语速校准没有返回结果")
    return character_card_payload(
        container=container,
        card=calibration.card,
        effect_message="字幕语速已测算",
        voice_match_status=await _safe_voice_match_status(container, calibration.card),
        settings=committed_settings,
    )


@router.post("/api/character-cards/{card_id}/voice/capture-from-runtime", status_code=202)
async def capture_character_card_voice_from_runtime(
    card_id: str,
    container: CharacterCardsDependency,
) -> dict[str, object]:
    """把当前 StreamVox 运行语音捕获为当前激活角色卡绑定。"""

    capture: CharacterVoiceCaptureResult | None = None
    committed_settings: dict[str, object] | None = None

    def prepare_runtime_capture(current_settings: Settings) -> dict[str, object]:
        """在设置提交锁内捕获角色语音并返回空字段 patch。

        Args:
            current_settings: 统一设置事务锁保护下的最新设置；捕获本身不直接
                修改其中的字段，但完整快照的派生字幕节奏会随角色卡变化。

        Returns:
            空 patch；角色卡资产由捕获事实单独持久化。

        Raises:
            StateConflict: 锁内发现目标角色已经不是当前激活卡。
        """

        del current_settings
        nonlocal capture
        if container.hub.character_cards.load_active_card_id() != card_id.strip():
            raise StateConflict(
                "can only capture runtime voice to active character card"
            )
        card = container.hub.character_cards.load_card(card_id)
        capture = container.character_voice.prepare_capture_from_runtime(card)
        return {}

    def rollback_runtime_capture(_previous_settings: Settings) -> None:
        """设置提交失败时恢复角色卡 YAML 和参考音频资产。"""

        if capture is not None:
            container.character_voice.rollback_capture_from_runtime(capture)

    try:
        committed_settings = await container.user_settings.commit_user_settings(
            prepare_update=prepare_runtime_capture,
            rollback_external=rollback_runtime_capture,
        )
    except HTTPException:
        raise
    except StateConflict as exception:
        raise HTTPException(status_code=409, detail=exception.detail) from exception
    except AssistantProcessNotReadyError as exception:
        raise HTTPException(status_code=503, detail=str(exception)) from exception
    except RuntimeError as exception:
        raise HTTPException(status_code=500, detail=str(exception)) from exception
    except CharacterCardStoreError as exception:
        raise HTTPException(status_code=400, detail=str(exception)) from exception
    if capture is None:
        raise HTTPException(status_code=500, detail="运行时音色捕获没有返回结果")
    return character_card_payload(
        container=container,
        card=capture.card,
        effect_message="已更新到角色卡",
        voice_match_status=await _safe_voice_match_status(container, capture.card),
        settings=committed_settings,
    )


@router.post("/api/character-cards/{card_id}/clone", status_code=201)
async def clone_character_card(
    card_id: str,
    payload: CharacterCardCloneRequest,
    container: CharacterCardsDependency,
) -> dict[str, object]:
    """复制一张角色卡。"""

    try:
        card = container.hub.character_cards.clone_card(
            card_id,
            card_id=payload.card_id,
            name=payload.name,
            title=payload.title,
        )
    except CharacterCardStoreError as exception:
        raise HTTPException(status_code=400, detail=str(exception)) from exception
    return character_card_payload(container=container, card=card, effect_message="新角色卡已创建")


@router.post("/api/character-cards/{card_id}/cover", status_code=202)
async def upload_character_card_cover(
    card_id: str,
    container: CharacterCardsDependency,
    image_file: UploadFile = File(...),
) -> dict[str, object]:
    """上传或替换角色卡封面。"""

    try:
        content = await image_file.read(MAX_CHARACTER_CARD_COVER_BYTES + 1)
        updated_card = container.hub.character_cards.save_cover_asset(
            card_id,
            content_type=image_file.content_type or "",
            content=content,
        )
    except CharacterCardStoreError as exception:
        raise HTTPException(status_code=400, detail=str(exception)) from exception
    return character_card_payload(container=container, card=updated_card, effect_message="封面已更新")


@router.delete("/api/character-cards/{card_id}/cover", status_code=202)
async def delete_character_card_cover(
    card_id: str,
    container: CharacterCardsDependency,
) -> dict[str, object]:
    """删除角色卡封面。"""

    try:
        updated_card = container.hub.character_cards.delete_cover_asset(card_id)
    except CharacterCardStoreError as exception:
        raise HTTPException(status_code=400, detail=str(exception)) from exception
    return character_card_payload(container=container, card=updated_card, effect_message="封面已删除")


@router.get("/api/character-cards/{card_id}/asset/{asset_path:path}")
async def get_character_card_asset(
    card_id: str,
    asset_path: str,
    container: CharacterCardsDependency,
) -> Response:
    """读取当前卡已声明的资产文件。"""

    try:
        resolved_path = container.hub.character_cards.resolve_cover_asset(card_id, asset_path)
    except CharacterCardStoreError as exception:
        raise HTTPException(status_code=404, detail=str(exception)) from exception
    media_type = "image/png" if resolved_path.suffix.lower() == ".png" else "image/jpeg"
    return Response(content=resolved_path.read_bytes(), media_type=media_type)
