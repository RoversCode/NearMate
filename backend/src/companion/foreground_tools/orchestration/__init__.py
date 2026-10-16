"""提供 Foreground 多工具调用的分类、执行与续写决策。"""

from companion.foreground_tools.orchestration.contracts import (
    BackgroundActivityPrepared,
    DeferredActionPrepared,
    ExecutedToolCall,
    ForegroundToolExecution,
    InlineToolCompleted,
    ToolFailed,
    ToolFollowupFact,
    ToolFollowupFactKind,
    ToolExecutionChannel,
    ToolOutcome,
    ToolRejected,
    ToolReplyRequirement,
    tool_outcome_from_payload,
)
from companion.foreground_tools.orchestration.orchestrator import (
    ForegroundToolCallOrchestrator,
    InlineToolResult,
    PreparedForegroundToolExecution,
)

__all__ = [
    "BackgroundActivityPrepared",
    "DeferredActionPrepared",
    "ExecutedToolCall",
    "ForegroundToolCallOrchestrator",
    "ForegroundToolExecution",
    "InlineToolCompleted",
    "InlineToolResult",
    "PreparedForegroundToolExecution",
    "ToolFailed",
    "ToolFollowupFact",
    "ToolFollowupFactKind",
    "ToolExecutionChannel",
    "ToolOutcome",
    "ToolRejected",
    "ToolReplyRequirement",
    "tool_outcome_from_payload",
]
