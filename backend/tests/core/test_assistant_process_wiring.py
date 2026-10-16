"""验证助手子进程组合根始终遵守依赖构造契约。"""

from ast import Attribute, Call, Expression, Name, parse, walk
from inspect import getsource, signature
from pathlib import Path
from textwrap import dedent
from types import SimpleNamespace

import httpx
from openai import OpenAI
import pytest

from companion.foreground_self_reflection.coordinator import (
    ForegroundSelfReflectionCoordinator,
)
from companion.llm.client_factory import LlmClientFactory
from companion.llm.config import LlmSettings
from companion.llm.model_registry import QWEN_COGNITIVE_REPAIR_INTEGRATION_ID
from companion.observability.prompt_audit import PromptAuditRecorder
from companion.realtime.assistant_child_runtime import AssistantChildRuntime
from companion.realtime.assistant_process import _build_assistant_child_runtime


def test_self_reflection_coordinator_wiring_matches_constructor_contract() -> None:
    """验证组合根传入的依赖与 Coordinator 当前构造签名一致。

    Returns:
        None: 构造参数存在遗漏或残留时，签名绑定会直接让测试失败。
    """

    syntax_tree = parse(dedent(getsource(_build_assistant_child_runtime)))
    coordinator_calls = [
        node
        for node in walk(syntax_tree)
        if isinstance(node, Call)
        and isinstance(node.func, Name)
        and node.func.id == "ForegroundSelfReflectionCoordinator"
    ]

    assert len(coordinator_calls) == 1
    coordinator_call = coordinator_calls[0]
    assert all(keyword.arg is not None for keyword in coordinator_call.keywords)

    wired_dependencies = {
        keyword.arg: object()
        for keyword in coordinator_call.keywords
        if keyword.arg is not None
    }
    signature(ForegroundSelfReflectionCoordinator).bind(**wired_dependencies)


def test_windows_application_tool_is_wired_with_system_prewarm() -> None:
    """生产组合根必须构造并预热真实 Windows 应用启动 Adapter。"""

    syntax_tree = parse(dedent(getsource(_build_assistant_child_runtime)))
    foreground_service_calls = [
        node
        for node in walk(syntax_tree)
        if isinstance(node, Call)
        and isinstance(node.func, Name)
        and node.func.id == "ForegroundToolService"
    ]

    assert len(foreground_service_calls) == 1
    application_tool_values = [
        keyword.value
        for keyword in foreground_service_calls[0].keywords
        if keyword.arg == "windows_application_tools"
    ]
    assert len(application_tool_values) == 1

    factory_call = application_tool_values[0]
    assert isinstance(factory_call, Call)
    assert isinstance(factory_call.func, Attribute)
    assert isinstance(factory_call.func.value, Name)
    assert factory_call.func.value.id == "WindowsApplicationForegroundTool"
    assert factory_call.func.attr == "from_system"
    assert factory_call.args == []
    assert factory_call.keywords == []


@pytest.mark.parametrize("timeout", [0.0, 1.0])
@pytest.mark.parametrize("fail_first_client", [False, True])
def test_shutdown_wiring_closes_all_llm_clients(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
    timeout: float,
    fail_first_client: bool,
) -> None:
    """生产关闭回调必须释放全部模型连接，且一个关闭失败不阻断其余客户端。

    Args:
        monkeypatch: 替换网络传输和单调时钟的测试工具。
        caplog: 收集关闭失败日志的测试工具。
        tmp_path: 测试专用的临时目录。
        timeout: 子运行时可用于关闭的总秒数。
        fail_first_client: 是否让第一个客户端的传输关闭抛出异常。

    Returns:
        无返回值；直接检查连接状态、关闭顺序和错误日志。
    """

    observed: list[str] = []
    http_clients: list[httpx.Client] = []

    def create_sdk_client(**kwargs: object) -> OpenAI:
        """保留真实 SDK 和 HTTP 客户端，只替换不应访问外网的传输。

        Args:
            **kwargs: 生产模型客户端传给 SDK 的连接配置。

        Returns:
            使用隔离传输的真实 SDK 客户端。
        """

        client_index = len(http_clients)
        transport = httpx.MockTransport(lambda _request: httpx.Response(200))

        def close_transport() -> None:
            """记录连接释放；按用例模拟首个传输关闭失败。

            Returns:
                无返回值；模拟故障时抛出异常。
            """

            observed.append(f"client_{client_index}")
            if fail_first_client and client_index == 0:
                raise RuntimeError("transport close failed")

        monkeypatch.setattr(transport, "close", close_transport)
        http_client = httpx.Client(transport=transport)
        http_clients.append(http_client)
        return OpenAI(http_client=http_client, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("companion.llm.chat_client.OpenAI", create_sdk_client)
    monkeypatch.setattr(
        "companion.realtime.assistant_child_runtime.monotonic", lambda: 10.0
    )
    factory = LlmClientFactory(
        settings=LlmSettings.model_validate(
            {
                "schema_version": 2,
                "credentials": {
                    provider: {
                        "base_url": "https://llm.example/v1", "api_key": "test-key"
                    }
                    for provider in ("deepseek", "qwen")
                },
            }
        ),
        prompt_audit_recorder=PromptAuditRecorder(
            enabled=False,
            output_dir=tmp_path,
            max_runs=1,
            process_role="test",
        ),
    )
    dependencies: dict[str, object] = {
        "foreground_role_client": factory.client("foreground"),
        "cognitive_client": factory.client("cognitive"),
        "cognitive_repair_client": factory.integration_client(
            QWEN_COGNITIVE_REPAIR_INTEGRATION_ID, purpose="cognitive_result_repair"
        ),
        "summary_client": factory.client("summary"),
    }
    worker_names = (
        "foreground_turn_processor",
        "cognitive_activity_executor",
        "background_agent_executor",
        "shutdown_cua_after_background",
        "activity_progress_scheduler",
        "runtime_memory_compaction_worker",
    )
    for name in worker_names:
        callback = lambda _timeout, name=name: observed.append(name)
        dependencies[name] = (
            callback
            if name == "shutdown_cua_after_background"
            else SimpleNamespace(shutdown=callback)
        )

    # 直接执行生产装配里的回调表达式，避免测试自己另写一份正确签名而漏掉故障。
    syntax_tree = parse(dedent(getsource(_build_assistant_child_runtime)))
    runtime_calls = [
        node
        for node in walk(syntax_tree)
        if isinstance(node, Call)
        and isinstance(node.func, Name)
        and node.func.id == "AssistantChildRuntime"
    ]
    assert len(runtime_calls) == 1
    callback_expression = next(
        keyword.value
        for keyword in runtime_calls[0].keywords
        if keyword.arg == "shutdown_callbacks"
    )
    callbacks = eval(
        compile(Expression(callback_expression), "<shutdown-wiring>", "eval"),
        dependencies,
    )
    runtime = AssistantChildRuntime(
        runner=SimpleNamespace(  # type: ignore[arg-type]
            shutdown=lambda timeout: observed.append("runner")
        ),
        shutdown_callbacks=callbacks,
    )

    try:
        runtime.shutdown(timeout=timeout)

        assert observed == [
            "runner", *worker_names, "client_0", "client_1", "client_2", "client_3"
        ]
        assert all(client.is_closed for client in http_clients)
        assert caplog.text.count("助手附属运行时关闭失败") == int(fail_first_client)
        assert "TypeError" not in caplog.text
        if fail_first_client:
            assert "transport close failed" in caplog.text
    finally:
        fail_first_client = False
        for client in http_clients:
            if not client.is_closed:
                client.close()
