from __future__ import annotations

import asyncio
import json
import io
import sys
from dataclasses import replace
from pathlib import Path
from threading import Event, Thread

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, SystemMessage, UserMessage
from claude_agent_sdk.types import (
    ServerToolResultBlock,
    ServerToolUseBlock,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)

from companion.background_agent.contracts import AgentEvent, AgentMaterial, AgentTask
from companion.background_agent.cua_capability import CuaCapabilitySlot
from companion.background_agent.cua_integration import (
    CUA_MCP_SERVER_NAME,
    CUA_SKILL_ID,
)
from companion.background_agent.model_config import AgentExecutionConfig, AgentModelConfig
from companion.background_agent.runner import AgentTaskRunner
from companion.background_agent.skill_registry import SkillExecutionSnapshot
from companion.background_agent.workspace import WorkspaceManager
from companion.background_agent.work_plan_mirror import AgentWorkPlanMirror


class SuccessClient:
    async def connect(self) -> None:
        return None

    async def query(self, _prompt: str) -> None:
        return None

    async def receive_response(self):
        yield SystemMessage(subtype="init", data={"session_id": "session-sdk"})
        yield AssistantMessage(
            content=[TextBlock(text="任务完成")],
            model="test-model",
        )
        yield _result_message(subtype="success", result="不应读取的自由文本")

    async def interrupt(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None


class TerminalDiagnosticsClient(SuccessClient):
    """在正常消息流之外补一行 CLI stderr，供终端诊断测试观察。"""

    def __init__(self, options: object) -> None:
        """保存 Runner 创建的 SDK options。

        Args:
            options: Runner 为当前任务构造的 Claude SDK 配置。
        """

        self.options = options

    async def connect(self) -> None:
        """模拟 Claude CLI 在连接时输出一行标准错误。"""

        stderr_callback = getattr(self.options, "stderr", None)
        assert callable(stderr_callback)
        stderr_callback("cli-stderr-sentinel\n")

    async def receive_response(self):
        """返回同时包含可读正文和应过滤元数据的完整消息流。

        Yields:
            SDK 初始化、思考、正文、工具调用、工具结果和成功终态。
        """

        yield SystemMessage(subtype="init", data={"session_id": "session-sdk"})
        yield SystemMessage(
            subtype="thinking_tokens",
            data={
                "thinking_tokens": 17,
                "uuid": "hidden-system-uuid",
                "session_id": "session-sdk",
            },
        )
        yield AssistantMessage(
            content=[
                ThinkingBlock(
                    thinking="我需要先检查页面",
                    signature="hidden-thinking-signature",
                ),
                TextBlock(text="已确认页面"),
                ToolUseBlock(
                    id="hidden-tool-use-id",
                    name="Read",
                    input={"file_path": "资料.txt"},
                ),
                ToolUseBlock(
                    id="hidden-empty-tool-use-id",
                    name="Glob",
                    input={},
                ),
                ServerToolUseBlock(
                    id="hidden-server-tool-use-id",
                    name="web_search",
                    input={"query": "NearMate"},
                ),
            ],
            model="hidden-model-name",
            usage={"input_tokens": 23, "output_tokens": 41},
            message_id="hidden-message-id",
            session_id="session-sdk",
            uuid="hidden-assistant-uuid",
        )
        yield UserMessage(
            content=[
                ToolResultBlock(
                    tool_use_id="hidden-tool-use-id",
                    content=[
                        {"type": "text", "text": "已读取资料"},
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "data": "hidden-image-base64",
                            },
                        },
                    ],
                    is_error=False,
                ),
                ServerToolResultBlock(
                    tool_use_id="hidden-server-tool-use-id",
                    content={"type": "text", "text": "已完成网页搜索"},
                ),
            ],
            uuid="hidden-user-uuid",
        )
        yield _result_message(
            subtype="success",
            result="最终结果文字",
        )


class TerminalDiagnosticsFailureClient(TerminalDiagnosticsClient):
    """返回带具体失败原因的 SDK 终态。"""

    async def receive_response(self):
        """返回初始化消息和失败终态。

        Yields:
            SDK 初始化和带稳定技术原因的失败终态。
        """

        yield SystemMessage(subtype="init", data={"session_id": "session-sdk"})
        yield ResultMessage(
            subtype="error_during_execution",
            duration_ms=1,
            duration_api_ms=1,
            is_error=True,
            num_turns=1,
            session_id="session-sdk",
            usage={"output_tokens": 99},
            errors=["browser_connection_lost"],
            terminal_reason="tool_error",
            uuid="hidden-result-uuid",
        )


class UnencodableTerminalDiagnosticsClient(TerminalDiagnosticsClient):
    """返回 GBK 无法编码的窗口标题，复现真实 Edge 诊断故障。"""

    async def receive_response(self):
        """返回包含零宽字符的工具结果和正常成功终态。

        Yields:
            SDK 初始化、含零宽字符的工具结果和成功终态。
        """

        yield SystemMessage(subtype="init", data={"session_id": "session-sdk"})
        yield UserMessage(
            content=[
                ToolResultBlock(
                    tool_use_id="edge-window-result",
                    content="about:blank - 用户配置 1 - Microsoft\u200b Edge",
                    is_error=False,
                )
            ]
        )
        yield _result_message(subtype="success", result="终端编码不应影响任务")


class ResponseStreamFailureClient(SuccessClient):
    """在 SDK 已初始化后让响应流抛错，验证 Runner 的异常终态。"""

    async def receive_response(self):
        """先建立可信会话身份，再模拟响应流异常。

        Yields:
            仅返回 SDK 初始化消息，随后抛出固定异常。

        Raises:
            RuntimeError: 模拟已连接 transport 的响应流故障。
        """

        yield SystemMessage(subtype="init", data={"session_id": "session-sdk"})
        raise RuntimeError("测试响应流异常")


class RecordingCuaIntegration:
    """提供已准备好的 CUA 连接，并记录 Runner 是否错误地重启 Host。"""

    def __init__(self) -> None:
        """初始化零次 Host 启动和固定的 stdio 配置。"""

        self.start_calls = 0
        self.ended_browser_activity_ids: list[str] = []
        self.mcp_server_name = CUA_MCP_SERVER_NAME
        self.browser_socket_path = r"\\.\pipe\nearmate-runner-test-browser"

    def browser_cli_session_name(self, activity_id: str) -> str:
        """返回测试可预测的 Activity Browser CLI Session。

        Args:
            activity_id: 当前后台任务编号。

        Returns:
            测试可直接断言的 session 名。
        """

        return f"browser-{activity_id}"

    def transport_instructions(
        self,
        activity_id: str,
        *,
        browser_guide_path: Path,
    ) -> str:
        """返回测试使用的 Computer MCP 与 Browser CLI 声明。

        Args:
            activity_id: 当前后台任务编号。
            browser_guide_path: 当前 Activity 私有官方 Browser 指南路径。

        Returns:
            追加到系统提示词的测试声明。
        """

        return (
            "Computer 使用 mcp__cua_driver__*；Browser 使用 cua-driver call；"
            f"session={self.browser_cli_session_name(activity_id)}；"
            f"socket={self.browser_socket_path}；BROWSER.md={browser_guide_path}；"
            "Chrome/Edge nearmate-default"
        )

    async def start(self, *, cancel_event: Event | None = None) -> None:
        """记录错误越过 lane 启动边界的 Host 启动调用。

        Args:
            cancel_event: 兼容生产异步启动签名的取消事件。

        Returns:
            无返回值；调用次数保存在实例中。
        """

        del cancel_event
        self.start_calls += 1

    def mcp_server_config(self) -> dict[str, object]:
        """返回无需启动真实 Driver 的 stdio 测试配置。

        Returns:
            Claude Agent SDK 可接收的固定配置。
        """

        return {
            "type": "stdio",
            "command": "cua-driver",
            "args": ["mcp"],
        }

    def end_browser_session(self, activity_id: str) -> None:
        """记录 Runner 已在 finally 收口 Browser CLI session。

        Args:
            activity_id: 当前结束的后台任务编号。

        Returns:
            无返回值；调用记录保存在实例中。
        """

        self.ended_browser_activity_ids.append(activity_id)


class TurnTransportClient(SuccessClient):
    """记录一个 Activity Turn 独占 transport 的 query 与连接生命周期。"""

    def __init__(self, session_id: str = "session-sdk-reused") -> None:
        self.prompts: list[str] = []
        self.connect_count = 0
        self.disconnect_count = 0
        self.session_id = session_id

    async def connect(self) -> None:
        """记录 transport 只建立一次。"""

        self.connect_count += 1

    async def query(self, prompt: str) -> None:
        """保存每个 Activity 发来的完整任务 prompt。"""

        self.prompts.append(prompt)

    async def receive_response(self):
        """为每轮 query 返回独立成功终态。"""

        yield SystemMessage(
            subtype="init",
            data={"session_id": self.session_id},
        )
        yield _result_message(subtype="success", session_id=self.session_id)

    async def disconnect(self) -> None:
        """记录 Runner 关闭时统一释放 transport。"""

        self.disconnect_count += 1


class RepeatedSystemMessageClient(SuccessClient):
    """模拟 SDK 在同一任务里连续返回多条 System 消息。"""

    def __init__(self, result_session_id: str) -> None:
        """保存终态消息要返回的会话编号。

        Args:
            result_session_id: ResultMessage 携带的 SDK 会话编号。

        Returns:
            无。
        """

        self._result_session_id = result_session_id

    async def receive_response(self):
        """返回重复会话编号，并用缺少编号的消息结束 System 消息序列。

        Yields:
            SDK 初始化、状态和成功终态消息。
        """

        yield SystemMessage(subtype="init", data={"session_id": "session-from-init"})
        yield SystemMessage(subtype="status", data={"session_id": "session-from-init"})
        yield SystemMessage(subtype="status", data={})
        yield _result_message(
            subtype="success",
            result="不应读取的自由文本",
            session_id=self._result_session_id,
        )


