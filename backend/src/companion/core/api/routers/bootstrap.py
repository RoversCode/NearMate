from __future__ import annotations

from fastapi import APIRouter, HTTPException, Response
from starlette.concurrency import run_in_threadpool

from companion.core.api.dependencies import BootstrapDependency
from companion.core.api.schemas import (
    BootstrapInitializeRequest,
    LlmConfigRequest,
    QWeatherApiKeyRequest,
    TtsSetupStartRequest,
)
from companion.core.application.errors import StateConflict
from companion.core.execution_config import ExecutionConfiguration
from companion.llm.model_registry import model_registry_payload
from companion.llm.role_contracts import role_contracts_payload


router = APIRouter()


@router.get("/api/bootstrap/status")
async def get_bootstrap_status(container: BootstrapDependency) -> dict[str, object]:
    """读取已经提交的启动初始化状态。

    Args:
        container: 当前 Backend 的 Bootstrap 领域依赖。

    Returns:
        不包含明文密钥的初始化状态。
    """

    return await container.runtime_reconfiguration.current_bootstrap_status_payload()


@router.post("/api/bootstrap/initialize", status_code=202)
async def initialize_bootstrap(
    payload: BootstrapInitializeRequest,
    container: BootstrapDependency,
) -> dict[str, object]:
    """提交启动初始化密钥。"""

    return await container.runtime_reconfiguration.initialize_bootstrap(
        qweather_api_key=payload.qweather_api_key,
        qweather_api_host=payload.qweather_api_host,
    )


@router.post("/api/service-keys/qweather", status_code=202)
async def update_qweather_api_key(
    payload: QWeatherApiKeyRequest,
    container: BootstrapDependency,
) -> dict[str, object]:
    """验证并保存可选和风天气服务密钥。"""

    return await container.runtime_reconfiguration.update_qweather_api_key(
        api_key=payload.qweather_api_key,
        api_host=payload.qweather_api_host,
    )


@router.get("/api/llm/config")
async def get_llm_config(container: BootstrapDependency) -> dict[str, object]:
    """读取已经提交的 LLM 配置安全视图。

    Args:
        container: 当前 Backend 的 Bootstrap 领域依赖。

    Returns:
        不包含明文 API Key 的厂商凭据状态、职责绑定和逐职责就绪状态。
    """

    return await container.runtime_reconfiguration.current_llm_config_payload()


@router.get("/api/llm/account-status")
async def get_llm_account_status(container: BootstrapDependency) -> dict[str, object]:
    """读取当前 LLM 厂商账户余额的安全投影。

    Args:
        container: 启动与 LLM 路由依赖。

    Returns:
        不包含 API Key 和厂商原始响应的账户状态快照。
    """

    return await container.runtime_reconfiguration.current_llm_account_status_payload()


@router.put("/api/llm/config", status_code=202)
async def update_llm_config(
    payload: LlmConfigRequest,
    container: BootstrapDependency,
) -> dict[str, object]:
    """保存 schema v2 厂商凭据和显式职责绑定。"""

    return await container.runtime_reconfiguration.update_llm_config(
        credentials=payload.credentials,
        role_bindings=payload.role_bindings,
    )


@router.get("/api/llm/supported-models")
async def list_supported_llm_models() -> dict[str, object]:
    """读取项目已验证的厂商、接入规格和职责合同。"""

    return {
        **model_registry_payload(),
        "role_contracts": role_contracts_payload(),
    }


@router.get("/api/bootstrap/tts/models")
async def list_tts_models(container: BootstrapDependency) -> dict[str, object]:
    """只读模型文件清单和下载状态，不在事件循环中同步访问模型磁盘。

    Args:
        container: 当前 Backend 的语音配置依赖。
    Returns:
        发行模型、已下载状态与当前选用关系。
    """

    return await run_in_threadpool(container.tts_setup.catalog_payload)


@router.get("/api/bootstrap/tts/configuration")
async def get_execution_configuration(container: BootstrapDependency) -> dict:
    """读取已确认的执行方式和用户模型目录。"""

    return container.tts_setup.configuration_payload()


@router.post("/api/bootstrap/tts/probe")
async def probe_execution_configuration(container: BootstrapDependency) -> dict:
    """实际执行小型运算和设备初始化，返回可用执行方式及失败原因。

    探测会启动受限子进程
    等待硬件和动态库结果，不能直接阻塞 FastAPI 事件循环，失败由探测服务
    转换成结构化的不可用结果。

    Args:
        container: 首次语音配置服务所在的依赖切面。
    Returns:
        当前设备可用的执行方式和安全探测结果。
    """

    return await run_in_threadpool(container.tts_setup.probe_capabilities)


@router.put("/api/bootstrap/tts/configuration")
async def set_execution_configuration(payload: ExecutionConfiguration, container: BootstrapDependency) -> dict:
    """通过统一配置门禁提交执行方式；变化时关闭旧推理资源。"""

    try:
        return await container.runtime_reconfiguration.configure_voice_execution(payload)
    except (ValueError, OSError) as exception:
        raise HTTPException(status_code=400, detail=str(exception)) from exception


