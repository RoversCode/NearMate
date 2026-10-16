"""核对首次语音配置路由的可达性、委托参数与状态机错误映射。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from companion.core.api.dependencies import get_bootstrap_dependencies
from companion.core.api.exception_handlers import install_application_error_handlers
from companion.core.api.routers import bootstrap
from companion.core.application.errors import InvalidRequest, ServiceUnavailable, StateConflict
from companion.core.execution_config import ExecutionConfiguration


class FakeTtsSetupService:
    """记录路由委托参数的语音配置服务替身。"""

    def __init__(self) -> None:
        """准备固定载荷和可注入的冲突错误。"""

        self.catalog = {"provider": "local_streamvox", "models": [], "configured_model": None}
        self.configuration = {"confirmed": False, "selection": None, "model_directory": "G:/models", "capabilities": None}
        self.status = {"provider": "local_streamvox", "is_ready": False, "phase": "unconfigured"}
        self.preview = b"RIFF....WAVEfmt "
        self.conflict: StateConflict | None = None
        self.cancelled_task_ids: list[str] = []
        self.preview_task_ids: list[str] = []
        self.cancelled_download_ids: list[str] = []
        self.download_status = {'phase': 'idle', 'task_id': None, 'model_id': None, 'progress': None,
                                'error': None, 'can_cancel': False, 'can_retry': False, 'can_confirm': False}

    def catalog_payload(self) -> dict[str, object]:
        """返回固定模型目录。"""

        return self.catalog

    def configuration_payload(self) -> dict[str, object]:
        """返回固定执行配置视图。"""

        return self.configuration

    def probe_capabilities(self) -> dict[str, object]:
        """返回固定探测结果。"""

        return {"backends": [], "recommendation": None}

    def status_payload(self) -> dict[str, object]:
        """返回固定任务状态。"""

        return self.status

    def cancel(self, task_id: str) -> dict[str, object]:
        """记录取消任务并按需抛出冲突。"""

        if self.conflict is not None:
            raise self.conflict
        self.cancelled_task_ids.append(task_id)
        return self.status

    def preview_bytes(self, task_id: str) -> bytes:
        """记录试听任务并按需抛出冲突。"""

        if self.conflict is not None:
            raise self.conflict
        self.preview_task_ids.append(task_id)
        return self.preview

    def download_status_payload(self) -> dict:
        """返回纯文件下载快照，保持原语音状态独立。"""

        return self.download_status

    def cancel_model_download(self, task_id: str) -> dict:
        """记录文件任务取消，供路由委托断言。"""

        if self.conflict is not None:
            raise self.conflict
        self.cancelled_download_ids.append(task_id)
        return self.download_status


class FakeVoiceCoordinator:
    """记录语音配置门禁收到的参数的协调器替身。"""

    def __init__(self) -> None:
        """准备完整 Bootstrap 状态载荷和可注入的错误。"""

        self.bootstrap_status = {
            "llm": {"is_ready": True, "missing": [], "errors": [], "roles": {}},
            "voice": {"provider": "local_streamvox", "is_ready": True, "phase": "ready"},
            "has_qweather_api_key": True,
            "is_initialized": True,
        }
        self.error: Exception | None = None
        self.configured: list[ExecutionConfiguration] = []
        self.started_model_ids: list[str] = []
        self.retried_task_ids: list[str] = []
        self.confirmed_task_ids: list[str] = []
        self.downloaded_model_ids: list[str] = []
        self.retried_download_ids: list[str] = []

    def _raise_if_failed(self) -> None:
        """统一抛出测试注入的错误。"""

        if self.error is not None:
            raise self.error

    async def configure_voice_execution(self, configuration: ExecutionConfiguration) -> dict[str, object]:
        """记录路由解析后的执行配置模型。"""

        self._raise_if_failed()
        self.configured.append(configuration)
        return {"confirmed": True}

    async def start_voice_setup(self, *, model_id: str | None = None, retry_task_id: str | None = None) -> dict[str, object]:
        """记录新任务模型或重试任务编号。"""

        self._raise_if_failed()
        if model_id is not None:
            self.started_model_ids.append(model_id)
        if retry_task_id is not None:
            self.retried_task_ids.append(retry_task_id)
        return {"task_id": "task-1", "phase": "queued"}

    async def confirm_voice_setup(self, task_id: str) -> dict[str, object]:
        """记录确认任务并返回完整 Bootstrap 状态。"""

        self._raise_if_failed()
        self.confirmed_task_ids.append(task_id)
        return self.bootstrap_status

    async def start_voice_model_download(self, *, model_id: str | None = None, retry_task_id: str | None = None) -> dict:
        """记录纯文件下载委托，不触碰语音准备调用记录。"""

        self._raise_if_failed()
        if model_id is not None:
            self.downloaded_model_ids.append(model_id)
        if retry_task_id is not None:
            self.retried_download_ids.append(retry_task_id)
        return {'task_id': 'download-1', 'phase': 'queued'}

    async def initialize_bootstrap(
        self,
        *,
        qweather_api_key: str | None,
        qweather_api_host: str | None,
    ) -> dict[str, object]:
        """模拟首次初始化入口，允许测试应用层状态冲突映射。"""

        _ = qweather_api_key, qweather_api_host
        self._raise_if_failed()
        return self.bootstrap_status

    async def update_qweather_api_key(
        self,
        *,
        api_key: str,
        api_host: str,
    ) -> dict[str, object]:
        """模拟天气服务密钥入口，允许测试应用层状态冲突映射。"""

        _ = api_key, api_host
        self._raise_if_failed()
        return self.bootstrap_status


def _client() -> tuple[TestClient, FakeTtsSetupService, FakeVoiceCoordinator, Mock]:
    """构造只注册 Bootstrap 路由的测试客户端。

    Returns:
        FastAPI 客户端、语音服务替身、协调器替身和额外调用的记录。
    """

    tts_setup = FakeTtsSetupService()
    coordinator = FakeVoiceCoordinator()
    require = Mock()
    app = FastAPI()
    install_application_error_handlers(app)
    app.include_router(bootstrap.router)
    app.dependency_overrides[get_bootstrap_dependencies] = lambda: SimpleNamespace(
        hub=SimpleNamespace(bootstrap=SimpleNamespace()),
        runtime_reconfiguration=coordinator,
        tts_setup=tts_setup,
    )
    return TestClient(app), tts_setup, coordinator, require


def test_catalog_configuration_and_status_routes_delegate_to_tts_setup() -> None:
    """目录、执行配置和任务状态读取应直接返回语音服务的载荷。"""

    client, tts_setup, _, _ = _client()

    assert client.get("/api/bootstrap/tts/models").json() == tts_setup.catalog
    assert client.get("/api/bootstrap/tts/configuration").json() == tts_setup.configuration
    assert client.get("/api/bootstrap/tts/status").json() == tts_setup.status


def test_model_download_routes_are_separate_from_voice_setup() -> None:
    """纯下载的读取、启动、取消、重试均不能误走角色语音准备入口。"""

    client, tts_setup, coordinator, _ = _client()
    assert client.get('/api/bootstrap/tts/downloads').json() == tts_setup.download_status
    assert client.post('/api/bootstrap/tts/downloads', json={'model_id': 's2-pro-4b-gguf'}).status_code == 202
    assert client.post('/api/bootstrap/tts/downloads/download-1/cancel').status_code == 202
    assert client.post('/api/bootstrap/tts/downloads/download-1/retry').status_code == 202
    assert coordinator.downloaded_model_ids == ['s2-pro-4b-gguf']
    assert coordinator.retried_download_ids == ['download-1']
    assert tts_setup.cancelled_download_ids == ['download-1']
    assert not coordinator.started_model_ids
    assert not coordinator.retried_task_ids
    tts_setup.conflict = StateConflict('模型下载任务不存在或不可取消')
    assert client.post('/api/bootstrap/tts/downloads/stale/cancel').status_code == 409
    coordinator.error = ValueError('模型不在已固定版本的发行目录中')
    assert client.post('/api/bootstrap/tts/downloads', json={'model_id': 'unknown'}).status_code == 400


def test_probe_returns_structured_capabilities() -> None:
    """设备探测返回结构化执行能力。"""

    client, _, _, require = _client()

    response = client.post("/api/bootstrap/tts/probe")

    assert response.status_code == 200
    assert response.json() == {"backends": [], "recommendation": None}
    require.assert_not_called()


def test_put_configuration_passes_parsed_model_and_maps_value_error_to_400() -> None:
    """执行方式提交应传入解析后的配置模型；目录校验失败映射为 400。"""

    client, _, coordinator, _ = _client()
    body = {"selection": {"onnx_provider": "cpu", "llama_backend": "cpu"}, "model_directory": "G:/models"}

    response = client.put("/api/bootstrap/tts/configuration", json=body)

    assert response.status_code == 200
    assert coordinator.configured == [ExecutionConfiguration.model_validate(body)]

    coordinator.error = ValueError("请选择模型的绝对目录")
    error_response = client.put("/api/bootstrap/tts/configuration", json=body)

    assert error_response.status_code == 400
    assert error_response.json() == {"detail": "请选择模型的绝对目录"}


def test_put_configuration_rejects_unknown_field() -> None:
    """执行配置模型不允许合同外字段被静默忽略。"""

    client, _, coordinator, _ = _client()
    body = {
        "selection": {"onnx_provider": "cpu", "llama_backend": "cpu"},
        "model_directory": "G:/models",
        "unexpected_field": "unexpected-value",
    }

    response = client.put("/api/bootstrap/tts/configuration", json=body)

    assert response.status_code == 422
    assert coordinator.configured == []


def test_start_task_passes_model_id_and_maps_state_machine_errors() -> None:
    """启动任务透传模型编号；状态冲突映射 409，请求校验失败映射 400。"""

    client, _, coordinator, _ = _client()

    response = client.post("/api/bootstrap/tts/tasks", json={"model_id": "qwen3-tts-clone-0.6b-gguf"})

    assert response.status_code == 202
    assert coordinator.started_model_ids == ["qwen3-tts-clone-0.6b-gguf"]

    coordinator.error = StateConflict("语音配置任务仍在运行或释放资源，请稍后再试")
    conflict_response = client.post("/api/bootstrap/tts/tasks", json={"model_id": "model"})

    assert conflict_response.status_code == 409

    coordinator.error = ValueError("模型不在已固定版本的发行目录中")
    invalid_response = client.post("/api/bootstrap/tts/tasks", json={"model_id": "model"})

    assert invalid_response.status_code == 400

    # 协调器抛出的应用层请求错误不经路由捕获，由全局 handler 统一映射为 400。
    coordinator.error = InvalidRequest("必须提供模型或重试任务之一")
    application_error_response = client.post("/api/bootstrap/tts/tasks", json={"model_id": "model"})

    assert application_error_response.status_code == 400
    assert application_error_response.json() == {"detail": "必须提供模型或重试任务之一"}


def test_cancel_and_retry_pass_task_id_and_map_conflict_to_409() -> None:
    """取消直达语音服务，重试走协调器的重试入口；冲突统一映射 409。"""

    client, tts_setup, coordinator, _ = _client()

    cancel_response = client.post("/api/bootstrap/tts/tasks/task-1/cancel")
    retry_response = client.post("/api/bootstrap/tts/tasks/task-1/retry")

    assert cancel_response.status_code == 202
    assert retry_response.status_code == 202
    assert tts_setup.cancelled_task_ids == ["task-1"]
    assert coordinator.retried_task_ids == ["task-1"]
    assert coordinator.started_model_ids == []

    tts_setup.conflict = StateConflict("语音配置任务不存在或已过期")
    stale_cancel_response = client.post("/api/bootstrap/tts/tasks/task-9/cancel")

    assert stale_cancel_response.status_code == 409
    assert stale_cancel_response.json() == {"detail": "语音配置任务不存在或已过期"}

    coordinator.error = StateConflict("语音任务不可重试或已过期")
    stale_retry_response = client.post("/api/bootstrap/tts/tasks/task-9/retry")

    assert stale_retry_response.status_code == 409


def test_preview_returns_wav_bytes_and_maps_conflict_to_409() -> None:
    """试听返回 WAV 字节流；试听未就绪或任务过期映射 409。"""

    client, tts_setup, _, _ = _client()

    response = client.get("/api/bootstrap/tts/tasks/task-1/preview")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("audio/wav")
    assert response.content == tts_setup.preview
    assert tts_setup.preview_task_ids == ["task-1"]

    tts_setup.conflict = StateConflict("试听尚未准备完成或素材已经失效")
    conflict_response = client.get("/api/bootstrap/tts/tasks/task-1/preview")

    assert conflict_response.status_code == 409


def test_confirm_returns_full_bootstrap_status_payload_and_maps_conflict_to_409() -> None:
    """确认试听返回完整 Bootstrap 状态载荷；任务过期或冲突映射 409。"""

    client, _, coordinator, _ = _client()

    response = client.post("/api/bootstrap/tts/tasks/task-1/confirm")

    assert response.status_code == 202
    assert response.json() == coordinator.bootstrap_status
    assert set(response.json()) == {"llm", "voice", "has_qweather_api_key", "is_initialized"}
    assert coordinator.confirmed_task_ids == ["task-1"]

    coordinator.error = StateConflict("语音配置任务不存在或已过期")
    conflict_response = client.post("/api/bootstrap/tts/tasks/task-9/confirm")

    assert conflict_response.status_code == 409


def test_weather_configuration_conflict_is_mapped_to_http_409() -> None:
    """天气配置 CAS 冲突必须沿用应用错误统一映射为 409。"""

    client, _, coordinator, _ = _client()
    coordinator.error = StateConflict("和风天气配置已变化，请重新读取后保存")

    initialize_response = client.post(
        "/api/bootstrap/initialize",
        json={
            "qweather_api_key": "older-key",
            "qweather_api_host": "https://older.example.com",
        },
    )
    update_response = client.post(
        "/api/service-keys/qweather",
        json={
            "qweather_api_key": "older-key",
            "qweather_api_host": "https://older.example.com",
        },
    )

    assert initialize_response.status_code == 409
    assert update_response.status_code == 409
    assert initialize_response.json() == {
        "detail": "和风天气配置已变化，请重新读取后保存"
    }
    assert update_response.json() == {
        "detail": "和风天气配置已变化，请重新读取后保存"
    }


def test_weather_upstream_failure_is_mapped_to_http_503_without_losing_detail() -> None:
    """天气代理/TLS 故障应返回可重试的 503，而不是误报为 400。"""

    client, _, coordinator, _ = _client()
    coordinator.error = ServiceUnavailable(
        {
            "code": "qweather_tls_connection_failed",
            "message": "和风天气验证暂时失败，密钥尚未保存：天气服务 TLS 连接失败，请检查 API Host、系统代理和网络后重试。",
            "retryable": True,
        }
    )

    response = client.post(
        "/api/service-keys/qweather",
        json={
            "qweather_api_key": "weather-key",
            "qweather_api_host": "weather.example.com",
        },
    )

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "qweather_tls_connection_failed"
    assert response.json()["detail"]["retryable"] is True