class InterruptibleClient:
    def __init__(self) -> None:
        self.interrupted = asyncio.Event()

    async def connect(self) -> None:
        return None

    async def query(self, _prompt: str) -> None:
        return None

    async def receive_response(self):
        yield SystemMessage(subtype="init", data={"session_id": "session-sdk"})
        await self.interrupted.wait()
        yield _result_message(subtype="error_during_execution")

    async def interrupt(self) -> None:
        self.interrupted.set()

    async def disconnect(self) -> None:
        return None


class CancellationRaceSuccessClient(SuccessClient):
    """让 interrupt 已被接纳后仍返回成功 Result，稳定复现终态竞态。"""

    def __init__(self) -> None:
        """初始化响应流、结果放行和 interrupt 到达信号。"""

        self.completion_allowed = Event()
        self.interrupted = Event()
        self.response_started = Event()

    async def receive_response(self):
        """等待测试放行后返回一条迟到的成功终态。

        Yields:
            取消请求之后到达的成功 Result。
        """

        # 竞态测试必须先确认 Runner 已进入真实响应流；只等 started 或 connect
        # 仍可能落在 query 前取消分支，测不到“取消后迟到成功”的目标路径。
        self.response_started.set()
        yield SystemMessage(subtype="init", data={"session_id": "session-sdk"})
        while not self.completion_allowed.is_set():
            await asyncio.sleep(0.01)
        yield _result_message(subtype="success")

    async def interrupt(self) -> None:
        """记录 SDK interrupt 已经真正进入事件循环。"""

        self.interrupted.set()


class EmptyResponseClient(SuccessClient):
    async def receive_response(self):
        if False:
            yield None


class DuplicateResultClient(SuccessClient):
    async def receive_response(self):
        yield SystemMessage(subtype="init", data={"session_id": "session-sdk"})
        yield _result_message(subtype="success", result="第一次结果")
        yield _result_message(subtype="error_during_execution")


class MissingStructuredOutputClient(SuccessClient):
    async def receive_response(self):
        """返回表面成功但缺少结构化结果的非法 Provider 终态。"""

        yield SystemMessage(subtype="init", data={"session_id": "session-sdk"})
        yield _result_message(
            subtype="success",
            result="不能作为兼容回退的自由文本",
            structured_output=None,
        )


class WorkPlanClient(SuccessClient):
    async def receive_response(self):
        """返回一组真实 TaskCreate/TaskUpdate 消息和最终结果。"""

        yield SystemMessage(subtype="init", data={"session_id": "session-sdk"})
        yield AssistantMessage(
            content=[
                ToolUseBlock(
                    id="create-a",
                    name="TaskCreate",
                    input={
                        "subject": "理解参考项目",
                        "description": "读取参考实现",
                        "activeForm": "正在理解参考项目",
                    },
                )
            ],
            model="test-model",
        )
        yield UserMessage(
            content=[
                ToolResultBlock(
                    tool_use_id="create-a",
                    content="Task #1 created successfully: 理解参考项目",
                    is_error=False,
                )
            ],
            tool_use_result={"taskId": "1"},
        )
        yield AssistantMessage(
            content=[
                ToolUseBlock(
                    id="update-a",
                    name="TaskUpdate",
                    input={"taskId": "1", "status": "in_progress"},
                )
            ],
            model="test-model",
        )
        yield UserMessage(
            content=[
                ToolResultBlock(
                    tool_use_id="update-a",
                    content="Task updated",
                    is_error=False,
                )
            ],
            tool_use_result={},
        )
        yield _result_message(subtype="success")


class ConnectFailureClient(SuccessClient):
    async def connect(self) -> None:
        raise RuntimeError("Claude Code CLI exited before initialize")


class SlowConnectClient(InterruptibleClient):
    def __init__(self) -> None:
        super().__init__()
        self.connect_started = Event()
        self.release_connect = Event()

    async def connect(self) -> None:
        self.connect_started.set()
        while not self.release_connect.is_set():
            await asyncio.sleep(0.01)


class StubbornShutdownClient(SuccessClient):
    """模拟已经产生部分用量、但不会响应 SDK interrupt 的运行中任务。"""

    def __init__(self) -> None:
        self.waiting_after_usage = Event()
        self.release_blocking_receive = Event()

    async def receive_response(self):
        """先返回一条用量消息，再永久等待进程关闭。"""

        yield SystemMessage(subtype="init", data={"session_id": "session-sdk"})
        yield AssistantMessage(
            content=[],
            model="test-model",
            message_id="message-before-shutdown",
            usage={
                "input_tokens": 11,
                "cache_creation_input_tokens": 2,
                "cache_read_input_tokens": 3,
                "output_tokens": 5,
            },
        )
        self.waiting_after_usage.set()
        # 故意阻塞 SDK 事件循环线程，模拟第三方调用没有及时把控制权还给 asyncio。
        self.release_blocking_receive.wait(timeout=2.0)

    async def interrupt(self) -> None:
        """故意不解除等待，复现 SDK interrupt 超过关闭预算。"""


class DisconnectReleasedResponseClient(SuccessClient):
    """模拟只有 transport 断开后，响应流才能完成自身清理的 SDK Client。"""

    def __init__(self) -> None:
        """创建可观察的响应等待和 transport 断开信号。"""

        self.response_waiting = Event()
        self.disconnected = Event()

    async def receive_response(self):
        """返回初始化消息，然后一直等待 transport 被宿主断开。

        Yields:
            一条足以让 Runner 进入 processing 的 SDK 初始化消息。
        """

        yield SystemMessage(subtype="init", data={"session_id": "session-sdk"})
        self.response_waiting.set()
        try:
            await asyncio.Future()
        finally:
            # 部分 SDK 的流关闭逻辑需要底层 transport 先结束。这个等待用于稳定
            # 复现“先等任务 cleanup、后 disconnect”造成的关闭顺序死锁。
            while not self.disconnected.is_set():
                await asyncio.sleep(0.01)

    async def disconnect(self) -> None:
        """记录宿主已经主动断开 transport，允许响应流的 finally 结束。"""

        self.disconnected.set()

    async def interrupt(self) -> None:
        """模拟 Provider 流卡住时，SDK interrupt 本身也无法返回。"""

        await asyncio.Future()


class QuestionCancellationClient(SuccessClient):
    def __init__(self, options) -> None:
        self.options = options

    async def receive_response(self):
        yield SystemMessage(subtype="init", data={"session_id": "session-sdk"})
        assert self.options.can_use_tool is not None
        await self.options.can_use_tool(
            "AskUserQuestion",
            {
                "questions": [
                    {
                        "question": "还要继续吗？",
                        "header": "继续",
                        "options": [],
                        "multiSelect": False,
                    }
                ]
            },
            None,
        )
        yield _result_message(subtype="error_during_execution")


class SupplementalMaterialQuestionClient(SuccessClient):
    """在资料问题恢复后立刻验证新增材料是否已经获得读取权限。"""

    def __init__(self, options, supplemental_path: Path) -> None:
        self.options = options
        self.supplemental_path = supplemental_path
        self.hook_result: dict[str, object] | None = None
        self.updated_question_input: dict[str, object] | None = None

    async def receive_response(self):
        """等待回答，随后立即调用文件护栏验证恢复前授权顺序。

        Yields:
            成功终态。
        """

        yield SystemMessage(subtype="init", data={"session_id": "session-sdk"})
        assert self.options.can_use_tool is not None
        permission_result = await self.options.can_use_tool(
            "AskUserQuestion",
            {
                "questions": [
                    {
                        "question": "请提供补充文件。",
                        "header": "补充文件",
                        "options": [],
                        "multiSelect": False,
                    }
                ]
            },
            None,
        )
        self.updated_question_input = permission_result.updated_input
        hook = self.options.hooks["PreToolUse"][0].hooks[0]
        self.hook_result = await hook(
            {
                "tool_name": "Read",
                "tool_input": {"file_path": str(self.supplemental_path)},
            },
            None,
            None,
        )
        yield _result_message(subtype="success")


class ActivityCancellationDuringQuestionClient(SuccessClient):
    """模拟 CLI interrupt 直接取消正在等待权限回调的行为。"""

    def __init__(self, options) -> None:
        self.options = options
        self.question_task: asyncio.Task[object] | None = None
        self.question_resumed = Event()

    async def receive_response(self):
        """挂起资料问题，直到 Activity 取消触发 interrupt。

        Yields:
            CLI 中断对应的错误终态。
        """

        yield SystemMessage(subtype="init", data={"session_id": "session-sdk"})
        assert self.options.can_use_tool is not None
        self.question_task = asyncio.create_task(
            self.options.can_use_tool(
                "AskUserQuestion",
                {
                    "questions": [
                        {
                            "question": "还要继续吗？",
                            "header": "继续",
                            "options": [],
                            "multiSelect": False,
                        }
                    ]
                },
                None,
            )
        )
        try:
            await self.question_task
        except asyncio.CancelledError:
            pass
        self.question_resumed.set()
        yield _result_message(subtype="error_during_execution")

    async def interrupt(self) -> None:
        """取消 SDK 当前的权限回调。

        Returns:
            无。
        """

        if self.question_task is not None:
            self.question_task.cancel()


