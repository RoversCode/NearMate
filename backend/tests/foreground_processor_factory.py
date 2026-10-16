"""为 Processor 单元测试提供经过真实构造函数的完整实例。"""

from __future__ import annotations

from typing import Any, cast

from companion.foreground_tools.catalog.service import ForegroundToolService
from companion.llm.foreground_turn_processor import ForegroundTurnProcessor
from companion.session.context import SessionContext


class _UnexpectedClient:
    """在测试没有显式提供模型客户端时阻止意外模型调用。"""

    def stream_message(self, **_kwargs: object) -> list[object]:
        """拒绝未声明的流式模型调用。

        Args:
            **_kwargs: 意外传入的模型请求参数。

        Raises:
            AssertionError: 测试没有显式配置模型客户端。
        """

        raise AssertionError("本测试没有配置 Foreground 模型客户端")


class _UnexpectedPromptBuilder:
    """在测试没有显式提供 PromptBuilder 时阻止意外构建。"""

    def build(self, _input_value: object) -> list[dict[str, object]]:
        """拒绝未声明的 Prompt 构建。

        Args:
            _input_value: 意外传入的 Prompt 构建参数。

        Raises:
            AssertionError: 测试没有显式配置 PromptBuilder。
        """

        raise AssertionError("本测试没有配置 PromptBuilder")


def build_foreground_processor_for_test(
    *,
    client: object | None = None,
    prompt_builder: object | None = None,
    session_context: SessionContext | None = None,
    foreground_tools: object | None = None,
    cognitive_activity_executor: object | None = None,
    background_agent_activity_executor: object | None = None,
    work_interaction_submission_broker: object | None = None,
    self_reflection_tool_executor: object | None = None,
) -> ForegroundTurnProcessor:
    """通过生产构造函数创建字段完整的 Processor 测试实例。

    Args:
        client: 可选模型客户端替身。
        prompt_builder: 可选 Prompt 构建器替身。
        session_context: 可选会话上下文。
        foreground_tools: 可选工具目录或执行替身。
        cognitive_activity_executor: 可选认知 Activity 执行器。
        background_agent_activity_executor: 可选后台工作 Activity 执行器。
        work_interaction_submission_broker: 可选工作交互权威回执 Broker。
        self_reflection_tool_executor: 可选的 Recovery 专用工具事务执行器。

    Returns:
        所有生产字段都已经初始化的 ForegroundTurnProcessor。
    """

    return ForegroundTurnProcessor(
        client=cast(Any, client or _UnexpectedClient()),
        prompt_builder=cast(Any, prompt_builder or _UnexpectedPromptBuilder()),
        session_context=session_context or SessionContext(),
        foreground_tools=cast(
            ForegroundToolService,
            foreground_tools or ForegroundToolService(),
        ),
        cognitive_activity_executor=cast(Any, cognitive_activity_executor),
        background_agent_activity_executor=cast(
            Any,
            background_agent_activity_executor,
        ),
        work_interaction_submission_broker=cast(
            Any,
            work_interaction_submission_broker,
        ),
        self_reflection_tool_executor=cast(Any, self_reflection_tool_executor),
    )
