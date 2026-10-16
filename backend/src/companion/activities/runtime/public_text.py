"""集中清洗 Activity 对 Desktop 与 Console 公开的文本。"""

from __future__ import annotations

MAX_ACTIVITY_GOAL_CHARS = 2000
MAX_ACTIVITY_GOAL_SUMMARY_CHARS = 80
MAX_ACTIVITY_RESULT_CHARS = 20000


def sanitize_activity_goal(value: object) -> str:
    """清洗并限制 Activity 公开目标。

    Args:
        value: 来源事件中的目标字段。

    Returns:
        最多两千字的公开目标。
    """

    return _limit_text(value, MAX_ACTIVITY_GOAL_CHARS)


def summarize_activity_goal(value: object) -> str:
    """把完整目标压缩为任务列表使用的目标摘要。

    Args:
        value: 来源事件中的目标字段。

    Returns:
        最多八十字的目标摘要。
    """

    return _limit_text(value, MAX_ACTIVITY_GOAL_SUMMARY_CHARS)


def normalize_task_result_goal(value: object) -> str:
    """归一化任务结果公开使用的完整用户目标。

    Args:
        value: 任务结果事件中的用户原始目标。

    Returns:
        去除首尾空白但不截断的目标；非字符串返回空字符串。
    """

    return value.strip() if isinstance(value, str) else ""


def sanitize_activity_result(value: object) -> str:
    """清洗并限制 Activity 公开结果。

    Args:
        value: 来源事件中的结果字段。

    Returns:
        最多两万字的公开结果。
    """

    return _limit_text(value, MAX_ACTIVITY_RESULT_CHARS)


def normalize_activity_completion_content(value: object) -> str:
    """归一化需要进入结果卡与 Activity 详情的完整成果正文。

    Args:
        value: Activity 成功终态中的完整正文。

    Returns:
        去除首尾空白但不截断的完整正文；非字符串返回空字符串。
    """

    return value.strip() if isinstance(value, str) else ""


def _limit_text(value: object, limit: int) -> str:
    """去除首尾空白，并按统一省略号规则限制文本。

    Args:
        value: 待清洗的任意字段。
        limit: 允许公开的最大字符数。

    Returns:
        空字符串或限制后的文本。
    """

    text = value.strip() if isinstance(value, str) else ""
    if len(text) <= limit:
        return text
    return f"{text[: limit - 1]}…"
