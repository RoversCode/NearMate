"""验证 Windows 应用 Foreground Tool 的公开合同。"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Sequence

import pytest

from companion.capabilities.windows_applications.models import (
    ApplicationLaunchResult,
    ApplicationRuntimeSnapshot,
    InstalledApplication,
)
from companion.capabilities.windows_applications.service import (
    WindowsApplicationLaunchService,
)
from companion.capabilities.windows_applications.windows_gateway import (
    WindowsApplicationGateway,
)
from companion.foreground_tools.catalog.service import ForegroundToolService
from companion.foreground_tools.external.windows_application_tool import (
    OPEN_INSTALLED_WINDOWS_APP_TOOL,
    WindowsApplicationForegroundTool,
)
from companion.foreground_tools.inline_execution import InlineExecutionContext
from companion.foreground_tools.orchestration import (
    ForegroundToolCallOrchestrator,
    InlineToolCompleted,
)
from companion.llm.foreground_models import ForegroundToolCall


class FakeApplicationLaunchService:
    """记录 Adapter 归一化后的应用启动参数。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def open_application(
        self,
        *,
        application_name: str,
        launch_mode: str,
        side_effect_deadline: float | None = None,
        cancellation_requested: Callable[[], bool] | None = None,
    ) -> ApplicationLaunchResult:
        """返回稳定的同步完成事实。"""

        del side_effect_deadline, cancellation_requested
        self.calls.append((application_name, launch_mode))
        return ApplicationLaunchResult.completed(
            outcome="launch_requested",
            application_name=application_name,
        )


def test_tool_schema_accepts_one_application_and_optional_launch_mode() -> None:
    """公开 Schema 只允许单应用名称和两种启动模式。"""

    tool = WindowsApplicationForegroundTool(service=None)
    schema = tool.tool_schemas()[0]["function"]
    parameters = schema["parameters"]

    assert schema["name"] == OPEN_INSTALLED_WINDOWS_APP_TOOL
    assert parameters["required"] == ["application_name"]
    assert set(parameters["properties"]) == {"application_name", "launch_mode"}
    assert parameters["properties"]["launch_mode"]["enum"] == [
        "reuse_existing",
        "new_instance",
    ]
    assert parameters["additionalProperties"] is False


def test_tool_defaults_to_reuse_existing() -> None:
    """普通“打开应用”不传模式时必须默认复用现有窗口。"""

    launch_service = FakeApplicationLaunchService()
    tool = WindowsApplicationForegroundTool(service=launch_service)  # type: ignore[arg-type]

    payload = tool.execute(
        OPEN_INSTALLED_WINDOWS_APP_TOOL,
        {"application_name": "QQ"},
    )

    assert payload == {
        "state": "completed",
        "outcome": "launch_requested",
        "application_name": "QQ",
    }
    assert launch_service.calls == [("QQ", "reuse_existing")]


def test_foreground_catalog_always_exposes_windows_application_tool() -> None:
    """后台配置和真实 Adapter 缺失都不能让稳定 Tool Schema 消失。"""

    service = ForegroundToolService()
    schema_names = {
        schema["function"]["name"]
        for schema in service.tool_schemas()
    }
    metadata = service.tool_metadata(OPEN_INSTALLED_WINDOWS_APP_TOOL)

    assert OPEN_INSTALLED_WINDOWS_APP_TOOL in schema_names
    assert metadata.self_reflection_safe is False
    assert service.execution_timeout_seconds(OPEN_INSTALLED_WINDOWS_APP_TOOL) == 4.0


def test_self_reflection_does_not_receive_application_launch_tool() -> None:
    """Self-Reflection 的静态安全工具集合不得包含应用启动副作用。"""

    tool_names = {
        item.schema["function"]["name"]
        for item in ForegroundToolService().self_reflection_tools()
    }

    assert OPEN_INSTALLED_WINDOWS_APP_TOOL not in tool_names


def test_unavailable_adapter_returns_safe_failure_without_local_identity() -> None:
    """目录展示用空 Adapter 被误执行时只能返回安全失败。"""

    payload = WindowsApplicationForegroundTool(service=None).execute(
        OPEN_INSTALLED_WINDOWS_APP_TOOL,
        {"application_name": "QQ"},
    )

    assert payload == {
        "state": "failed",
        "reason": "windows_application_launcher_unavailable",
        "safe_message": "Windows 应用启动能力当前不可用。",
    }
    assert "path" not in payload
    assert "pid" not in payload


def test_non_windows_adapter_returns_stable_platform_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """非 Windows 生产 Adapter 应保留 Schema，并如实返回平台不支持。

    Args:
        monkeypatch: 临时替换当前平台能力判断的测试夹具。
    """

    monkeypatch.setattr(
        WindowsApplicationGateway,
        "is_supported",
        property(lambda _self: False),
    )
    tool = WindowsApplicationForegroundTool.from_system(prewarm=False)

    payload = tool.execute(
        OPEN_INSTALLED_WINDOWS_APP_TOOL,
        {"application_name": "QQ"},
    )

    assert payload == {
        "state": "failed",
        "reason": "unsupported_platform",
        "safe_message": "当前系统不支持 Windows 应用同步启动。",
    }


