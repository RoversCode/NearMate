from __future__ import annotations

from fastapi import APIRouter, File, Form, HTTPException, Response, UploadFile
from starlette.concurrency import run_in_threadpool

from companion.core.api.dependencies import StreamVoxDependency
from companion.core.api.schemas import StreamVoxConfigRequest, StreamVoxRolePreviewRequest
from companion.core.application.errors import ServiceUnavailable
from companion.core.settings import Settings
from companion.core.streamvox_assets import save_streamvox_role_reference_audio
from companion.core.streamvox_runtime_config import StreamVoxRuntimeConfiguration
from companion.realtime.assistant_process_client import AssistantProcessNotReadyError


router = APIRouter()


@router.get("/api/streamvox/runtime")
async def get_streamvox_runtime(container: StreamVoxDependency) -> dict[str, object]:
    """读取 StreamVox 当前运行状态。"""

    runtime = container.assistant_process_supervisor.require_for_streamvox_management()
    try:
        # 管理 RPC 底层是会硬阻塞的跨进程 Queue.get；必须放到线程池，
        # 否则同一事件循环上的控制广播和授权监控会被一起冻结。
        return await run_in_threadpool(runtime.get_streamvox_runtime_state, timeout=5.0)
    except AssistantProcessNotReadyError as exception:
        raise ServiceUnavailable(str(exception)) from exception
    except RuntimeError as exception:
        raise HTTPException(status_code=500, detail=str(exception)) from exception


@router.post("/api/streamvox/config", status_code=202)
async def update_streamvox_config(
    payload: StreamVoxConfigRequest,
    container: StreamVoxDependency,
) -> dict[str, object]:
    """保存并切换 StreamVox 运行配置。"""

    runtime = container.assistant_process_supervisor.require_for_streamvox_management()
    target_configuration = StreamVoxRuntimeConfiguration(
        model=payload.model,
        device=payload.device,
        llama_backend=payload.llama_backend,
        role_name=payload.role_name,
        streamvox_seed=payload.streamvox_seed,
        inference_options=dict(payload.inference_options),
    )
    previous_configuration: StreamVoxRuntimeConfiguration | None = None
    runtime_switch_attempted = False
    committed_runtime_state: dict[str, object] | None = None
    committed_settings: dict[str, object] | None = None

    def prepare_streamvox_update(
        _current_settings: Settings,
    ) -> dict[str, object]:
        """切换 StreamVox 并返回实际设备字段 patch。

        Args:
            _current_settings: 拿到统一提交锁后的最新设置；切换参数全部来自请求。

        Returns:
            子进程实际采用的设备与 llama.cpp 后端字段。
        """

        nonlocal previous_configuration, runtime_switch_attempted
        nonlocal committed_runtime_state
        current_runtime_state = runtime.get_streamvox_runtime_state(timeout=5.0)
        previous_configuration = StreamVoxRuntimeConfiguration.from_runtime_state(
            current_runtime_state
        )
        if target_configuration.matches_runtime_state(current_runtime_state):
            committed_runtime_state = current_runtime_state
        else:
            # 切换命令即使抛错也可能已经触达子进程，所以调用前先记录补偿资格。
            runtime_switch_attempted = True
            committed_runtime_state = target_configuration.apply(runtime)

        applied_configuration = StreamVoxRuntimeConfiguration.from_runtime_state(
            committed_runtime_state
        )
        return applied_configuration.user_settings_patch()

    def rollback_streamvox_update(_previous_settings: Settings) -> None:
        """设置提交失败时恢复切换前的 StreamVox 配置。

        Args:
            _previous_settings: 统一事务提供的旧设置；运行配置已有独立快照。

        Returns:
            无返回值。
        """

        if not runtime_switch_attempted or previous_configuration is None:
            return
        previous_configuration.apply(runtime)

    try:
        committed_settings = await container.user_settings.commit_user_settings(
            prepare_update=prepare_streamvox_update,
            rollback_external=rollback_streamvox_update,
        )
    except AssistantProcessNotReadyError as exception:
        raise ServiceUnavailable(str(exception)) from exception
    except RuntimeError as exception:
        raise HTTPException(status_code=500, detail=str(exception)) from exception

    if committed_runtime_state is None:
        raise HTTPException(status_code=500, detail="StreamVox config switch returned no state")
    response_payload = {
        **committed_runtime_state,
    }
    if committed_settings is not None:
        # StreamVox 配置与普通设置共用 settings_revision；附带提交点快照，
        # 让 Console 无需依赖一次额外 GET 才能继续保存其它设置。
        response_payload["settings"] = committed_settings
    return response_payload


