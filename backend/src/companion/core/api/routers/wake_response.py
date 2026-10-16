from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Response
from fastapi.responses import FileResponse

from companion.character_cards.store import CharacterCardStoreError
from companion.core.api.dependencies import WakeResponseDependency
from companion.core.api.schemas import WakeResponseConfigRequest, WakeResponsePreviewRequest, WakeResponseSynthesizeRequest
from companion.llm.client_factory import LlmRoleDisabledError
from companion.realtime.assistant_process_client import AssistantProcessNotReadyError


router = APIRouter()


@router.get("/api/character-cards/{card_id}/wake-response")
async def get_character_card_wake_response_config(
    card_id: str,
    container: WakeResponseDependency,
) -> dict[str, object]:
    """读取指定角色卡的唤醒回复配置。"""

    try:
        return container.wake_response.config_payload(card_id)
    except CharacterCardStoreError as exception:
        raise HTTPException(status_code=404, detail=str(exception)) from exception


@router.post("/api/character-cards/{card_id}/wake-response/generate")
async def generate_character_card_wake_response_config(
    card_id: str,
    container: WakeResponseDependency,
) -> dict[str, object]:
    """生成指定角色卡的唤醒回复文本，同时保持实时音频正常消费。

    Args:
        card_id: 要生成回复的角色卡编号。
        container: 提供角色卡与文本生成服务的依赖。

    Returns:
        按时段分组的生成文本。
    """

    try:
        # LLM 请求会等待网络响应，不能占住实时媒体和控制面共用的事件循环。
        return await asyncio.to_thread(container.wake_response.generate_texts, card_id)
    except CharacterCardStoreError as exception:
        raise HTTPException(status_code=404, detail=str(exception)) from exception
    except LlmRoleDisabledError as exception:
        raise HTTPException(status_code=503, detail=str(exception)) from exception
    except RuntimeError as exception:
        raise HTTPException(status_code=500, detail=str(exception)) from exception


@router.put("/api/character-cards/{card_id}/wake-response")
async def save_character_card_wake_response_config(
    card_id: str,
    payload: WakeResponseConfigRequest,
    container: WakeResponseDependency,
) -> dict[str, object]:
    """保存唤醒回复，并在工作线程合成缺失音频。

    Args:
        card_id: 要保存的角色卡编号。
        payload: 用户提交的分时回复文本。
        container: 提供唤醒回复保存服务的依赖。

    Returns:
        保存后的完整回复配置。
    """

    try:
        # 批量 TTS 可能持续几十秒；同步服务负责串行写入，事件循环继续接收和分发麦克风帧。
        return await asyncio.to_thread(
            container.wake_response.save_payload,
            card_id,
            {
                bucket: [item.model_dump() for item in items]
                for bucket, items in payload.buckets.items()
            },
        )
    except CharacterCardStoreError as exception:
        raise HTTPException(status_code=404, detail=str(exception)) from exception
    except AssistantProcessNotReadyError as exception:
        raise HTTPException(status_code=503, detail=str(exception)) from exception
    except RuntimeError as exception:
        raise HTTPException(status_code=500, detail=str(exception)) from exception


@router.post("/api/character-cards/{card_id}/wake-response/preview")
async def preview_character_card_wake_response(
    card_id: str,
    payload: WakeResponsePreviewRequest,
    container: WakeResponseDependency,
) -> Response:
    """在工作线程合成一条唤醒回复，返回可试听的音频。

    Args:
        card_id: 要试听的角色卡编号。
        payload: 用户输入的试听文字。
        container: 提供语音合成服务的依赖。

    Returns:
        WAV 音频响应。
    """

    try:
        wav_bytes = await asyncio.to_thread(container.wake_response.synthesize_wav, card_id, payload.text)
    except CharacterCardStoreError as exception:
        raise HTTPException(status_code=404, detail=str(exception)) from exception
    except AssistantProcessNotReadyError as exception:
        raise HTTPException(status_code=503, detail=str(exception)) from exception
    except RuntimeError as exception:
        raise HTTPException(status_code=500, detail=str(exception)) from exception
    return Response(content=wav_bytes, media_type="audio/wav")


@router.post("/api/character-cards/{card_id}/wake-response/{item_id}/synthesize")
async def synthesize_character_card_wake_response_item(
    card_id: str,
    item_id: str,
    payload: WakeResponseSynthesizeRequest,
    container: WakeResponseDependency,
) -> dict[str, object]:
    """在工作线程合成并保存指定的一条唤醒回复。

    Args:
        card_id: 要更新的角色卡编号。
        item_id: 要保存的回复条目编号。
        payload: 条目的时段与文字。
        container: 提供唤醒回复保存服务的依赖。

    Returns:
        合成后保存的回复配置。
    """

    try:
        return await asyncio.to_thread(
            container.wake_response.synthesize_item, card_id, item_id, bucket=payload.bucket, text=payload.text
        )
    except CharacterCardStoreError as exception:
        raise HTTPException(status_code=404, detail=str(exception)) from exception
    except KeyError:
        raise HTTPException(status_code=404, detail="wake response item not found")
    except AssistantProcessNotReadyError as exception:
        raise HTTPException(status_code=503, detail=str(exception)) from exception
    except RuntimeError as exception:
        raise HTTPException(status_code=500, detail=str(exception)) from exception


@router.delete("/api/character-cards/{card_id}/wake-response/{item_id}", status_code=204)
async def delete_character_card_wake_response_item(
    card_id: str,
    item_id: str,
    container: WakeResponseDependency,
) -> Response:
    """在工作线程删除一条唤醒回复，等待合成写锁时继续处理音频。

    Args:
        card_id: 回复所属的角色卡编号。
        item_id: 要删除的回复编号。
        container: 提供唤醒回复管理服务的依赖。

    Returns:
        删除成功后的空响应。
    """

    try:
        # 删除与批量保存共用服务写锁，等待也必须留在工作线程，避免被正在合成的请求堵住媒体消费。
        await asyncio.to_thread(container.wake_response.delete_item, card_id, item_id)
        return Response(status_code=204)
    except CharacterCardStoreError as exception:
        raise HTTPException(status_code=404, detail=str(exception)) from exception
    except KeyError:
        raise HTTPException(status_code=404, detail="wake response item not found")


@router.get("/api/character-cards/{card_id}/wake-response/audio/{file_name}")
async def get_character_card_wake_response_audio(
    card_id: str,
    file_name: str,
    container: WakeResponseDependency,
) -> FileResponse:
    """读取指定角色卡已合成的唤醒回复音频。"""

    try:
        audio_path = container.wake_response.audio_path_for_card(card_id, file_name)
    except CharacterCardStoreError as exception:
        raise HTTPException(status_code=404, detail=str(exception)) from exception
    except KeyError as exception:
        raise HTTPException(status_code=404, detail="wake response audio not found") from exception
    return FileResponse(audio_path, media_type="audio/wav")