def test_agent_task_runner_maps_success_result(monkeypatch, tmp_path: Path) -> None:
    """成功任务只发布产品需要的事件，并保留终态会话编号。

    Args:
        monkeypatch: 用于替换真实 SDK Client 的 pytest 工具。
        tmp_path: pytest 为当前用例创建的临时目录。

    Returns:
        无。
    """

    client = SuccessClient()
    monkeypatch.setattr(
        "companion.background_agent.runner.ClaudeSDKClient",
        lambda _options: client,
    )
    events: list[AgentEvent] = []
    terminal = Event()
    runner = _runner(tmp_path, events, terminal)
    task = _task(tmp_path)

    assert runner.start(task, _execution_config(tmp_path)) is True
    assert terminal.wait(2.0) is True

    completed = next(event for event in events if event.event_type == "completed")
    assert completed.payload["display_content"] == "任务完成"
    assert completed.payload["delivery_brief"] == "任务已经完成并生成了可查看的结果。"
    assert "result_text" not in completed.payload
    assert completed.payload["session_id"] == "session-sdk"
    assert completed.payload["internal_usage"] == {
        "is_complete": False,
        "reference_cost_usd": None,
        "by_model": [],
    }
    assert all(event.event_type != "session_started" for event in events)
    started_index = next(
        index
        for index, event in enumerate(events)
        if event.event_type == "started"
    )
    processing_index = next(
        index
        for index, event in enumerate(events)
        if event.event_type == "stage_changed"
        and event.payload.get("stage") == "processing"
    )
    delivering_result_index = next(
        index
        for index, event in enumerate(events)
        if event.event_type == "stage_changed"
        and event.payload.get("stage") == "delivering_result"
    )
    completed_index = events.index(completed)
    assert started_index < processing_index < delivering_result_index < completed_index
    assert [
        event.payload["content_text"]
        for event in events
        if event.event_type == "turn_output"
    ] == ["任务完成"]
    runner.shutdown()


@pytest.mark.parametrize(
    ("terminal_diagnostics_enabled", "should_be_visible"),
    [(True, True), (False, False)],
)
def test_agent_task_runner_prints_readable_sdk_text_only_when_enabled(
    monkeypatch,
    tmp_path: Path,
    capsys,
    terminal_diagnostics_enabled: bool,
    should_be_visible: bool,
) -> None:
    """开发开关只显示 SDK 可读内容和 Claude CLI stderr。"""

    monkeypatch.setattr(
        "companion.background_agent.runner.ClaudeSDKClient",
        lambda options: TerminalDiagnosticsClient(options),
    )
    events: list[AgentEvent] = []
    terminal = Event()
    runner = _runner(
        tmp_path,
        events,
        terminal,
        terminal_diagnostics_enabled=terminal_diagnostics_enabled,
    )
    task = _task(tmp_path)

    try:
        assert runner.start(task, _execution_config(tmp_path)) is True
        assert terminal.wait(2.0) is True
    finally:
        runner.shutdown()

    terminal_text = capsys.readouterr().out
    visible_markers = (
        "[thinking] 我需要先检查页面",
        "[assistant] 已确认页面",
        "[tool] Read",
        '"file_path": "资料.txt"',
        "[tool] Glob\n{}",
        "[tool] web_search",
        '"query": "NearMate"',
        "[tool.result] 已读取资料",
        "[tool.result] 已完成网页搜索",
        "[result] 最终结果文字",
        "[cli.stderr] cli-stderr-sentinel",
        task.activity_id,
    )
    hidden_markers = (
        "SystemMessage",
        "AssistantMessage",
        "UserMessage",
        "ResultMessage",
        "thinking_tokens",
        "hidden-thinking-signature",
        "hidden-tool-use-id",
        "hidden-empty-tool-use-id",
        "hidden-server-tool-use-id",
        "hidden-system-uuid",
        "hidden-assistant-uuid",
        "hidden-user-uuid",
        "hidden-message-id",
        "hidden-model-name",
        "hidden-image-base64",
        "session-sdk",
        "input_tokens",
        "output_tokens",
    )
    if should_be_visible:
        assert all(marker in terminal_text for marker in visible_markers)
    else:
        assert terminal_text == ""
    assert all(marker not in terminal_text for marker in hidden_markers)