@router.post("/api/streamvox/roles", status_code=202)
async def create_streamvox_role(
    container: StreamVoxDependency,
    role_name: str = Form(...),
    prompt_text: str = Form(...),
    audio_file: UploadFile = File(...),
) -> dict[str, object]:
    """创建当前模型下的自定义 StreamVox 音色。"""

    runtime = container.assistant_process_supervisor.require_for_streamvox_management()
    normalized_prompt_text = prompt_text.strip()
    if not normalized_prompt_text:
        raise HTTPException(status_code=400, detail="StreamVox prompt_text must not be blank")
    try:
        # 两次跨进程管理 RPC 都会硬阻塞当前线程，统一经线程池执行以保持事件循环可用。
        current_runtime_state = await run_in_threadpool(
            runtime.get_streamvox_runtime_state,
            timeout=5.0,
        )
        audio_content = await audio_file.read()
        role_asset = save_streamvox_role_reference_audio(
            assets_root=container.hub.bootstrap_secret_store.db_path.parent / "streamvox_roles",
            model=str(
                current_runtime_state.get(
                    "model",
                    container.hub.current_character_experience_runtime_config().streamvox_model,
                )
            ),
            role_name=role_name,
            original_filename=audio_file.filename or "reference.wav",
            content=audio_content,
        )
        return await run_in_threadpool(
            runtime.create_streamvox_role,
            role_name=role_asset.role_name,
            audio_path=str(role_asset.audio_path),
            prompt_text=normalized_prompt_text,
            timeout=30.0,
        )
    except ValueError as exception:
        raise HTTPException(status_code=400, detail=str(exception)) from exception
    except AssistantProcessNotReadyError as exception:
        raise ServiceUnavailable(str(exception)) from exception
    except RuntimeError as exception:
        raise HTTPException(status_code=500, detail=str(exception)) from exception
    finally:
        await audio_file.close()


@router.post("/api/streamvox/roles/{role_name}/preview")
async def preview_streamvox_role(
    role_name: str,
    payload: StreamVoxRolePreviewRequest,
    container: StreamVoxDependency,
) -> Response:
    """试听当前模型下已创建 StreamVox 角色的推理声音。"""

    runtime = container.assistant_process_supervisor.require_for_streamvox_management()
    try:
        # 试听推理最长等待 30 秒，硬阻塞 RPC 必须离开事件循环线程。
        preview = await run_in_threadpool(
            runtime.preview_streamvox_role,
            role_name=role_name,
            text=payload.text,
            timeout=30.0,
        )
    except AssistantProcessNotReadyError as exception:
        raise ServiceUnavailable(str(exception)) from exception
    except RuntimeError as exception:
        raise HTTPException(status_code=500, detail=str(exception)) from exception
    wav_bytes = preview.get("wav_bytes", b"")
    if not isinstance(wav_bytes, bytes):
        raise HTTPException(status_code=500, detail="StreamVox preview response missing wav bytes")
    return Response(content=wav_bytes, media_type="audio/wav")


@router.delete("/api/streamvox/roles/{role_name}", status_code=202)
async def delete_streamvox_role(
    role_name: str,
    container: StreamVoxDependency,
) -> dict[str, object]:
    """删除当前模型下的非当前使用角色。"""

    normalized_role_name = role_name.strip()
    runtime = container.assistant_process_supervisor.require_for_streamvox_management()
    try:
        # 删除前的状态读取同样是硬阻塞 RPC，不能在事件循环线程里直接等待。
        current_runtime_state = await run_in_threadpool(
            runtime.get_streamvox_runtime_state,
            timeout=5.0,
        )
    except AssistantProcessNotReadyError as exception:
        raise ServiceUnavailable(str(exception)) from exception
    except RuntimeError as exception:
        raise HTTPException(status_code=500, detail=str(exception)) from exception
    if normalized_role_name == str(current_runtime_state.get("role_name", "")):
        raise HTTPException(status_code=409, detail="cannot delete current StreamVox role")
    try:
        return await run_in_threadpool(
            runtime.delete_streamvox_role,
            role_name=normalized_role_name,
            timeout=10.0,
        )
    except AssistantProcessNotReadyError as exception:
        raise ServiceUnavailable(str(exception)) from exception
    except RuntimeError as exception:
        raise HTTPException(status_code=500, detail=str(exception)) from exception
