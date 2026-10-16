from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from companion.foreground_tools.activity_control.policy import (
        CancellableActivityContext,
    )
    from companion.foreground_tools.activity_correction.policy import (
        CorrectableActivityContext,
    )
    from companion.foreground_tools.orchestration.transactions import CommitAction


TTS_DELIVERY_IMMEDIATE = "immediate"
ForegroundRequestMode = Literal[
    "user_turn",
    "system_notice",
    "timed_reminder_notice",
    "self_reflection_entry",
    "self_reflection_recovery",
    "self_reflection_interruption_reply",
]
ToolInvocationPolicy = Literal["auto", "forbid"]


@dataclass(frozen=True, slots=True)
class ForegroundExecutionContext:
    """保存助手子进程根据冻结事实计算的一轮执行期上下文。

    Args:
        available_tools: 当前请求真实开放的工具声明。
        prompt_environment: 注入派生事实后的 Prompt 环境，不反向修改冻结请求快照。
        correctable_activities: 与 Prompt 和工具 schema 同源的本轮纠正允许列表。
        cancellable_activities: 与 Prompt 和工具 schema 同源的本轮取消允许列表。
        timed_reminders: 与 Prompt、schema 和执行同源的本轮活跃提醒快照。
        timed_reminder_runtime_id: 冻结提醒清单所属的 Backend 运行编号。
    """

    available_tools: tuple[dict[str, Any], ...]
    prompt_environment: dict[str, object]
    correctable_activities: tuple[CorrectableActivityContext, ...] = ()
    cancellable_activities: tuple[CancellableActivityContext, ...] = ()
    timed_reminders: tuple[dict[str, object], ...] = ()
    timed_reminder_runtime_id: str = ""


@dataclass
class ForegroundToolCall:
    """表示前台模型请求执行的一次工具调用。

    Attributes:
        tool_call_id: 模型侧工具调用标识。
        name: 工具名称。
        arguments: 工具参数字典。
    """

    tool_call_id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class ForegroundModelMessage:
    """表示前台模型一次完整回复的标准化结果。

    Attributes:
        content_chunks: 可按原顺序拼接的文本分片列表。
        tool_calls: 模型请求执行的工具调用列表。
    """

    content_chunks: list[str] = field(default_factory=list)
    tool_calls: list[ForegroundToolCall] = field(default_factory=list)


@dataclass
class ForegroundModelStreamChunk:
    """表示前台模型流式返回过程中的一个标准化片段。

    Attributes:
        kind: 片段类型。
        text: 文本增量内容，仅在文本片段中使用。
        tool_calls: 工具调用列表，仅在工具片段中使用。
    """

    kind: Literal["text_delta", "tool_call_started", "tool_calls"]
    text: str = ""
    tool_calls: list[ForegroundToolCall] = field(default_factory=list)


@dataclass
class LlmTextStreamChunk:
    """表示普通文本 LLM 流中的标准化片段。

    Attributes:
        kind: 片段类型；思考、正式文本和用量统计必须分开传递。
        text: 文本增量内容，仅在思考或正式文本片段中使用。
        reasoning_tokens: API 能提供时记录的思考 token 数。
        output_tokens: API 能提供时记录的输出 token 数。
    """

    kind: Literal["reasoning_delta", "text_delta", "usage"]
    text: str = ""
    reasoning_tokens: int | None = None
    output_tokens: int | None = None


@dataclass
class ForegroundEvent:
    """表示前台编排层对外释放的一条事件。

    Attributes:
        type: 事件类型。
        payload: 事件载荷字典。
        commit_action: 仅供 generation 接收边界使用的内部事务，不进入公开事件载荷。
    """

    type: str
    payload: dict[str, Any]
    commit_action: CommitAction | None = None