def test_multiple_application_calls_run_concurrently_and_keep_model_order() -> None:
    """多个纯应用必须分别并发执行，Tool 结果仍按模型调用顺序回写。"""

    barrier = threading.Barrier(2, timeout=1.0)

    class ConcurrentLaunchService(FakeApplicationLaunchService):
        """等待两项应用启动同时进入服务。"""

        def open_application(
            self,
            *,
            application_name: str,
            launch_mode: str,
            side_effect_deadline: float | None = None,
            cancellation_requested: Callable[[], bool] | None = None,
        ) -> ApplicationLaunchResult:
            """同步两条工作线程后返回各自应用事实。"""

            del side_effect_deadline, cancellation_requested
            self.calls.append((application_name, launch_mode))
            barrier.wait()
            return ApplicationLaunchResult.completed(
                outcome="launch_requested",
                application_name=application_name,
            )

    application_service = ConcurrentLaunchService()
    foreground_tools = ForegroundToolService(
        windows_application_tools=WindowsApplicationForegroundTool(
            service=application_service,  # type: ignore[arg-type]
        )
    )
    orchestrator = ForegroundToolCallOrchestrator(max_inline_workers=2)
    try:
        execution = orchestrator.prepare(
            tool_calls=[
                ForegroundToolCall(
                    tool_call_id="open-qq",
                    name=OPEN_INSTALLED_WINDOWS_APP_TOOL,
                    arguments={"application_name": "QQ"},
                ),
                ForegroundToolCall(
                    tool_call_id="open-music",
                    name=OPEN_INSTALLED_WINDOWS_APP_TOOL,
                    arguments={"application_name": "网易云音乐"},
                ),
            ],
            is_cognitive_tool=lambda _name: False,
            is_task_result_tool=lambda _name: False,
            execute_cognitive_calls=lambda _calls: (),
            execute_task_result_call=lambda _call: {},
            execute_inline_call=lambda call, context: foreground_tools.execute(
                call.name,
                call.arguments,
                inline_execution_context=context,
            ),
            inline_timeout_resolver=foreground_tools.execution_timeout_seconds,
        ).collect()
    finally:
        orchestrator.shutdown()

    assert all(isinstance(call.outcome, InlineToolCompleted) for call in execution.calls)
    assert [call.result_payload()["application_name"] for call in execution.calls] == [
        "QQ",
        "网易云音乐",
    ]


def test_queued_application_call_cannot_launch_after_side_effect_deadline() -> None:
    """Inline 排队吃完副作用预算后，迟到工作线程不得读取目录或启动应用。"""

    application = InstalledApplication(
        display_name="QQ",
        application_id="QQ",
        launch_target="shell:AppsFolder\\QQ",
    )

    class RecordingGateway:
        """记录所有可能触达 Windows 的调用，证明超时门禁先于这些动作。"""

        is_supported = True

        def __init__(self) -> None:
            self.list_calls = 0
            self.focus_calls: list[int] = []
            self.launch_calls: list[InstalledApplication] = []

        def list_installed_applications(self) -> tuple[InstalledApplication, ...]:
            """返回唯一应用并记录目录访问。"""

            self.list_calls += 1
            return (application,)

        def resolve_default_browser(
            self,
            applications: Sequence[InstalledApplication],
        ) -> InstalledApplication | None:
            """本测试不解析默认浏览器。"""

            del applications
            return None

        def snapshot(
            self,
            selected_application: InstalledApplication,
        ) -> ApplicationRuntimeSnapshot:
            """返回没有现有实例的空快照。"""

            del selected_application
            return ApplicationRuntimeSnapshot()

        def focus_window(self, window_handle: int) -> bool:
            """记录不应发生的窗口置前。"""

            self.focus_calls.append(window_handle)
            return True

        def launch(self, selected_application: InstalledApplication) -> bool:
            """记录不应发生的 Shell 启动。"""

            self.launch_calls.append(selected_application)
            return True

    gateway = RecordingGateway()
    foreground_tools = ForegroundToolService(
        windows_application_tools=WindowsApplicationForegroundTool(
            service=WindowsApplicationLaunchService(gateway=gateway),
        )
    )
    orchestrator = ForegroundToolCallOrchestrator(max_inline_workers=1)

    def execute_inline(
        call: ForegroundToolCall,
        context: InlineExecutionContext,
    ) -> dict[str, object]:
        """先占住唯一工作线程，再让应用调用带着原始截止时间迟到执行。"""

        if call.name == "blocking_inline":
            time.sleep(0.15)
            return {"state": "completed"}
        return foreground_tools.execute(
            call.name,
            call.arguments,
            inline_execution_context=context,
        )

    try:
        execution = orchestrator.prepare(
            tool_calls=[
                ForegroundToolCall(
                    tool_call_id="blocking-call",
                    name="blocking_inline",
                    arguments={},
                ),
                ForegroundToolCall(
                    tool_call_id="open-qq",
                    name=OPEN_INSTALLED_WINDOWS_APP_TOOL,
                    arguments={"application_name": "QQ"},
                ),
            ],
            is_cognitive_tool=lambda _name: False,
            is_task_result_tool=lambda _name: False,
            execute_cognitive_calls=lambda _calls: (),
            execute_task_result_call=lambda _call: {},
            execute_inline_call=execute_inline,
            inline_timeout_resolver=lambda _name: 0.3,
        ).collect()
    finally:
        orchestrator.shutdown()

    assert execution.calls[1].result_payload()["reason"] == (
        "application_launch_timeout"
    )
    assert gateway.list_calls == 0
    assert gateway.focus_calls == []
    assert gateway.launch_calls == []
