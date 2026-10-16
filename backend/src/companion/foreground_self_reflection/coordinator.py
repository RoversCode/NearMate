"""执行可见 Self-Reflection Recovery 选择的唯一安全工具事务。"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import (
    ThreadPoolExecutor,
    TimeoutError as FutureTimeoutError,
)
from dataclasses import dataclass
from json import dumps
from math import isfinite
from threading import Event, Lock, Timer
from time import monotonic
from typing import Any, Protocol

from companion.foreground_self_reflection.contracts import (
    SelfReflectionRecoveryContext,
    SelfReflectionToolCommitDecision,
)
from companion.foreground_self_reflection.models import (
    SelfReflectionOutcomeFact,
    SelfReflectionOutcomeKind,
    SelfReflectionToolFact,
    SelfReflectionToolStatus,
)
from companion.foreground_self_reflection.tool_policy import (
    SelfReflectionToolDefinition,
)
from companion.foreground_tools.orchestration.commit_barrier import (
    BackgroundCommitDrainBarrier,
)
from companion.llm.foreground_models import ForegroundToolCall
from companion.observability.logging_config import get_backend_logger
from companion.realtime.assistant_events import RuntimeEvent


logger = get_backend_logger(__name__)


class SelfReflectionForegroundToolProtocol(Protocol):
    """定义 Coordinator 使用的静态目录和 Inline 执行端口。"""

    def self_reflection_tools(self) -> tuple[SelfReflectionToolDefinition, ...]:
        """返回本轮专用最小工具集合。

        Returns:
            仅包含静态标记为 recovery-safe 的工具定义。
        """

    def execute(self, name: str, arguments: dict[str, object]) -> dict[str, object]:
        """执行一个已经通过白名单的 Inline 工具。

        Args:
            name: 已在自我反思工具目录登记的工具名。
            arguments: 模型提交并通过工具 schema 校验的参数。

        Returns:
            工具产生的结构化终态载荷。
        """

    def execution_timeout_seconds(self, name: str) -> float | None:
        """返回 Inline 工具的本地等待时限。

        Args:
            name: 需要查询执行预算的工具名。

        Returns:
            工具自己的最大等待秒数；没有额外限制时返回空。
        """


class SelfReflectionBackgroundToolProtocol(Protocol):
    """定义 canonical Background Work 工具执行器的窄端口。"""

    def execute_start_background_work(self, **kwargs: object) -> dict[str, object]:
        """校验参数并准备一项尚未提交的 Background Work。

        Args:
            **kwargs: 后台目标、材料、事务身份和取消信号等完整提交参数。

        Returns:
            accepted 或稳定拒绝状态的结构化载荷。
        """

    def take_prepared_background_work(self, activity_id: str) -> object | None:
        """按 Activity 身份交出唯一 canonical Prepared 结果。

        Args:
            activity_id: prepare 阶段返回的 Background Activity 编号。

        Returns:
            尚未领取的 canonical Prepared 结果；不存在时返回空。
        """


class SelfReflectionToolCommitBrokerProtocol(Protocol):
    """定义真实工具执行前同步等待主进程决定的窄端口。"""

    def request_and_wait(self, **kwargs: object) -> SelfReflectionToolCommitDecision:
        """发送完整提交身份，并等待批准、拒绝、取消或超时。

        Args:
            **kwargs: Recovery、工具、canonical 事实和取消信号。

        Returns:
            主进程决定或本地等待失效事实。
        """


class _RecoveryCancellation:
    """保存工具 deadline 独有的取消信号和稳定原因。"""

    def __init__(self) -> None:
        """创建一份尚未取消且没有原因的工具事务令牌。"""

        self.event = Event()
        self._lock = Lock()
        self._reason = ""

    def cancel(self, reason: str) -> bool:
        """首次设置取消原因并唤醒模型或工具等待。

        Args:
            reason: runtime 热替换、关闭或 deadline 等稳定原因。

        Returns:
            本次首次取消时返回真。
        """

        normalized_reason = reason.strip()
        if not normalized_reason:
            raise ValueError("取消原因不能为空")
        with self._lock:
            if self.event.is_set():
                return False
            self._reason = normalized_reason
            self.event.set()
            return True

    @property
    def reason(self) -> str:
        """返回已经冻结的取消原因。

        Returns:
            尚未取消时为空字符串。
        """

        with self._lock:
            return self._reason


@dataclass(frozen=True, slots=True)
class _RecoveryToolCommand:
    """保存一次 Recovery 工具事务真正需要的冻结字段。

    Args:
        self_reflection_id: 当前自我反思编号。
        runtime_epoch: 所属 Assistant Runtime 世代。
        source_activity_id: 被恢复的 Cognitive Activity 编号。
        source_request_id: 创建来源 Activity 的 Foreground 请求编号。
        source_protocol_transaction_id: 来源 Cognitive 工具事务编号。
        interaction_session_id: 当前产品交互会话编号。
        deadline: 从 Attention Admission 起计算的绝对截止时间。
        foreground_context: 来源请求冻结的材料上下文。
        environment_snapshot: 来源请求冻结的环境现场。
    """

    self_reflection_id: str
    runtime_epoch: int
    source_activity_id: str
    source_request_id: str
    source_protocol_transaction_id: str
    interaction_session_id: str
    deadline: float
    foreground_context: dict[str, object]
    environment_snapshot: object | None


class ForegroundSelfReflectionCoordinator:
    """执行 Recovery 工具事务并返回 canonical 工具事实。

    Args:
        foreground_tools: 自我反思静态工具目录和 Inline 执行端口。
        tool_commit_broker: 在任何真实工具动作前同步取得主进程授权的 Broker。
        background_tool_executor: 可选 canonical Background Work 工具执行器。
        monotonic_now: 可替换的单调时钟。

    模型生成、可见正文和输出结算全部由 ``ForegroundTurnProcessor`` 持有。这里
    只执行模型已经选择的唯一安全工具，不再启动隐藏 LLM 请求。
    """

    def __init__(
        self,
        *,
        foreground_tools: SelfReflectionForegroundToolProtocol,
        tool_commit_broker: SelfReflectionToolCommitBrokerProtocol,
        background_tool_executor: SelfReflectionBackgroundToolProtocol | None = None,
        monotonic_now: Callable[[], float] = monotonic,
        commit_barrier: BackgroundCommitDrainBarrier | None = None,
    ) -> None:
        """初始化 Recovery 工具目录、同步提交端口和执行线程。

        Args:
            foreground_tools: 提供静态白名单和 Inline 执行的工具端口。
            tool_commit_broker: 在工具副作用前等待主进程权威决定的 Broker。
            background_tool_executor: 可选的 canonical Background Work 工具执行器。
            monotonic_now: 计算 45 秒绝对预算时使用的单调时钟。
            commit_barrier: Background Work 提交与执行器关闭共用的排水屏障。
        """

        self._foreground_tools = foreground_tools
        self._background_tool_executor = background_tool_executor
        self._tool_commit_broker = tool_commit_broker
        self._monotonic_now = monotonic_now
        self._commit_barrier = commit_barrier or BackgroundCommitDrainBarrier()
        self._lock = Lock()
        # 这里只登记已经拿到 Background reservation、正在等待或应用主进程决定的恢复。
        # Runtime 关闭会优先让这段提交事务收口，不能把 canonical 已登记的任务回滚掉。
        self._background_commit_sections: set[str] = set()
        self._shutdown_requested = False
        self._inline_executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="self-reflection-inline-tool",
        )

    @property
    def commit_barrier(self) -> BackgroundCommitDrainBarrier:
        """返回与 Background Executor 共用的提交排水屏障。

        Returns:
            当前 Coordinator 使用的生命周期屏障。
        """

        return self._commit_barrier

    def execute_tool(
        self,
        *,
        context: SelfReflectionRecoveryContext,
        tool_call: ForegroundToolCall,
        interaction_session_id: str,
        foreground_context: dict[str, object],
        environment_snapshot: object | None,
        cancellation_event: Event | None,
    ) -> SelfReflectionOutcomeFact:
        """执行可见 Recovery generation 已经选择的唯一安全工具。

        Args:
            context: Recovery 身份、截止时间和失败事实。
            tool_call: 模型产生且必须命中静态白名单的唯一调用。
            interaction_session_id: 当前产品交互会话编号。
            foreground_context: 来源请求冻结的材料与现场上下文。
            environment_snapshot: 来源请求冻结的环境快照。
            cancellation_event: 当前可见 generation 的取消信号。

        Returns:
            Inline 或 Background 提交边界形成的 canonical 恢复事实。
        """

        command = _RecoveryToolCommand(
            self_reflection_id=context.self_reflection_id,
            runtime_epoch=context.runtime_epoch,
            source_activity_id=context.source_activity_id,
            source_request_id=context.source_request_id,
            source_protocol_transaction_id=context.source_protocol_transaction_id,
            interaction_session_id=interaction_session_id,
            deadline=context.deadline,
            foreground_context=dict(foreground_context),
            environment_snapshot=environment_snapshot,
        )
        with self._lock:
            if self._shutdown_requested:
                return self._failed(command, "runtime_shutdown")
        cancellation = _RecoveryCancellation()
        if cancellation_event is not None and cancellation_event.is_set():
            cancellation.cancel(
                str(getattr(cancellation_event, "reason", "") or "reflection_cancelled")
            )
        reason = self._stop_reason(
            command,
            cancellation,
            external_cancellation_event=cancellation_event,
        )
        if reason is not None:
            return self._failed(command, reason)

        definitions = self._foreground_tools.self_reflection_tools()
        definition_by_name = {item.name: item for item in definitions}
        definition = definition_by_name.get(tool_call.name)
        if definition is None or not tool_call.tool_call_id.strip():
            return self._failed(command, "unknown_self_reflection_tool")

        remaining = max(0.0, command.deadline - self._now())
        deadline_timer = Timer(
            remaining,
            lambda: cancellation.cancel("reflection_deadline_exceeded"),
        )
        deadline_timer.daemon = True
        deadline_timer.start()
        try:
            combined_cancellation = _CombinedCancellationSignal(
                cancellation.event,
                cancellation_event,
            )
            if definition.execution_channel == "inline":
                decision = self._tool_commit_broker.request_and_wait(
                    self_reflection_id=command.self_reflection_id,
                    source_activity_id=command.source_activity_id,
                    runtime_epoch=command.runtime_epoch,
                    tool_call_id=tool_call.tool_call_id,
                    tool_name=tool_call.name,
                    execution_channel="inline",
                    canonical_event=None,
                    deadline=command.deadline,
                    cancellation_event=combined_cancellation,
                )
                if not decision.approved:
                    return self._failed(command, decision.reason)
                # 工具请求越过主进程授权边界后，45 秒只剩诊断意义；工具执行和
                # canonical 提交必须继续完成，不能被决策 reaper 取消或重复。
                deadline_timer.cancel()
                return self._execute_inline_tool(
                    command,
                    tool_call=tool_call,
                    external_cancellation_event=cancellation_event,
                )
            # Background 同样在 request_and_wait 返回批准后越过安全边界；后续
            # canonical 提交由主进程事实决定，不再受 Recovery 决策计时器影响。
            deadline_timer.cancel()
            return self._execute_background_tool(
                command,
                cancellation=cancellation,
                tool_call=tool_call,
                external_cancellation_event=cancellation_event,
            )
        except Exception:
            return self._failed(command, "self_reflection_tool_execution_failed")
        finally:
            deadline_timer.cancel()

    def shutdown(self, timeout: float = 1.0) -> None:
        """停止接纳新工具事务，并有界等待 Background 提交排水。

        Args:
            timeout: 等待 Background 正式提交临界区完成的最长秒数。
        """

        normalized_timeout = max(0.0, timeout)
        with self._lock:
            self._shutdown_requested = True

        # 已经越过主进程批准的 Background 提交必须排水，不能在关闭时回滚真实任务。
        self._commit_barrier.request_shutdown()
        drain_status = self._commit_barrier.wait_for_drain(normalized_timeout)
        if drain_status.pending_owner_ids:
            logger.warning(
                "Self-Reflection Background 提交关闭等待超时 count=%s",
                len(drain_status.pending_owner_ids),
            )
        self._inline_executor.shutdown(wait=False, cancel_futures=True)

    def _execute_inline_tool(
        self,
        command: _RecoveryToolCommand,
        *,
        tool_call: ForegroundToolCall,
        external_cancellation_event: Event | None = None,
    ) -> SelfReflectionOutcomeFact:
        """执行已经获得主进程授权的唯一 Inline 工具。

        Args:
            command: 当前恢复命令。
            tool_call: 已通过白名单的唯一调用。
            external_cancellation_event: Runtime 关闭或热替换使用的取消信号。

        Returns:
            Inline 结果可用或安全失败事实。
        """

        future = self._inline_executor.submit(
            self._foreground_tools.execute,
            tool_call.name,
            dict(tool_call.arguments),
        )
        configured_timeout = self._foreground_tools.execution_timeout_seconds(
            tool_call.name
        )
        tool_deadline = (
            self._now() + configured_timeout
            if configured_timeout is not None
            else None
        )
        while True:
            if (
                external_cancellation_event is not None
                and external_cancellation_event.is_set()
            ):
                future.cancel()
                return self._failed(
                    command,
                    str(
                        getattr(external_cancellation_event, "reason", "")
                        or "reflection_cancelled"
                    ),
                )
            wait_timeout = 0.05
            if tool_deadline is not None:
                remaining = tool_deadline - self._now()
                if remaining <= 0:
                    future.cancel()
                    return _inline_failure(
                        command,
                        tool_call=tool_call,
                        safe_message="这项即时查询超时。",
                    )
                wait_timeout = min(wait_timeout, remaining)
            try:
                payload = future.result(timeout=wait_timeout)
                break
            except FutureTimeoutError:
                continue
            except Exception:
                return _inline_failure(
                    command,
                    tool_call=tool_call,
                    safe_message="这项即时查询失败。",
                )

        state = str(payload.get("state") or "").strip()
        if state != "completed":
            safe_message = _safe_message(
                payload,
                fallback="这项即时查询失败。",
            )
            return _inline_failure(
                command,
                tool_call=tool_call,
                safe_message=safe_message,
            )
        return SelfReflectionOutcomeFact(
            self_reflection_id=command.self_reflection_id,
            source_activity_id=command.source_activity_id,
            kind=SelfReflectionOutcomeKind.INLINE_RESULT_AVAILABLE,
            tool_fact=SelfReflectionToolFact(
                tool_name=tool_call.name,
                status=SelfReflectionToolStatus.COMPLETED,
                safe_result=_safe_inline_result(payload),
            ),
        )

    def _execute_background_tool(
        self,
        command: _RecoveryToolCommand,
        *,
        cancellation: _RecoveryCancellation,
        tool_call: ForegroundToolCall,
        external_cancellation_event: Event | None = None,
    ) -> SelfReflectionOutcomeFact:
        """执行 canonical Background prepare、事件提交和 after_commit。

        Args:
            command: 当前恢复命令及 Admission 前材料快照。
            cancellation: 独立恢复取消令牌。
            tool_call: 已通过白名单的唯一后台调用。

        Returns:
            已开始、启动失败或工具拒绝事实。
        """

        executor = self._background_tool_executor
        if executor is None:
            return _background_rejected(
                command,
                tool_call=tool_call,
                safe_message="后台工作当前不可用。",
            )
        try:
            payload = executor.execute_start_background_work(
                execution_lane="recovery",
                tool_call=tool_call,
                interaction_session_id=command.interaction_session_id,
                foreground_context=dict(command.foreground_context),
                environment_snapshot=command.environment_snapshot,
                protocol_transaction_id=(
                    f"self-reflection-{command.self_reflection_id}"
                ),
                cancellation_event=_CombinedCancellationSignal(
                    cancellation.event,
                    external_cancellation_event,
                ),
            )
        except Exception:
            return _background_start_failed(
                command,
                tool_call=tool_call,
                safe_message="后台任务没有成功建立。",
            )

        state = str(payload.get("state") or "").strip()
        if state != "accepted":
            if state == "failed":
                return _background_start_failed(
                    command,
                    tool_call=tool_call,
                    safe_message=_safe_message(
                        payload,
                        fallback="后台任务没有成功建立。",
                    ),
                )
            return _background_rejected(
                command,
                tool_call=tool_call,
                safe_message=_safe_message(
                    payload,
                    fallback="后台任务没有被接纳。",
                ),
            )
        activity_id = str(payload.get("activity_id") or "").strip()
        if not activity_id:
            return _background_start_failed(
                command,
                tool_call=tool_call,
                safe_message="后台任务没有成功建立。",
            )
        prepared = executor.take_prepared_background_work(activity_id)
        if prepared is None:
            return _background_start_failed(
                command,
                tool_call=tool_call,
                safe_message="后台任务提交状态丢失。",
            )
        try:
            reservation = prepared.claim_reservation()
            action = reservation.action
        except Exception:
            return _background_start_failed(
                command,
                tool_call=tool_call,
                safe_message="后台任务提交状态无效。",
            )

        reason = self._stop_reason(
            command,
            cancellation,
            external_cancellation_event=external_cancellation_event,
        )
        if reason is not None:
            action.rollback()
            return self._failed(command, reason)
        try:
            if action.accept() is not True:
                action.rollback()
                return _background_start_failed(
                    command,
                    tool_call=tool_call,
                    safe_message="后台任务没有成功提交。",
                )
            # Background 事件必须复用普通 Foreground 工具完成事实的完整字段；
            # TaskResult、Runtime Memory 和 Notice 都只认这份 canonical 载荷，不能
            # 依赖 Recovery 自己的 outcome 文案猜测任务身份。
            result_payload = dict(payload)
            result_payload["interaction_session_id"] = command.interaction_session_id
            result_payload["recovery_of_activity_id"] = command.source_activity_id
            material_context = command.foreground_context.get(
                "active_turn_material_context"
            )
            turn_material_context_id = None
            if isinstance(material_context, dict):
                candidate_context_id = material_context.get("context_id")
                if not isinstance(candidate_context_id, str) or not candidate_context_id.strip():
                    candidate_context_id = material_context.get("id")
                if isinstance(candidate_context_id, str) and candidate_context_id.strip():
                    turn_material_context_id = candidate_context_id.strip()
            canonical_payload: dict[str, object] = {
                "interaction_session_id": command.interaction_session_id,
                "source_user_message_id": None,
                "tool_call_id": tool_call.tool_call_id,
                "protocol_transaction_id": (
                    f"self-reflection-{command.self_reflection_id}"
                ),
                "name": tool_call.name,
                "activity_id": activity_id,
                "state": "accepted",
                "request_id": command.source_request_id,
                "result": result_payload,
                "recovery_of_activity_id": command.source_activity_id,
                "self_reflection_id": command.self_reflection_id,
            }
            deliverable = payload.get("deliverable")
            if isinstance(deliverable, bool):
                canonical_payload["deliverable"] = deliverable
                result_payload["deliverable"] = deliverable
            if turn_material_context_id is not None:
                canonical_payload["turn_material_context_id"] = turn_material_context_id
                result_payload["turn_material_context_id"] = turn_material_context_id
            based_on_activity_id = payload.get("based_on_activity_id")
            if isinstance(based_on_activity_id, str) and based_on_activity_id.strip():
                canonical_payload["based_on_activity_id"] = based_on_activity_id.strip()
                result_payload["based_on_activity_id"] = based_on_activity_id.strip()
            canonical_event = RuntimeEvent(
                type="llm.foreground.tool_call_completed",
                source="assistant_runtime",
                payload=canonical_payload,
                produced_at=self._now(),
                runtime_epoch=command.runtime_epoch,
            )
            commit_owner_id = f"self-reflection:{command.self_reflection_id}"
            if not self._commit_barrier.enter(commit_owner_id):
                try:
                    action.rollback()
                except Exception:
                    pass
                return self._failed(command, "runtime_shutdown")
            with self._lock:
                self._background_commit_sections.add(command.self_reflection_id)
            try:
                try:
                    decision = self._tool_commit_broker.request_and_wait(
                        self_reflection_id=command.self_reflection_id,
                        source_activity_id=command.source_activity_id,
                        runtime_epoch=command.runtime_epoch,
                        tool_call_id=tool_call.tool_call_id,
                        tool_name=tool_call.name,
                        execution_channel="background",
                        canonical_event=canonical_event,
                        deadline=command.deadline,
                        cancellation_event=_CombinedCancellationSignal(
                            cancellation.event,
                            external_cancellation_event,
                        ),
                    )
                except Exception:
                    try:
                        action.rollback()
                    except Exception:
                        pass
                    return _background_start_failed(
                        command,
                        tool_call=tool_call,
                        safe_message="后台任务提交授权失败。",
                    )
                if not decision.approved:
                    try:
                        action.rollback()
                    except Exception:
                        pass
                    return self._failed(
                        command,
                        self._stop_reason(command, cancellation)
                        or decision.reason,
                    )
                if action.commit_enqueued() is not True:
                    return _background_start_failed(
                        command,
                        tool_call=tool_call,
                        safe_message="后台任务没有成功提交。",
                    )
                action.run_after_commit()
            finally:
                self._commit_barrier.leave(commit_owner_id)
                with self._lock:
                    self._background_commit_sections.discard(
                        command.self_reflection_id
                    )
        except Exception:
            action.rollback()
            return _background_start_failed(
                command,
                tool_call=tool_call,
                safe_message="后台任务提交失败。",
            )
        return _background_outcome_from_followup(
            command,
            tool_call=tool_call,
            activity_id=activity_id,
            followup_fact=action.after_commit_fact,
        )

    def _stop_reason(
        self,
        command: _RecoveryToolCommand,
        cancellation: _RecoveryCancellation,
        external_cancellation_event: Event | None = None,
    ) -> str | None:
        """统一读取 runtime 取消和绝对 deadline。

        Args:
            command: 带绝对截止时间的恢复命令。
            cancellation: 当前独立取消令牌。

        Returns:
            应立即失败关闭时的稳定原因，否则返回空。
        """

        if (
            external_cancellation_event is not None
            and external_cancellation_event.is_set()
        ):
            return str(
                getattr(external_cancellation_event, "reason", "")
                or "reflection_cancelled"
            )
        if cancellation.event.is_set():
            return cancellation.reason or "reflection_cancelled"
        if self._now() >= command.deadline:
            cancellation.cancel("reflection_deadline_exceeded")
            return "reflection_deadline_exceeded"
        return None
    def _failed(
        self,
        command: _RecoveryToolCommand,
        reason: str,
    ) -> SelfReflectionOutcomeFact:
        """构造与当前命令严格绑定的稳定失败事实。

        Args:
            command: 当前恢复命令。
            reason: 不含异常正文的稳定失败码。

        Returns:
            ``reflection_failed`` 结构化结果。
        """

        return SelfReflectionOutcomeFact.failed(
            self_reflection_id=command.self_reflection_id,
            source_activity_id=command.source_activity_id,
            reason=reason,
        )

    def _now(self) -> float:
        """读取并验证当前单调时钟。

        Returns:
            有限浮点单调时间。
        """

        value = self._monotonic_now()
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not isfinite(float(value))
        ):
            raise ValueError("monotonic_now 必须返回有限数字")
        return float(value)


class _CombinedCancellationSignal:
    """把 deadline 与 generation 取消合并成工具端口需要的最小信号。"""

    def __init__(self, primary: Event, secondary: Event | None) -> None:
        """保存两个只读取消来源。

        Args:
            primary: Recovery 绝对截止时间产生的取消信号。
            secondary: 当前可见 generation 的协作式取消信号。
        """

        self._primary = primary
        self._secondary = secondary

    def is_set(self) -> bool:
        """判断任一取消来源是否已经触发。

        Returns:
            任一信号已触发时返回真，否则返回假。
        """

        return self._primary.is_set() or (
            self._secondary is not None and self._secondary.is_set()
        )


def _inline_failure(
    command: _RecoveryToolCommand,
    *,
    tool_call: ForegroundToolCall,
    safe_message: str,
) -> SelfReflectionOutcomeFact:
    """构造一项不包含异常正文的 Inline 失败事实。

    Args:
        command: 当前恢复命令。
        tool_call: 实际尝试的安全 Inline 工具。
        safe_message: 可以交给最终表达的安全原因。

    Returns:
        ``inline_tool_failed`` 结果。
    """

    return SelfReflectionOutcomeFact(
        self_reflection_id=command.self_reflection_id,
        source_activity_id=command.source_activity_id,
        kind=SelfReflectionOutcomeKind.INLINE_TOOL_FAILED,
        tool_fact=SelfReflectionToolFact(
            tool_name=tool_call.name,
            status=SelfReflectionToolStatus.FAILED,
            safe_result=safe_message,
        ),
    )


def _background_rejected(
    command: _RecoveryToolCommand,
    *,
    tool_call: ForegroundToolCall,
    safe_message: str,
) -> SelfReflectionOutcomeFact:
    """构造 Background prepare 真实拒绝事实。

    Args:
        command: 当前恢复命令。
        tool_call: 实际尝试的后台工具。
        safe_message: canonical prepare 返回的安全原因。

    Returns:
        ``tool_rejected`` 结果。
    """

    return SelfReflectionOutcomeFact(
        self_reflection_id=command.self_reflection_id,
        source_activity_id=command.source_activity_id,
        kind=SelfReflectionOutcomeKind.TOOL_REJECTED,
        tool_fact=SelfReflectionToolFact(
            tool_name=tool_call.name,
            status=SelfReflectionToolStatus.REJECTED,
            safe_result=safe_message,
        ),
    )


def _background_start_failed(
    command: _RecoveryToolCommand,
    *,
    tool_call: ForegroundToolCall,
    safe_message: str,
) -> SelfReflectionOutcomeFact:
    """构造 Background 提交或启动失败事实。

    Args:
        command: 当前恢复命令。
        tool_call: 实际尝试的后台工具。
        safe_message: 可以交给最终表达的安全原因。

    Returns:
        ``activity_start_failed`` 结果。
    """

    return SelfReflectionOutcomeFact(
        self_reflection_id=command.self_reflection_id,
        source_activity_id=command.source_activity_id,
        kind=SelfReflectionOutcomeKind.ACTIVITY_START_FAILED,
        tool_fact=SelfReflectionToolFact(
            tool_name=tool_call.name,
            status=SelfReflectionToolStatus.START_FAILED,
            safe_result=safe_message,
        ),
    )


def _background_outcome_from_followup(
    command: _RecoveryToolCommand,
    *,
    tool_call: ForegroundToolCall,
    activity_id: str,
    followup_fact: object,
) -> SelfReflectionOutcomeFact:
    """把 after_commit 事实映射成恢复结果，不从 prepare 文案猜状态。

    Args:
        command: 当前恢复命令。
        tool_call: 已提交的后台工具调用。
        activity_id: canonical prepare 建立的 Activity 编号。
        followup_fact: 执行通道返回的真实提交后事实。

    Returns:
        已开始或启动失败结果。
    """

    kind = str(getattr(followup_fact, "kind", "") or "").strip()
    fact_activity_id = str(
        getattr(followup_fact, "activity_id", "") or activity_id
    ).strip()
    if kind == "activity_started":
        return SelfReflectionOutcomeFact(
            self_reflection_id=command.self_reflection_id,
            source_activity_id=command.source_activity_id,
            kind=SelfReflectionOutcomeKind.ACTIVITY_STARTED,
            tool_fact=SelfReflectionToolFact(
                tool_name=tool_call.name,
                status=SelfReflectionToolStatus.STARTED,
                # 安全说明会被闭环轮原文引用；保持第一人称语义，不给模型递
                # "执行器""后台"这类会被鹦鹉学舌进可见正文的词。
                safe_result="这项工作已经真实开始。",
                activity_id=fact_activity_id,
            ),
        )
    return _background_start_failed(
        command,
        tool_call=tool_call,
        safe_message=(
            str(getattr(followup_fact, "safe_message", "") or "").strip()
            or "这项工作没有启动。"
        ),
    )


def _safe_inline_result(payload: dict[str, object]) -> str:
    """把静态安全 Inline 载荷转换成有界自然事实。

    Args:
        payload: 工具执行器已经校验的结果字典。

    Returns:
        不超过 20000 字符的 JSON 文本；超限时使用安全失败说明。
    """

    rendered = dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    if len(rendered) <= 20_000:
        return rendered
    return "工具已经返回结果，但结果过长，无法安全用于本次表达。"


def _safe_message(payload: dict[str, object], *, fallback: str) -> str:
    """读取工具提供的安全说明，缺失时使用固定兜底。

    Args:
        payload: 工具失败或拒绝载荷。
        fallback: 没有合法安全说明时的固定文本。

    Returns:
        去除首尾空白后的安全说明。
    """

    value = payload.get("safe_message")
    if isinstance(value, str) and value.strip():
        return value.strip()[:2_000]
    return fallback