@router.get("/api/bootstrap/tts/status")
async def get_tts_setup_status(container: BootstrapDependency) -> dict[str, object]:
    """读取语音下载、准备和试听任务状态。"""

    return await run_in_threadpool(container.tts_setup.status_payload)


@router.get('/api/bootstrap/tts/downloads')
async def get_tts_model_download_status(container: BootstrapDependency) -> dict:
    """读取独立模型下载状态，下载成功不会打开或关闭语音门禁。

    Args:
        container: 当前 Backend 的语音配置依赖。
    Returns:
        文件下载进度与取消、重试资格。
    """

    return await run_in_threadpool(container.tts_setup.download_status_payload)


@router.post('/api/bootstrap/tts/downloads', status_code=202)
async def start_tts_model_download(payload: TtsSetupStartRequest, container: BootstrapDependency) -> dict:
    """只下载选定模型文件，保留当前角色语音与正在运行的助手。

    Args:
        payload: 发行固定的模型编号。
        container: 当前 Backend 的语音配置依赖。
    Returns:
        新文件下载任务的状态。
    """

    try:
        return await container.runtime_reconfiguration.start_voice_model_download(model_id=payload.model_id)
    except (ValueError, OSError) as exception:
        raise HTTPException(status_code=400, detail=str(exception)) from exception


@router.post('/api/bootstrap/tts/downloads/{task_id}/cancel', status_code=202)
async def cancel_tts_model_download(task_id: str, container: BootstrapDependency) -> dict:
    """取消当前文件下载，缓存保留且不会清除角色语音绑定。

    Args:
        task_id: 当前文件下载任务编号。
        container: 当前 Backend 的语音配置依赖。
    Returns:
        取消后的权威任务状态。
    """

    return await run_in_threadpool(container.tts_setup.cancel_model_download, task_id)


@router.post('/api/bootstrap/tts/downloads/{task_id}/retry', status_code=202)
async def retry_tts_model_download(task_id: str, container: BootstrapDependency) -> dict:
    """重试失败的模型下载，只恢复文件任务。

    Args:
        task_id: 当前可重试的文件下载任务编号。
        container: 当前 Backend 的语音配置依赖。
    Returns:
        新下载任务状态。
    """

    try:
        return await container.runtime_reconfiguration.start_voice_model_download(retry_task_id=task_id)
    except (ValueError, OSError) as exception:
        raise HTTPException(status_code=400, detail=str(exception)) from exception


@router.post("/api/bootstrap/tts/tasks", status_code=202)
async def start_tts_setup_task(
    payload: TtsSetupStartRequest,
    container: BootstrapDependency,
) -> dict[str, object]:
    """选择本地模型并启动受管语音配置任务。"""

    try:
        return await container.runtime_reconfiguration.start_voice_setup(model_id=payload.model_id)
    except StateConflict as exception:
        raise HTTPException(status_code=409, detail=str(exception)) from exception
    except ValueError as exception:
        raise HTTPException(status_code=400, detail=str(exception)) from exception


@router.post("/api/bootstrap/tts/tasks/{task_id}/cancel", status_code=202)
async def cancel_tts_setup_task(task_id: str, container: BootstrapDependency) -> dict[str, object]:
    """取消语音配置任务，保留下载缓存。"""

    try:
        return await run_in_threadpool(container.tts_setup.cancel, task_id)
    except StateConflict as exception:
        raise HTTPException(status_code=409, detail=str(exception)) from exception
    except ValueError as exception:
        raise HTTPException(status_code=409, detail=str(exception)) from exception


@router.post("/api/bootstrap/tts/tasks/{task_id}/retry", status_code=202)
async def retry_tts_setup_task(task_id: str, container: BootstrapDependency) -> dict[str, object]:
    """重试失败或取消的语音配置任务。"""

    try:
        return await container.runtime_reconfiguration.start_voice_setup(retry_task_id=task_id)
    except StateConflict as exception:
        raise HTTPException(status_code=409, detail=str(exception)) from exception
    except ValueError as exception:
        raise HTTPException(status_code=409, detail=str(exception)) from exception


@router.get("/api/bootstrap/tts/tasks/{task_id}/preview")
async def preview_tts_setup_task(task_id: str, container: BootstrapDependency) -> Response:
    """返回等待用户确认的试听 WAV。"""

    try:
        return Response(content=await run_in_threadpool(container.tts_setup.preview_bytes, task_id), media_type="audio/wav")
    except StateConflict as exception:
        raise HTTPException(status_code=409, detail=str(exception)) from exception
    except ValueError as exception:
        raise HTTPException(status_code=409, detail=str(exception)) from exception


@router.post("/api/bootstrap/tts/tasks/{task_id}/confirm", status_code=202)
async def confirm_tts_setup_task(task_id: str, container: BootstrapDependency) -> dict[str, object]:
    """确认试听并提交角色卡语音绑定。"""

    try:
        return await container.runtime_reconfiguration.confirm_voice_setup(task_id)
    except StateConflict as exception:
        raise HTTPException(status_code=409, detail=str(exception)) from exception
    except ValueError as exception:
        raise HTTPException(status_code=409, detail=str(exception)) from exception
