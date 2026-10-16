"""验证 Recovery Coordinator 只负责安全工具事务，不再启动隐藏模型。"""

from json import loads
from threading import Event

from companion.foreground_self_reflection.contracts import (
    SelfReflectionRecoveryContext,
    SelfReflectionToolCommitDecision,
)
from companion.foreground_self_reflection.coordinator import ForegroundSelfReflectionCoordinator
from companion.foreground_self_reflection.models import SelfReflectionOutcomeKind
from companion.foreground_self_reflection.prompt import RecoveryPromptFacts
from companion.foreground_self_reflection.tool_policy import SelfReflectionToolDefinition
from companion.llm.foreground_models import ForegroundToolCall


class InlineTools:
    """提供一个受白名单保护的即时工具。"""

    def self_reflection_tools(self):  # type: ignore[no-untyped-def]
        """返回天气类即时工具定义。"""

        return (
            SelfReflectionToolDefinition(
                name="lookup_weather",
                schema={
                    "type": "function",
                    "function": {
                        "name": "lookup_weather",
                        "parameters": {"type": "object"},
                    },
                },
                capability_description="查询当前天气",
                execution_channel="inline",
            ),
        )

    def execute(self, name: str, arguments: dict[str, object]):  # type: ignore[no-untyped-def]
        """返回一个已完成的即时工具事实。"""

        assert name == "lookup_weather"
        assert arguments == {"city": "武汉"}
        return {"state": "completed", "result": "晴朗"}

    def execution_timeout_seconds(self, name: str) -> float | None:
        """即时测试工具没有额外超时。"""

        return None


class ApprovingBroker:
    """无条件批准测试工具提交。"""

    def request_and_wait(self, **_kwargs: object) -> SelfReflectionToolCommitDecision:
        """返回主进程已经批准的工具提交事实。"""

        return SelfReflectionToolCommitDecision(approved=True, reason="tool_commit_approved")


class DeadlineCrossingBroker:
    """在主进程批准工具时把测试时钟推进到决策截止点之后。"""

    def __init__(self, clock: list[float]) -> None:
        """保存由测试控制的单调时钟。

        Args:
            clock: 只包含当前单调时间的可变列表。
        """

        self._clock = clock

    def request_and_wait(self, **_kwargs: object) -> SelfReflectionToolCommitDecision:
        """模拟工具在期限内送达、批准返回时刚好跨过期限。"""

        self._clock[0] = 101.0
        return SelfReflectionToolCommitDecision(approved=True, reason="tool_commit_approved")


def context() -> SelfReflectionRecoveryContext:
    """构造完整的冻结 Recovery 上下文。"""

    return SelfReflectionRecoveryContext(
        request_id="self-reflection-recovery-reflection-coordinator",
        self_reflection_id="reflection-coordinator",
        episode_id="episode-coordinator",
        runtime_epoch=1,
        source_activity_id="cognitive-coordinator",
        source_request_id="request-coordinator",
        source_protocol_transaction_id="transaction-coordinator",
        deadline=100.0,
        prompt_facts=RecoveryPromptFacts(
            original_user_request="查询天气",
            previous_method_capability="只做分析",
            failure_explanation="旧方法没有查询能力",
            tools=(),
        ),
        fallback_text="重新处理没有成功。",
    )


def test_coordinator_executes_one_whitelisted_inline_tool() -> None:
    """Coordinator 返回 canonical 结果，不创建第二个模型调用。"""

    coordinator = ForegroundSelfReflectionCoordinator(
        foreground_tools=InlineTools(),
        tool_commit_broker=ApprovingBroker(),
        monotonic_now=lambda: 10.0,
    )
    outcome = coordinator.execute_tool(
        context=context(),
        tool_call=ForegroundToolCall(
            tool_call_id="tool-call-1",
            name="lookup_weather",
            arguments={"city": "武汉"},
        ),
        interaction_session_id="interaction-1",
        foreground_context={},
        environment_snapshot=None,
        cancellation_event=Event(),
    )

    assert outcome.kind is SelfReflectionOutcomeKind.INLINE_RESULT_AVAILABLE
    assert outcome.tool_fact is not None
    assert loads(outcome.tool_fact.safe_result) == {
        "state": "completed",
        "result": "晴朗",
    }


def test_authorized_inline_tool_is_not_rewritten_by_decision_deadline() -> None:
    """工具越过主进程安全授权后，45 秒决策预算不能再取消执行。"""

    clock = [99.0]
    coordinator = ForegroundSelfReflectionCoordinator(
        foreground_tools=InlineTools(),
        tool_commit_broker=DeadlineCrossingBroker(clock),
        monotonic_now=lambda: clock[0],
    )

    outcome = coordinator.execute_tool(
        context=context(),
        tool_call=ForegroundToolCall(
            tool_call_id="tool-call-crossing-deadline",
            name="lookup_weather",
            arguments={"city": "武汉"},
        ),
        interaction_session_id="interaction-1",
        foreground_context={},
        environment_snapshot=None,
        cancellation_event=Event(),
    )

    assert outcome.kind is SelfReflectionOutcomeKind.INLINE_RESULT_AVAILABLE
    assert outcome.tool_fact is not None
    assert outcome.tool_fact.status.value == "completed"


def test_coordinator_rejects_unknown_tool_without_side_effect() -> None:
    """不在静态白名单中的调用只能形成失败事实。"""

    coordinator = ForegroundSelfReflectionCoordinator(
        foreground_tools=InlineTools(),
        tool_commit_broker=ApprovingBroker(),
        monotonic_now=lambda: 10.0,
    )
    outcome = coordinator.execute_tool(
        context=context(),
        tool_call=ForegroundToolCall(
            tool_call_id="tool-call-unknown",
            name="start_background_work",
            arguments={},
        ),
        interaction_session_id="interaction-1",
        foreground_context={},
        environment_snapshot=None,
        cancellation_event=None,
    )

    assert outcome.kind is SelfReflectionOutcomeKind.REFLECTION_FAILED
    coordinator.shutdown()
