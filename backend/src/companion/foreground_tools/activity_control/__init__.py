"""提供 Foreground 对当前 Activity 的精确控制能力。"""

from companion.foreground_tools.activity_control.policy import (
    CancellableActivityContext,
    select_cancellable_activity_contexts,
)
from companion.foreground_tools.activity_control.schemas import (
    CANCEL_ACTIVITY_TOOL,
    activity_control_tool_catalog,
    activity_control_tool_family_catalog,
    activity_control_tool_schemas,
    is_cancel_activity_tool,
)

__all__ = [
    "CANCEL_ACTIVITY_TOOL",
    "CancellableActivityContext",
    "activity_control_tool_catalog",
    "activity_control_tool_family_catalog",
    "activity_control_tool_schemas",
    "is_cancel_activity_tool",
    "select_cancellable_activity_contexts",
]