def test_agent_task_runner_prints_result_failure_reason(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    """失败终态必须显示具体原因，但不能带回身份和用量元数据。"""

    monkeypatch.setattr(
        "companion.background_agent.runner.ClaudeSDKClient",
        lambda options: TerminalDiagnosticsFailureClient(options),
    )
    events: list[AgentEvent] = []
    terminal = Event()
    runner = _runner(
        tmp_path,
        events,
        terminal,
        terminal_diagnostics_enabled=True,
    )

    try:
        assert runner.start(_task(tmp_path), _execution_config(tmp_path)) is True
        assert terminal.wait(2.0) is True
    finally:
        runner.shutdown()

    terminal_text = capsys.readouterr().out
    assert "[result] 任务执行失败：browser_connection_lost" in terminal_text
    assert "hidden-result-uuid" not in terminal_text
    assert "session-sdk" not in terminal_text
    assert "output_tokens" not in terminal_text


def test_terminal_diagnostics_do_not_break_task_on_unencodable_text(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """开发终端无法编码窗口标题时，真实 Agent 仍必须完成任务。"""

    monkeypatch.setattr(
        "companion.background_agent.runner.ClaudeSDKClient",
        lambda options: UnencodableTerminalDiagnosticsClient(options),
    )
    terminal_bytes = io.BytesIO()
    terminal_stdout = io.TextIOWrapper(terminal_bytes, encoding="gbk")
    monkeypatch.setattr(sys, "stdout", terminal_stdout)
    events: list[AgentEvent] = []
    terminal = Event()
    runner = _runner(
        tmp_path,
        events,
        terminal,
        terminal_diagnostics_enabled=True,
    )

    try:
        assert runner.start(_task(tmp_path), _execution_config(tmp_path)) is True
        assert terminal.wait(2.0) is True
    finally:
        runner.shutdown()

    terminal_stdout.flush()
    terminal_text = terminal_bytes.getvalue().decode("gbk")
    terminal_events = [
        event
        for event in events
        if event.event_type in {"completed", "failed", "cancelled"}
    ]
    assert [event.event_type for event in terminal_events] == ["completed"]
    assert "Microsoft\\u200b Edge" in terminal_text


def test_connected_response_stream_failure_emits_one_failed_terminal(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """已连接后的响应异常必须收敛成一条可观察的失败终态。"""

    monkeypatch.setattr(
        "companion.background_agent.runner.ClaudeSDKClient",
        lambda _options: ResponseStreamFailureClient(),
    )
    events: list[AgentEvent] = []
    terminal = Event()
    runner = _runner(tmp_path, events, terminal)

    try:
        assert runner.start(_task(tmp_path), _execution_config(tmp_path)) is True
        assert terminal.wait(2.0) is True
    finally:
        runner.shutdown()

    terminal_events = [
        event
        for event in events
        if event.event_type in {"completed", "failed", "cancelled"}
    ]
    assert [event.event_type for event in terminal_events] == ["failed"]
    assert terminal_events[0].payload["error_code"] == "agent_execution_error"


def test_agent_task_runner_cleans_cua_before_finalizing_grant(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """CUA 清理必须依次结束 Session、断开 SDK/MCP，再处理 Grant。

    Args:
        monkeypatch: pytest 属性替换器。
        tmp_path: pytest 临时目录。

    Returns:
        无返回值；断言失败时由 pytest 报告。
    """

    lifecycle_events: list[str] = []

    class OrderedClient(SuccessClient):
        """记录 SDK/MCP transport 断开发生的顺序。"""

        async def disconnect(self) -> None:
            """记录当前 Client 已经完成断开。"""

            lifecycle_events.append("disconnect_transport")

    monkeypatch.setattr(
        "companion.background_agent.runner.ClaudeSDKClient",
        lambda _options: OrderedClient(),
    )
    events: list[AgentEvent] = []
    terminal = Event()
    integration = RecordingCuaIntegration()
    capability = CuaCapabilitySlot()
    original_end_browser_session = integration.end_browser_session

    def record_cleanup(activity_id: str) -> None:
        """记录 Browser Session 已在 transport 断开前结束。

        Args:
            activity_id: 当前结束的后台任务编号。

        Returns:
            无返回值；调用转交给原测试替身。
        """

        lifecycle_events.append("end_session")
        original_end_browser_session(activity_id)

    integration.end_browser_session = record_cleanup  # type: ignore[method-assign]

    def record(event: AgentEvent) -> None:
        """保存 Runner 事件并在终态时唤醒测试。

        Args:
            event: Runner 发布的 Activity 事件。

        Returns:
            无返回值；终态时设置同步事件。
        """

        events.append(event)
        if event.event_type in {"completed", "failed", "cancelled"}:
            terminal.set()

    def finalize_grant(activity_id: str) -> None:
        """记录 Grant finalizer 并精确释放当前所有权。"""

        lifecycle_events.append("finalize_grant")
        assert capability.release(activity_id) is True

    runner = AgentTaskRunner(
        runtime_dir=tmp_path / "runtime",
        workspace_manager=WorkspaceManager(
            asset_root=tmp_path / "assets",
            workspace_root=tmp_path / "workspace",
        ),
        event_callback=record,
        cua_integration=integration,
        cua_capability=capability,
        cua_grant_finalizer=finalize_grant,
    )
    task = replace(
        _task(tmp_path),
        skill_snapshot=SkillExecutionSnapshot(
            enabled_skill_ids=(CUA_SKILL_ID,),
            revision=1,
        ),
        cua_capability_granted=True,
    )
    capability.acquire(task.activity_id)

    try:
        assert runner.start(task, _execution_config(tmp_path)) is True
        assert terminal.wait(2.0) is True
        assert _wait_until(runner.is_available) is True
    finally:
        runner.shutdown()

    assert integration.start_calls == 0
    assert lifecycle_events == [
        "end_session",
        "disconnect_transport",
        "finalize_grant",
    ]
    assert integration.ended_browser_activity_ids == [task.activity_id]
    assert capability.snapshot().available is True


def test_agent_task_runner_releases_grant_when_end_session_fails(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """Browser Session 清理失败只能写诊断，不能挡住 transport 和 Grant 清理。

    Args:
        monkeypatch: pytest 属性替换器。
        tmp_path: pytest 临时目录。

    Returns:
        无返回值；断言失败时由 pytest 报告。
    """

    lifecycle_events: list[str] = []

    class FailingCleanupIntegration(RecordingCuaIntegration):
        """模拟官方 `end_session` 在短预算内失败。"""

        def end_browser_session(self, activity_id: str) -> None:
            """记录调用并模拟清理超时。"""

            self.ended_browser_activity_ids.append(activity_id)
            raise TimeoutError("end_session timeout")

    class DisconnectRecordingClient(SuccessClient):
        """记录 Session 清理异常后的 transport 断开。"""

        async def disconnect(self) -> None:
            """证明 transport 仍得到清理机会。"""

            lifecycle_events.append("disconnect_transport")

    monkeypatch.setattr(
        "companion.background_agent.runner.ClaudeSDKClient",
        lambda _options: DisconnectRecordingClient(),
    )
    events: list[AgentEvent] = []
    terminal = Event()
    integration = FailingCleanupIntegration()
    capability = CuaCapabilitySlot()

    def finalize_grant(activity_id: str) -> None:
        """记录失败隔离后的精确 Grant 释放。"""

        lifecycle_events.append("finalize_grant")
        assert capability.release(activity_id) is True

    runner = AgentTaskRunner(
        runtime_dir=tmp_path / "runtime",
        workspace_manager=WorkspaceManager(
            asset_root=tmp_path / "assets",
            workspace_root=tmp_path / "workspace",
        ),
        event_callback=lambda event: (
            events.append(event),
            terminal.set()
            if event.event_type in {"completed", "failed", "cancelled"}
            else None,
        ),
        cua_integration=integration,
        cua_capability=capability,
        cua_grant_finalizer=finalize_grant,
    )
    task = replace(
        _task(tmp_path),
        skill_snapshot=SkillExecutionSnapshot(
            enabled_skill_ids=(CUA_SKILL_ID,),
            revision=1,
        ),
        cua_capability_granted=True,
    )
    capability.acquire(task.activity_id)

    try:
        assert runner.start(task, _execution_config(tmp_path)) is True
        assert terminal.wait(2.0) is True
        assert _wait_until(runner.is_available) is True
    finally:
        runner.shutdown()

    assert any(event.event_type == "completed" for event in events)
    assert lifecycle_events == ["disconnect_transport", "finalize_grant"]
    assert capability.snapshot().available is True


@pytest.mark.parametrize("shutdown_timeout", [0.0, 0.05])
def test_agent_task_runner_shutdown_waits_for_real_disconnect_before_releasing_grant(
    monkeypatch,
    tmp_path: Path,
    shutdown_timeout: float,
) -> None:
    """shutdown 返回后也必须等 transport 真正断开才能释放 CUA Grant。

    Args:
        monkeypatch: pytest 属性替换器。
        tmp_path: pytest 临时目录。
        shutdown_timeout: 本次 shutdown 使用的同步等待预算。

    Returns:
        无返回值；放行前后的 Grant 和线程状态由断言验证。
    """

    disconnect_started = Event()
    allow_disconnect = Event()
    disconnect_finished = Event()
    lifecycle_events: list[str] = []

    class BlockingDisconnectClient(SuccessClient):
        """让 transport 断开等待测试显式放行。"""

        async def disconnect(self) -> None:
            """记录断开开始，并在真实工作线程结束后公开完成事实。"""

            disconnect_started.set()
            await asyncio.to_thread(allow_disconnect.wait)
            disconnect_finished.set()

    monkeypatch.setattr(
        "companion.background_agent.runner.ClaudeSDKClient",
        lambda _options: BlockingDisconnectClient(),
    )
    events: list[AgentEvent] = []
    terminal = Event()
    capability = CuaCapabilitySlot()

    def record(event: AgentEvent) -> None:
        """保存业务事件，并在终态形成时唤醒测试。

        Args:
            event: Runner 发布的 Activity 事件。

        Returns:
            无返回值；终态时设置同步事件。
        """

        events.append(event)
        if event.event_type in {"completed", "failed", "cancelled"}:
            terminal.set()

    def finalize_grant(activity_id: str) -> None:
        """记录取消清理后的 Grant 释放。

        Args:
            activity_id: 正在收口的 Activity 编号。

        Returns:
            无返回值；精确释放结果由断言验证。
        """

        lifecycle_events.append("finalize_grant")
        assert capability.release(activity_id) is True

    runner = AgentTaskRunner(
        runtime_dir=tmp_path / "runtime",
        workspace_manager=WorkspaceManager(
            asset_root=tmp_path / "assets",
            workspace_root=tmp_path / "workspace",
        ),
        event_callback=record,
        cua_integration=RecordingCuaIntegration(),
        cua_capability=capability,
        cua_grant_finalizer=finalize_grant,
    )
    task = replace(
        _task(tmp_path),
        skill_snapshot=SkillExecutionSnapshot(
            enabled_skill_ids=(CUA_SKILL_ID,),
            revision=1,
        ),
        cua_capability_granted=True,
    )
    capability.acquire(task.activity_id)

    try:
        assert runner.start(task, _execution_config(tmp_path)) is True
        assert terminal.wait(2.0) is True
        assert disconnect_started.wait(2.0) is True

        runner.shutdown(timeout=shutdown_timeout)

        assert [event.event_type for event in _terminal_events(events)] == ["completed"]
        assert disconnect_finished.is_set() is False
        assert lifecycle_events == []
        assert capability.snapshot().available is False
        assert runner._current_task is task  # noqa: SLF001
        assert runner._thread.is_alive() is True  # noqa: SLF001

        allow_disconnect.set()
        assert runner.wait_until_shutdown(timeout=2.0) is True
        assert disconnect_finished.is_set() is True
        assert lifecycle_events == ["finalize_grant"]
        assert capability.snapshot().available is True
        assert runner._current_task is None  # noqa: SLF001
        assert runner._thread.is_alive() is False  # noqa: SLF001
    finally:
        allow_disconnect.set()
        runner.shutdown(timeout=1.0)


def test_runner_reuses_sdk_context_with_new_turn_callback_transport(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """纠正复用 AgentSession 上下文，但每个 Turn 必须使用新的回调 transport。"""

    created_clients: list[TurnTransportClient] = []
    created_options: list[object] = []

    def create_client(options: object) -> TurnTransportClient:
        created_options.append(options)
        client = TurnTransportClient()
        created_clients.append(client)
        return client

    monkeypatch.setattr(
        "companion.background_agent.runner.ClaudeSDKClient",
        create_client,
    )
    events: list[AgentEvent] = []
    terminal = Event()
    runner = _runner(tmp_path, events, terminal)
    first_task = _task(tmp_path)

    assert runner.start(first_task, _execution_config(tmp_path)) is True
    assert terminal.wait(2.0) is True
    assert _wait_until(runner.is_available) is True
    first_completed = next(event for event in events if event.event_type == "completed")
    agent_session_id = first_completed.payload["agent_session_id"]
    assert isinstance(agent_session_id, str)

    terminal.clear()
    second_activity_id = "00000000-0000-4000-8000-000000000002"
    second_output_dir, _second_agent_config_dir = _prepare_followup_task_paths(
        tmp_path,
        second_activity_id,
        inherited_agent_config_dir=Path(first_task.agent_config_dir),
    )
    second_task = replace(
        first_task,
        activity_id=second_activity_id,
        goal="执行纠正后的目标",
        task_output_dir=str(second_output_dir),
        agent_session_id=agent_session_id,
        expected_sdk_session_id="session-sdk-reused",
        based_on_activity_id=first_task.activity_id,
        parent_task_context=None,
        task_mode="activity_correction",
        correction_instruction="新的目标权威，尽力清理旧目标无用文件。",
    )

    assert runner.start(second_task, _execution_config(tmp_path)) is True
    assert terminal.wait(2.0) is True
    assert _wait_until(runner.is_available) is True

    assert len(created_clients) == 2
    first_client, second_client = created_clients
    assert first_client.connect_count == 1
    assert second_client.connect_count == 1
    assert "完成测试任务" in first_client.prompts[0]
    assert "执行纠正后的目标" in second_client.prompts[0]
    assert getattr(created_options[1], "resume", None) == "session-sdk-reused"
    second_completed = next(
        event
        for event in events
        if event.event_type == "completed" and event.activity_id == second_activity_id
    )
    assert second_completed.payload["agent_session_id"] == agent_session_id
    assert first_task.activity_id != second_activity_id

    runner.shutdown()
    assert first_client.disconnect_count == 1
    assert second_client.disconnect_count == 1


def test_background_correction_fails_when_resume_init_returns_new_sdk_id(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """resume 参数存在也不够；init 返回新编号时必须拒绝纠正结果。"""

    created_clients: list[TurnTransportClient] = []
    created_options: list[object] = []

    def create_client(options: object) -> TurnTransportClient:
        created_options.append(options)
        session_id = "sdk-parent" if not created_clients else "sdk-unexpected-new"
        client = TurnTransportClient(session_id)
        created_clients.append(client)
        return client

    monkeypatch.setattr(
        "companion.background_agent.runner.ClaudeSDKClient",
        create_client,
    )
    events: list[AgentEvent] = []
    terminal = Event()
    runner = _runner(tmp_path, events, terminal)
    first_task = _task(tmp_path)
    assert runner.start(first_task, _execution_config(tmp_path)) is True
    assert terminal.wait(2.0) is True
    assert _wait_until(runner.is_available) is True
    first_completed = next(event for event in events if event.event_type == "completed")

    terminal.clear()
    second_activity_id = "00000000-0000-4000-8000-000000000099"
    second_output_dir, _second_agent_config_dir = _prepare_followup_task_paths(
        tmp_path,
        second_activity_id,
        inherited_agent_config_dir=Path(first_task.agent_config_dir),
    )
    correction_task = replace(
        first_task,
        activity_id=second_activity_id,
        goal="纠正后的新目标",
        task_output_dir=str(second_output_dir),
        agent_session_id=str(first_completed.payload["agent_session_id"]),
        expected_sdk_session_id="sdk-parent",
        based_on_activity_id=first_task.activity_id,
        task_mode="activity_correction",
    )
    assert runner.start(correction_task, _execution_config(tmp_path)) is True
    assert terminal.wait(2.0) is True

    failed = next(
        event
        for event in events
        if event.event_type == "failed" and event.activity_id == second_activity_id
    )
    assert failed.payload["error_code"] == "correction_session_resume_failed"
    assert not any(
        event.event_type == "completed" and event.activity_id == second_activity_id
        for event in events
    )
    assert getattr(created_options[1], "resume", None) == "sdk-parent"
    assert created_clients[1].disconnect_count >= 1
    runner.shutdown()


def test_runner_disconnects_previous_session_before_new_ordinary_task(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """普通任务不复用上下文，每项 Activity 完成后都断开自己的 Client。"""

    created_clients: list[TurnTransportClient] = []

    def create_client(_options: object) -> TurnTransportClient:
        """为每个普通 AgentSession 创建可计数的测试 Client。"""

        client = TurnTransportClient()
        created_clients.append(client)
        return client

    monkeypatch.setattr(
        "companion.background_agent.runner.ClaudeSDKClient",
        create_client,
    )
    events: list[AgentEvent] = []
    terminal = Event()
    runner = _runner(tmp_path, events, terminal)
    first_task = _task(tmp_path)

    assert runner.start(first_task, _execution_config(tmp_path)) is True
    assert terminal.wait(2.0) is True
    assert _wait_until(runner.is_available) is True

    terminal.clear()
    second_activity_id = "00000000-0000-4000-8000-000000000002"
    second_output_dir, second_agent_config_dir = _prepare_followup_task_paths(
        tmp_path,
        second_activity_id,
    )
    second_task = replace(
        first_task,
        activity_id=second_activity_id,
        goal="新的普通任务",
        agent_config_dir=str(second_agent_config_dir),
        task_output_dir=str(second_output_dir),
        agent_session_id=None,
        based_on_activity_id=None,
        parent_task_context=None,
    )
    second_execution_config = _execution_config(tmp_path)
    second_execution_config = replace(
        second_execution_config,
        model_config=replace(
            second_execution_config.model_config,
            config_dir=second_agent_config_dir,
        ),
    )
    assert runner.start(second_task, second_execution_config) is True
    assert terminal.wait(2.0) is True
    assert _wait_until(runner.is_available) is True

    assert len(created_clients) == 2
    assert created_clients[0].disconnect_count == 1
    assert created_clients[1].disconnect_count == 1
    runner.shutdown()
    assert created_clients[1].disconnect_count == 1


def test_runner_keeps_session_id_without_publishing_session_event(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """空 System 消息不能清掉会话编号，也不能产生无消费者事件。

    Args:
        monkeypatch: 用于替换真实 SDK Client 的 pytest 工具。
        tmp_path: pytest 为当前用例创建的临时目录。
    Returns:
        无。
    """

    client = RepeatedSystemMessageClient("session-from-init")
    monkeypatch.setattr(
        "companion.background_agent.runner.ClaudeSDKClient",
        lambda _options: client,
    )
    events: list[AgentEvent] = []
    terminal = Event()
    runner = _runner(tmp_path, events, terminal)

    assert runner.start(_task(tmp_path), _execution_config(tmp_path)) is True
    assert terminal.wait(2.0) is True

    completed = next(event for event in events if event.event_type == "completed")
    assert completed.payload["session_id"] == "session-from-init"
    assert all(event.event_type != "session_started" for event in events)
    runner.shutdown()


@pytest.mark.parametrize("result_session_id", ["", "session-from-result"])
def test_runner_rejects_result_session_id_drift(
    monkeypatch,
    tmp_path: Path,
    result_session_id: str,
) -> None:
    """Result 缺失或改变 SDK 编号时，普通任务也不能被误判为完成。"""

    monkeypatch.setattr(
        "companion.background_agent.runner.ClaudeSDKClient",
        lambda _options: RepeatedSystemMessageClient(result_session_id),
    )
    events: list[AgentEvent] = []
    terminal = Event()
    runner = _runner(tmp_path, events, terminal)

    assert runner.start(_task(tmp_path), _execution_config(tmp_path)) is True
    assert terminal.wait(2.0) is True

    failed = next(event for event in events if event.event_type == "failed")
    assert failed.payload["error_code"] == "invalid_provider_response"
    assert not any(event.event_type == "completed" for event in events)
    runner.shutdown()


def test_runner_emits_only_ordered_text_blocks_as_turn_output(tmp_path: Path) -> None:
    """同一 AssistantMessage 的多个 TextBlock 按顺序合并，工具块不进入正文。"""

    events: list[AgentEvent] = []
    runner = _runner(tmp_path, events, Event())
    task = _task(tmp_path)
    runner._consume_assistant_message(
        task,
        AssistantMessage(
            content=[
                TextBlock(text="第一段"),
                ToolUseBlock(id="tool-a", name="Read", input={"file_path": "a.txt"}),
                TextBlock(text="第二段"),
            ],
            model="test-model",
        ),
        AgentWorkPlanMirror(),
    )

    turn_outputs = [event for event in events if event.event_type == "turn_output"]
    assert len(turn_outputs) == 1
    assert turn_outputs[0].payload["turn_index"] == 1
    assert turn_outputs[0].payload["content_text"] == "第一段\n第二段"
    assert [event.event_type for event in events] == ["turn_output"]
    runner.shutdown()


def test_agent_task_runner_rejects_success_without_structured_output(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """SDK 标记成功但没有结构化终态时必须进入无效响应失败。"""

    monkeypatch.setattr(
        "companion.background_agent.runner.ClaudeSDKClient",
        lambda _options: MissingStructuredOutputClient(),
    )
    events: list[AgentEvent] = []
    terminal = Event()
    runner = _runner(tmp_path, events, terminal)

    assert runner.start(_task(tmp_path), _execution_config(tmp_path)) is True
    assert terminal.wait(2.0) is True

    failed = next(event for event in events if event.event_type == "failed")
    assert failed.payload["error_code"] == "invalid_provider_response"
    assert [event.event_type for event in _terminal_events(events)] == ["failed"]
    runner.shutdown()


def test_agent_task_runner_emits_confirmed_work_plan_without_task_tool_events(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """Runner 只在 Task 工具成功后发布全量计划，不念内部工具名。"""

    monkeypatch.setattr(
        "companion.background_agent.runner.ClaudeSDKClient",
        lambda _options: WorkPlanClient(),
    )
    events: list[AgentEvent] = []
    terminal = Event()
    runner = _runner(tmp_path, events, terminal)

    assert runner.start(_task(tmp_path), _execution_config(tmp_path)) is True
    assert terminal.wait(2.0) is True

    plan_events = [event for event in events if event.event_type == "work_plan_changed"]
    assert [event.payload["work_plan"]["revision"] for event in plan_events] == [1, 2]
    assert plan_events[-1].payload["work_plan"]["items"][0]["status"] == "in_progress"
    assert not any("tool_name" in event.payload for event in events)
    runner.shutdown()


def test_agent_task_runner_maps_explicit_interrupt_to_cancelled(
    monkeypatch,
    tmp_path: Path,
) -> None:
    client = InterruptibleClient()
    monkeypatch.setattr(
        "companion.background_agent.runner.ClaudeSDKClient",
        lambda _options: client,
    )
    events: list[AgentEvent] = []
    terminal = Event()
    runner = _runner(tmp_path, events, terminal)
    task = _task(tmp_path)

    assert runner.start(task, _execution_config(tmp_path)) is True
    assert _wait_until(lambda: any(event.event_type == "started" for event in events))
    assert runner.cancel(task.activity_id) is True
    assert terminal.wait(2.0) is True

    assert any(event.event_type == "cancelled" for event in events)
    assert not any(event.event_type == "failed" for event in events)
    runner.shutdown()


def test_agent_task_runner_preserves_user_correction_when_plain_cancel_arrives_late(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """纠正先冻结的取消原因不能被迟到的普通取消覆盖。"""

    client = CancellationRaceSuccessClient()
    monkeypatch.setattr(
        "companion.background_agent.runner.ClaudeSDKClient",
        lambda _options: client,
    )
    events: list[AgentEvent] = []
    terminal = Event()
    runner = _runner(tmp_path, events, terminal)
    task = _task(tmp_path)

    assert runner.start(task, _execution_config(tmp_path)) is True
    assert client.response_started.wait(2.0) is True
    assert runner.cancel(task.activity_id, reason="user_correction") is True
    assert client.interrupted.wait(2.0) is True
    assert runner.cancel(task.activity_id) is True
    client.completion_allowed.set()
    assert terminal.wait(2.0) is True

    cancelled = next(event for event in events if event.event_type == "cancelled")
    assert cancelled.payload["reason"] == "user_correction"
    runner.shutdown()


def test_agent_task_runner_rolls_back_cancel_when_interrupt_cannot_be_scheduled(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """interrupt 未进入事件循环时，取消原因和交互桥都不能部分提交。"""

    client = CancellationRaceSuccessClient()
    monkeypatch.setattr(
        "companion.background_agent.runner.ClaudeSDKClient",
        lambda _options: client,
    )
    events: list[AgentEvent] = []
    terminal = Event()
    runner = _runner(tmp_path, events, terminal)
    task = _task(tmp_path)
    rejected_coroutines: list[object] = []

    assert runner.start(task, _execution_config(tmp_path)) is True
    assert _wait_until(lambda: runner._current_client_connected) is True
    with runner._lock:
        bridge = runner._current_bridge
    assert bridge is not None

    def reject_interrupt(coroutine: object, _loop: object) -> object:
        """模拟事件循环拒绝接收 interrupt 协程。

        Args:
            coroutine: Runner 准备提交的 interrupt 协程。
            _loop: 本次测试不使用的 SDK 事件循环。

        Raises:
            RuntimeError: 每次提交都模拟事件循环已经关闭。
        """

        rejected_coroutines.append(coroutine)
        raise RuntimeError("SDK 事件循环已经关闭")

    monkeypatch.setattr(
        "companion.background_agent.runner.asyncio.run_coroutine_threadsafe",
        reject_interrupt,
    )
    try:
        with pytest.raises(RuntimeError, match="SDK 事件循环已经关闭"):
            runner.cancel(task.activity_id, reason="user_correction")

        with runner._lock:
            assert runner._cancel_requested_activity_id is None
            assert runner._cancel_requested_reason is None
        assert bridge._closed is False
        assert len(rejected_coroutines) == 1
        assert getattr(rejected_coroutines[0], "cr_frame", None) is None
    finally:
        monkeypatch.undo()
        for coroutine in rejected_coroutines:
            close = getattr(coroutine, "close", None)
            if callable(close):
                close()
        client.completion_allowed.set()
        assert terminal.wait(2.0) is True
        runner.shutdown()


def test_agent_task_runner_cancel_wins_over_late_success_result(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """Runner 接纳用户纠正后，迟到 success 必须投影为旧任务取消。"""

    client = CancellationRaceSuccessClient()
    monkeypatch.setattr(
        "companion.background_agent.runner.ClaudeSDKClient",
        lambda _options: client,
    )
    events: list[AgentEvent] = []
    terminal = Event()
    runner = _runner(tmp_path, events, terminal)
    task = _task(tmp_path)

    assert runner.start(task, _execution_config(tmp_path)) is True
    assert client.response_started.wait(2.0) is True
    assert runner.cancel(task.activity_id, reason="user_correction") is True
    assert client.interrupted.wait(2.0) is True
    client.completion_allowed.set()
    assert terminal.wait(2.0) is True

    terminal_events = _terminal_events(events)
    assert [event.event_type for event in terminal_events] == ["cancelled"]
    assert terminal_events[0].payload["reason"] == "user_correction"
    runner.shutdown()


def test_agent_task_runner_shutdown_with_zero_budget_keeps_partial_usage_terminal(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """关闭预算耗尽时也必须同步留下取消终态和已经收集的部分用量。"""

    client = StubbornShutdownClient()
    monkeypatch.setattr(
        "companion.background_agent.runner.ClaudeSDKClient",
        lambda _options: client,
    )
    events: list[AgentEvent] = []
    terminal = Event()
    runner = _runner(tmp_path, events, terminal)

    assert runner.start(_task(tmp_path), _execution_config(tmp_path)) is True
    assert client.waiting_after_usage.wait(2.0) is True

    try:
        runner.shutdown(timeout=0.0)

        assert terminal.wait(0.2) is True
        cancelled = next(event for event in events if event.event_type == "cancelled")
        assert cancelled.payload["internal_usage"] == {
            "is_complete": False,
            "reference_cost_usd": None,
            "by_model": [
                {
                    "model": "test-model",
                    "non_cached_input_tokens": 11,
                    "cache_creation_input_tokens": 2,
                    "cache_read_input_tokens": 3,
                    "output_tokens": 5,
                }
            ],
        }
        assert [event.event_type for event in _terminal_events(events)] == ["cancelled"]
    finally:
        client.release_blocking_receive.set()


def test_agent_task_runner_shutdown_before_coroutine_entry_runs_real_cleanup(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """协程首次执行前关闭时，仍必须由真实 finally 释放全部任务资源。

    Args:
        monkeypatch: pytest 属性替换器，用于隔离真实 Claude transport。
        tmp_path: pytest 提供的隔离目录。

    Returns:
        无返回值；终态、Grant、任务身份和线程退出由断言验证。
    """

    loop_blocked = Event()
    allow_loop_to_continue = Event()
    events: list[AgentEvent] = []
    terminal = Event()
    capability = CuaCapabilitySlot()
    integration = RecordingCuaIntegration()
    monkeypatch.setattr(
        "companion.background_agent.runner.ClaudeSDKClient",
        lambda _options: SuccessClient(),
    )
    monkeypatch.setattr(
        "companion.background_agent.runner.build_agent_options",
        lambda **_kwargs: object(),
    )
    runner = AgentTaskRunner(
        runtime_dir=tmp_path / "runtime",
        workspace_manager=WorkspaceManager(
            asset_root=tmp_path / "assets",
            workspace_root=tmp_path / "workspace",
        ),
        event_callback=lambda event: (
            events.append(event),
            terminal.set()
            if event.event_type in {"completed", "failed", "cancelled"}
            else None,
        ),
        cua_integration=integration,  # type: ignore[arg-type]
        cua_capability=capability,
    )
    task = replace(
        _task(tmp_path),
        skill_snapshot=SkillExecutionSnapshot(
            enabled_skill_ids=(CUA_SKILL_ID,),
            revision=1,
        ),
        cua_capability_granted=True,
    )
    capability.acquire(task.activity_id)

    def block_runner_loop() -> None:
        """占住事件循环，稳定制造 Future 已提交但协程尚未进入的窗口。"""

        loop_blocked.set()
        allow_loop_to_continue.wait(timeout=2.0)

    runner._loop.call_soon_threadsafe(block_runner_loop)  # noqa: SLF001
    assert loop_blocked.wait(timeout=2.0) is True

    try:
        assert runner.start(task, _execution_config(tmp_path)) is True

        runner.shutdown(timeout=0.0)
        allow_loop_to_continue.set()

        assert terminal.wait(timeout=2.0) is True
        assert runner.wait_until_shutdown(timeout=2.0) is True
        assert [event.event_type for event in _terminal_events(events)] == ["cancelled"]
        assert integration.ended_browser_activity_ids == [task.activity_id]
        assert capability.snapshot().available is True
        assert runner._current_task is None  # noqa: SLF001
        assert runner._thread.is_alive() is False  # noqa: SLF001
    finally:
        allow_loop_to_continue.set()
        if runner._thread.is_alive():  # noqa: SLF001
            with runner._lock:  # noqa: SLF001
                cleanup_event = runner._current_cleanup_event  # noqa: SLF001
            if cleanup_event is not None:
                cleanup_event.set()
            try:
                runner._loop.call_soon_threadsafe(runner._loop.stop)  # noqa: SLF001
            except RuntimeError:
                pass
            runner.wait_until_shutdown(timeout=2.0)
        capability.release(task.activity_id)


def test_agent_task_runner_zero_budget_waits_for_real_cua_cleanup(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """零关闭预算返回后也不能越过仍在执行的官方 Session 清理。

    Args:
        monkeypatch: pytest 属性替换器。
        tmp_path: pytest 提供的隔离目录。

    Returns:
        无返回值；Grant、Runner 身份和线程退出顺序由断言验证。
    """

    class BlockingEndSessionIntegration(RecordingCuaIntegration):
        """让官方 Session 清理停在可由测试控制的同步边界。"""

        def __init__(self) -> None:
            """初始化清理开始、放行和完成三个同步信号。"""

            super().__init__()
            self.cleanup_started = Event()
            self.allow_cleanup = Event()
            self.cleanup_finished = Event()

        def end_browser_session(self, activity_id: str) -> None:
            """等待测试放行后再完成官方 Session 清理。

            Args:
                activity_id: 当前结束的后台任务编号。

            Returns:
                无返回值；清理完成事实通过同步信号公开。
            """

            self.cleanup_started.set()
            self.allow_cleanup.wait(timeout=2.0)
            self.ended_browser_activity_ids.append(activity_id)
            self.cleanup_finished.set()

    client = SuccessClient()
    monkeypatch.setattr(
        "companion.background_agent.runner.ClaudeSDKClient",
        lambda _options: client,
    )
    events: list[AgentEvent] = []
    terminal = Event()
    capability = CuaCapabilitySlot()
    integration = BlockingEndSessionIntegration()
    runner = AgentTaskRunner(
        runtime_dir=tmp_path / "runtime",
        workspace_manager=WorkspaceManager(
            asset_root=tmp_path / "assets",
            workspace_root=tmp_path / "workspace",
        ),
        event_callback=lambda event: (
            events.append(event),
            terminal.set()
            if event.event_type in {"completed", "failed", "cancelled"}
            else None,
        ),
        cua_integration=integration,  # type: ignore[arg-type]
        cua_capability=capability,
    )
    task = replace(
        _task(tmp_path),
        skill_snapshot=SkillExecutionSnapshot(
            enabled_skill_ids=(CUA_SKILL_ID,),
            revision=1,
        ),
        cua_capability_granted=True,
    )
    capability.acquire(task.activity_id)

    try:
        assert runner.start(task, _execution_config(tmp_path)) is True
        assert integration.cleanup_started.wait(2.0) is True

        runner.shutdown(timeout=0.0)

        assert integration.cleanup_finished.is_set() is False
        assert capability.snapshot().available is False
        assert runner._current_task is task  # noqa: SLF001
        assert runner._thread.is_alive() is True  # noqa: SLF001

        integration.allow_cleanup.set()
        assert runner.wait_until_shutdown(timeout=2.0) is True
        assert integration.cleanup_finished.is_set() is True
        assert capability.snapshot().available is True
        assert runner._current_task is None  # noqa: SLF001
        assert runner._thread.is_alive() is False  # noqa: SLF001
    finally:
        integration.allow_cleanup.set()
        runner.shutdown(timeout=1.0)


def test_agent_task_runner_disconnects_transport_before_waiting_for_task_cleanup(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """响应流依赖 disconnect 收口时，Runner 不能先把全部预算耗在 cleanup 上。"""

    client = DisconnectReleasedResponseClient()
    monkeypatch.setattr(
        "companion.background_agent.runner.ClaudeSDKClient",
        lambda _options: client,
    )
    events: list[AgentEvent] = []
    terminal = Event()
    runner = _runner(tmp_path, events, terminal)

    assert runner.start(_task(tmp_path), _execution_config(tmp_path)) is True
    assert client.response_waiting.wait(2.0) is True

    runner.shutdown(timeout=1.0)

    assert client.disconnected.is_set() is True
    assert runner._thread.is_alive() is False
    assert [event.event_type for event in _terminal_events(events)] == ["cancelled"]


def test_agent_task_runner_maps_response_eof_without_result_to_cli_crash(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        "companion.background_agent.runner.ClaudeSDKClient",
        lambda _options: EmptyResponseClient(),
    )
    events: list[AgentEvent] = []
    terminal = Event()
    runner = _runner(tmp_path, events, terminal)

    assert runner.start(_task(tmp_path), _execution_config(tmp_path)) is True
    assert terminal.wait(2.0) is True

    failed = next(event for event in events if event.event_type == "failed")
    assert failed.payload["error_code"] == "agent_cli_crash"
    assert len(_terminal_events(events)) == 1
    runner.shutdown()


def test_agent_task_runner_emits_exactly_one_terminal_for_duplicate_results(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        "companion.background_agent.runner.ClaudeSDKClient",
        lambda _options: DuplicateResultClient(),
    )
    events: list[AgentEvent] = []
    terminal = Event()
    runner = _runner(tmp_path, events, terminal)

    assert runner.start(_task(tmp_path), _execution_config(tmp_path)) is True
    assert terminal.wait(2.0) is True
    assert _wait_until(lambda: len(_terminal_events(events)) >= 1)

    assert [event.event_type for event in _terminal_events(events)] == ["completed"]
    runner.shutdown()


def test_agent_task_runner_maps_connect_failure_to_cli_crash(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        "companion.background_agent.runner.ClaudeSDKClient",
        lambda _options: ConnectFailureClient(),
    )
    events: list[AgentEvent] = []
    terminal = Event()
    runner = _runner(tmp_path, events, terminal)

    assert runner.start(_task(tmp_path), _execution_config(tmp_path)) is True
    assert terminal.wait(2.0) is True

    failed = next(event for event in events if event.event_type == "failed")
    assert failed.payload["error_code"] == "agent_cli_crash"
    runner.shutdown()


def test_agent_task_runner_remembers_cancel_requested_while_client_connects(
    monkeypatch,
    tmp_path: Path,
) -> None:
    client = SlowConnectClient()
    monkeypatch.setattr(
        "companion.background_agent.runner.ClaudeSDKClient",
        lambda _options: client,
    )
    events: list[AgentEvent] = []
    terminal = Event()
    runner = _runner(tmp_path, events, terminal)
    task = _task(tmp_path)

    assert runner.start(task, _execution_config(tmp_path)) is True
    assert client.connect_started.wait(2.0) is True
    assert runner.cancel(task.activity_id) is True
    client.release_connect.set()
    assert terminal.wait(2.0) is True

    assert client.interrupted.is_set()
    assert [event.event_type for event in _terminal_events(events)] == ["cancelled"]
    runner.shutdown()


def test_agent_task_runner_remembers_cancel_before_client_is_created(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """started 事件与 SDK client 创建之间的取消不能被短窗口吞掉。"""

    client = InterruptibleClient()
    monkeypatch.setattr(
        "companion.background_agent.runner.ClaudeSDKClient",
        lambda _options: client,
    )
    events: list[AgentEvent] = []
    started_callback_entered = Event()
    release_started_callback = Event()
    terminal = Event()

    def record(event: AgentEvent) -> None:
        """在 started 回调中制造 client 尚未创建的确定性窗口。"""

        events.append(event)
        if event.event_type == "started":
            started_callback_entered.set()
            assert release_started_callback.wait(2.0)
        if event.event_type in {"completed", "failed", "cancelled"}:
            terminal.set()

    runner = AgentTaskRunner(
        runtime_dir=tmp_path / "runtime",
        workspace_manager=WorkspaceManager(
            asset_root=tmp_path / "assets",
            workspace_root=tmp_path / "workspace",
        ),
        event_callback=record,
    )
    task = _task(tmp_path)

    assert runner.start(task, _execution_config(tmp_path)) is True
    assert started_callback_entered.wait(2.0) is True
    assert runner.cancel(task.activity_id) is True
    release_started_callback.set()
    assert terminal.wait(2.0) is True

    assert client.interrupted.is_set()
    assert [event.event_type for event in _terminal_events(events)] == ["cancelled"]
    runner.shutdown()


def test_agent_task_runner_maps_question_cancel_answer_to_cancelled(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        "companion.background_agent.runner.ClaudeSDKClient",
        lambda options: QuestionCancellationClient(options),
    )
    events: list[AgentEvent] = []
    terminal = Event()
    runner = _runner(tmp_path, events, terminal)
    task = _task(tmp_path)

    assert runner.start(task, _execution_config(tmp_path)) is True
    assert _wait_until(
        lambda: any(event.event_type == "interaction_requested" for event in events)
    )
    request = next(
        event for event in events if event.event_type == "interaction_requested"
    )
    from companion.background_agent.interaction_bridge import AgentInteractionAnswer

    assert runner.answer_interaction(
        AgentInteractionAnswer(
            request_id=str(request.payload["request_id"]),
            outcome="cancel",
            answers={},
            reason="用户取消了任务。",
        )
    ) is True
    assert terminal.wait(2.0) is True

    assert [event.event_type for event in _terminal_events(events)] == ["cancelled"]
    runner.shutdown()


def test_agent_task_runner_authorizes_supplemental_material_before_question_resumes(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """补充材料必须在 SDK 问题 Future 恢复前进入文件读取护栏。"""

    supplemental_path = tmp_path / "supplemental.txt"
    supplemental_path.write_text("补充资料", encoding="utf-8")
    captured_client: SupplementalMaterialQuestionClient | None = None

    def build_client(options: object) -> SupplementalMaterialQuestionClient:
        """创建并记录测试客户端。"""

        nonlocal captured_client
        captured_client = SupplementalMaterialQuestionClient(
            options,
            supplemental_path,
        )
        return captured_client

    monkeypatch.setattr(
        "companion.background_agent.runner.ClaudeSDKClient",
        build_client,
    )
    events: list[AgentEvent] = []
    terminal = Event()
    runner = _runner(tmp_path, events, terminal)
    task = _task(tmp_path)

    assert runner.start(task, _execution_config(tmp_path)) is True
    assert _wait_until(
        lambda: any(event.event_type == "interaction_requested" for event in events)
    )
    request = next(
        event for event in events if event.event_type == "interaction_requested"
    )
    from companion.background_agent.interaction_bridge import AgentInteractionAnswer

    supplemental_material = AgentMaterial(
        material_id="material-supplemental",
        display_name="supplemental.txt",
        material_kind="dragged_file",
        media_class="text",
        local_path=str(supplemental_path),
        node_kind="file",
    )
    assert runner.answer_question(
        AgentInteractionAnswer(
            request_id=str(request.payload["request_id"]),
            outcome="answer",
            answers={"请提供补充文件。": "文件已附上。"},
            materials=(supplemental_material,),
        ),
    ) is True
    assert terminal.wait(2.0) is True
    assert captured_client is not None
    assert captured_client.hook_result == {}
    updated_question_input = captured_client.updated_question_input
    assert updated_question_input is not None
    response = str(updated_question_input["response"])
    assert "supplemental.txt" in response
    assert json.dumps(str(supplemental_path.resolve()), ensure_ascii=False) in response
    runner.shutdown()


def test_agent_task_runner_resolves_pending_question_before_activity_cancel(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """Activity 取消必须先收口待答卡片，再让 SDK interrupt 取消回调。"""

    created_clients: list[ActivityCancellationDuringQuestionClient] = []

    def create_client(options) -> ActivityCancellationDuringQuestionClient:
        """构造可观察问题 Future 恢复时机的 SDK client。

        Args:
            options: Runner 生成的 Claude Agent SDK 选项。

        Returns:
            本测试使用的 SDK client 替身。
        """

        client = ActivityCancellationDuringQuestionClient(options)
        created_clients.append(client)
        return client

    monkeypatch.setattr(
        "companion.background_agent.runner.ClaudeSDKClient",
        create_client,
    )
    events: list[AgentEvent] = []
    resolved_callback_entered = Event()
    release_resolved_callback = Event()
    terminal = Event()

    def record(event: AgentEvent) -> None:
        """在 resolved 发布中制造窗口，验证 SDK Future 尚未恢复。

        Args:
            event: Runner 产生的后台智能体领域事件。

        Returns:
            无返回值。
        """

        if event.event_type == "interaction_resolved":
            resolved_callback_entered.set()
            assert release_resolved_callback.wait(2.0)
        events.append(event)
        if event.event_type in {"completed", "failed", "cancelled"}:
            terminal.set()

    runner = AgentTaskRunner(
        runtime_dir=tmp_path / "runtime",
        workspace_manager=WorkspaceManager(
            asset_root=tmp_path / "assets",
            workspace_root=tmp_path / "workspace",
        ),
        event_callback=record,
    )
    task = _task(tmp_path)

    assert runner.start(task, _execution_config(tmp_path)) is True
    assert _wait_until(
        lambda: any(event.event_type == "interaction_requested" for event in events)
    )
    cancel_results: list[bool] = []

    def cancel_activity() -> None:
        """从独立线程取消任务，让测试线程可以观察发布屏障。

        Returns:
            无返回值。
        """

        cancel_results.append(runner.cancel(task.activity_id))

    cancel_thread = Thread(target=cancel_activity)
    cancel_thread.start()
    assert resolved_callback_entered.wait(2.0) is True
    question_resumed_before_resolved = created_clients[0].question_resumed.wait(0.2)
    release_resolved_callback.set()
    cancel_thread.join(2.0)
    assert terminal.wait(2.0) is True

    assert question_resumed_before_resolved is False
    assert cancel_results == [True]
    event_types = [event.event_type for event in events]
    resolved_index = event_types.index("interaction_resolved")
    cancelled_index = event_types.index("cancelled")
    assert resolved_index < cancelled_index
    resolved = events[resolved_index]
    assert resolved.payload["outcome"] == "cancelled"
    runner.shutdown()


@pytest.mark.parametrize("failure_site", ["workspace", "persona", "options"])
def test_agent_task_runner_terminalizes_and_releases_initialization_failures(
    monkeypatch,
    tmp_path: Path,
    failure_site: str,
) -> None:
    """工作区、Persona 或 SDK options 初始化失败都必须收口任务。"""

    events: list[AgentEvent] = []
    terminal = Event()
    runner = _runner(tmp_path, events, terminal)
    if failure_site == "workspace":
        monkeypatch.setattr(
            runner._workspace_manager,
            "validate_prepared_task",
            lambda _activity_id, **_kwargs: (_ for _ in ()).throw(
                OSError("workspace failed")
            ),
        )
    elif failure_site == "persona":
        monkeypatch.setattr(
            "companion.background_agent.runner.ensure_persona_file",
            lambda _runtime_dir: (_ for _ in ()).throw(OSError("persona failed")),
        )
    else:
        monkeypatch.setattr(
            "companion.background_agent.runner.build_agent_options",
            lambda **_kwargs: (_ for _ in ()).throw(ValueError("options failed")),
        )

    assert runner.start(_task(tmp_path), _execution_config(tmp_path)) is True
    assert terminal.wait(2.0) is True
    assert [event.event_type for event in _terminal_events(events)] == ["failed"]
    assert _wait_until(runner.is_available)
    runner.shutdown()


def test_agent_task_runner_releases_slot_when_coroutine_submission_fails(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """事件循环拒绝协程提交时，Runner 不能留下未运行的当前任务。"""

    runner = _runner(tmp_path, [], Event())
    with monkeypatch.context() as patch:
        patch.setattr(
            "companion.background_agent.runner.asyncio.run_coroutine_threadsafe",
            lambda _coroutine, _loop: (_ for _ in ()).throw(RuntimeError("loop stopped")),
        )
        assert runner.start(_task(tmp_path), _execution_config(tmp_path)) is False

    assert runner.is_available() is True
    runner.shutdown()


def _runner(
    tmp_path: Path,
    events: list[AgentEvent],
    terminal: Event,
    *,
    terminal_diagnostics_enabled: bool = False,
) -> AgentTaskRunner:
    """构造使用测试工作区的 Runner。"""

    def record(event: AgentEvent) -> None:
        events.append(event)
        if event.event_type in {"completed", "failed", "cancelled"}:
            terminal.set()

    return AgentTaskRunner(
        runtime_dir=tmp_path / "runtime",
        workspace_manager=WorkspaceManager(
            asset_root=tmp_path / "assets",
            workspace_root=tmp_path / "workspace",
        ),
        event_callback=record,
        terminal_diagnostics_enabled=terminal_diagnostics_enabled,
    )


def _execution_config(tmp_path: Path) -> AgentExecutionConfig:
    """构造一项测试任务冻结的可信 SDK 配置。"""

    return AgentExecutionConfig(
        model_config=AgentModelConfig(
            environment={},
            config_dir=(
                tmp_path
                / "runtime"
                / "activities"
                / "00000000-0000-4000-8000-000000000001"
            ),
            model="claude-sonnet-4-5",
        ),
        permission_mode="acceptEdits",
        max_turns=30,
    )


def _task(tmp_path: Path) -> AgentTask:
    """构造与测试工作区一致的后台任务。"""

    activity_id = "00000000-0000-4000-8000-000000000001"
    workspace_root = (tmp_path / "workspace").resolve()
    task_root = workspace_root / "tasks" / activity_id
    agent_config_dir = (
        tmp_path / "runtime" / "activities" / activity_id
    ).resolve()
    (task_root / "inputs").mkdir(parents=True, exist_ok=True)
    (task_root / "outputs").mkdir(exist_ok=True)
    agent_config_dir.mkdir(parents=True, exist_ok=True)
    return AgentTask(
        activity_id=activity_id,
        interaction_session_id="interaction-a",
        goal="完成测试任务",
        deliverable=True,
        requirements="",
        turn_materials=(),
        material_catalog=(),
        workspace_root=str(workspace_root),
        agent_config_dir=str(agent_config_dir),
        task_output_dir=str((task_root / "outputs").resolve()),
        permission_mode="acceptEdits",
    )


def _prepare_followup_task_paths(
    tmp_path: Path,
    activity_id: str,
    *,
    inherited_agent_config_dir: Path | None = None,
) -> tuple[Path, Path]:
    """为 Runner 的第二个 Turn 建立与生产合同一致的目录。

    Args:
        tmp_path: Pytest 为本用例创建的独立临时目录。
        activity_id: 第二项普通任务或纠正任务的 Activity 编号。
        inherited_agent_config_dir: 纠正任务需要复用的父私有配置；普通任务为空。

    Returns:
        第二项任务的输出目录和实际私有 Agent 配置目录。
    """

    task_root = (tmp_path / "workspace" / "tasks" / activity_id).resolve()
    output_root = task_root / "outputs"
    (task_root / "inputs").mkdir(parents=True, exist_ok=True)
    output_root.mkdir(exist_ok=True)
    agent_config_dir = (
        inherited_agent_config_dir.absolute()
        if inherited_agent_config_dir is not None
        else (tmp_path / "runtime" / "activities" / activity_id).resolve()
    )
    agent_config_dir.mkdir(parents=True, exist_ok=True)
    return output_root, agent_config_dir


_DEFAULT_STRUCTURED_OUTPUT = object()


def _result_message(
    *,
    subtype: str,
    result: str | None = None,
    structured_output: object = _DEFAULT_STRUCTURED_OUTPUT,
    session_id: str = "session-sdk",
) -> ResultMessage:
    """按 SDK 当前公开构造函数生成测试终态。

    Args:
        subtype: SDK 终态子类型。
        result: SDK 返回的自由文本结果。
        structured_output: SDK 返回的结构化结果。
        session_id: SDK 终态携带的会话编号。

    Returns:
        可直接交给 Runner 消费的 SDK 终态消息。
    """

    resolved_structured_output = (
        {
            "display_content": "任务完成",
            "delivery_brief": "任务已经完成并生成了可查看的结果。",
        }
        if structured_output is _DEFAULT_STRUCTURED_OUTPUT and subtype == "success"
        else None if structured_output is _DEFAULT_STRUCTURED_OUTPUT else structured_output
    )

    return ResultMessage(
        subtype=subtype,
        duration_ms=1,
        duration_api_ms=1,
        is_error=subtype != "success",
        num_turns=1,
        session_id=session_id,
        result=result,
        structured_output=resolved_structured_output,
    )


def _wait_until(predicate, timeout: float = 2.0) -> bool:
    """在短预算内等待跨线程事件出现。"""

    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def _terminal_events(events: list[AgentEvent]) -> list[AgentEvent]:
    """返回 Runner 已发出的全部终态事件。"""

    return [
        event
        for event in events
        if event.event_type in {"completed", "failed", "cancelled"}
    ]
