from __future__ import annotations

from companion.foreground_tools.cognitive.contracts import CognitiveExecutionPlan
from companion.llm.request_intent import LlmInvocationIntent


def cognitive_invocation_intent(
    plan: CognitiveExecutionPlan,
) -> LlmInvocationIntent:
    """把 Cognitive 计划投影为不含厂商字段的调用意图。

    Args:
        plan: 已完成能力合并和证据准备的认知计划。

    Returns:
        描述标记文本、Thinking 和联网需求的语义调用意图。
    """

    return LlmInvocationIntent.text(
        thinking_level=plan.reasoning,
        web_search=plan.web_search,
        web_extract=plan.web_extract,
    )
