from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from threading import Event
from time import monotonic

from companion.foreground_tools.orchestration.contracts import (
    BackgroundActivityPrepared,
    DeferredActionPrepared,
    ExecutedToolCall,
    ForegroundToolExecution,
    InlineToolCompleted,
    ToolFailed,
    ToolOutcome,
    ToolRejected,
    ToolReplyRequirement,
    tool_outcome_from_payload,
)
from companion.foreground_tools.inline_execution import InlineExecutionContext
from companion.llm.foreground_models import ForegroundToolCall
from companion.materials.foreground_images import FrozenVisualInput
from companion.observability.logging_config import get_backend_logger


RawToolPayload = dict[str, object]


@dataclass(frozen=True)
class InlineToolResult:
    """保存 Inline 工具的公开载荷和只在本轮继续使用的临时视觉输入。"""

    payload: RawToolPayload
    mounted_visual_input: FrozenVisualInput = FrozenVisualInput()


ToolPredicate = Callable[[str], bool]
ExecuteTool = Callable[[ForegroundToolCall], RawToolPayload]
ExecuteInlineTool = Callable[
    [ForegroundToolCall, InlineExecutionContext],
    RawToolPayload | InlineToolResult,
]
ExecuteCognitiveCalls = Callable[
    [tuple[ForegroundToolCall, ...]],
    tuple[RawToolPayload, ...],
]
InlineTimeoutResolver = Callable[[str], float | None]

logger = get_backend_logger(__name__)


@dataclass(frozen=True)
class _InlineToolOutcome:
    """保存 Inline 工具的稳定结果和真实完成时刻。

    Args:
        outcome: 明确的 Inline 完成、拒绝或失败结果。
        completed_at: 工具函数返回或抛错时的单调时钟时间。
    """

    outcome: ToolOutcome
    completed_at: float


@dataclass
class PreparedForegroundToolExecution:
    """保存后台准备结果和已经启动的 Inline future。

    Args:
        tool_calls: 模型返回的原始工具调用。
        prepared_by_index: TaskResult 与 Cognitive 在 prepare 阶段得到的结果。
        inline_calls: 需要在当前轮收集结果的 Inline 调用。
        inline_futures: 已经并发启动的 Inline future。
        inline_deadlines: Inline future 提交时确定的绝对截止时间。
        inline_execution_contexts: 工作线程与收集方共享的截止时间和取消门禁。
        cancelled_before_commit: prepare 是否在后台任务提交前观察到 generation 取消。
    """

    tool_calls: tuple[ForegroundToolCall, ...]
    prepared_by_index: dict[int, ExecutedToolCall]
    inline_calls: tuple[tuple[int, ForegroundToolCall], ...]
    inline_futures: dict[int, Future[_InlineToolOutcome]]
    inline_deadlines: dict[int, float | None]
    inline_execution_contexts: dict[int, InlineExecutionContext]
    cancelled_before_commit: bool = False

    @property
    def prepared_calls(self) -> tuple[ExecutedToolCall, ...]:
        """按模型原始顺序返回 prepare 阶段已经确定的调用。"""

        return tuple(
            self.prepared_by_index[index]
            for index in sorted(self.prepared_by_index)
        )

    def collect(
        self,
        *,
        cancellation_event: Event | None = None,
    ) -> ForegroundToolExecution:
        """等待 Inline 结果并恢复完整的模型调用顺序。

        Args:
            cancellation_event: 当前 generation 的协作式取消信号。

        Returns:
            与原始调用顺序严格一致的完整工具执行结果。
        """

        if self.cancelled_before_commit:
            raise RuntimeError("已经取消的工具准备不能收集结果")

        result_by_index = dict(self.prepared_by_index)
        # 按 prepare 阶段提交的顺序依次收集 Inline future，确保结果按原始下标回填。
        for index, call in self.inline_calls:
            outcome = _collect_inline_result(
                future=self.inline_futures[index],
                call=call,
                deadline=self.inline_deadlines[index],
                cancellation_event=cancellation_event,
                execution_context=self.inline_execution_contexts[index],
            )
            result_by_index[index] = ExecutedToolCall(
                call=call,
                outcome=outcome,
                reply_requirement=_inline_reply_requirement(outcome),
                channel="inline",
            )

        return ForegroundToolExecution(
            calls=tuple(result_by_index[index] for index in range(len(self.tool_calls)))
        )

    def cancel_inline(self) -> None:
        """取消待运行 Future，并通知运行中工具停止后续副作用。"""

        for execution_context in self.inline_execution_contexts.values():
            execution_context.cancel()
        for future in self.inline_futures.values():
            future.cancel()


class ForegroundToolCallOrchestrator:
    """按后台任务、延迟动作和 Inline 通道准备一轮工具调用。"""

    def __init__(self, *, max_inline_workers: int = 4) -> None:
        """初始化多工具编排器。

        Args:
            max_inline_workers: 同一时刻最多执行的 Inline 工具数量。
        """

        if max_inline_workers <= 0:
            raise ValueError("Inline 工具并发数必须大于零")
        self._inline_executor = ThreadPoolExecutor(
            max_workers=max_inline_workers,
            thread_name_prefix="foreground-inline-tool",
        )

    def prepare(
        self,
        *,
        tool_calls: list[ForegroundToolCall],
        is_cognitive_tool: ToolPredicate,
        is_task_result_tool: ToolPredicate,
        execute_cognitive_calls: ExecuteCognitiveCalls,
        execute_task_result_call: ExecuteTool,
        execute_inline_call: ExecuteInlineTool,
        is_deferred_action_tool: ToolPredicate | None = None,
        execute_deferred_action_call: ExecuteTool | None = None,
        inline_timeout_resolver: InlineTimeoutResolver | None = None,
        cancellation_event: Event | None = None,
    ) -> PreparedForegroundToolExecution:
        """准备后台调用并立即启动全部 Inline future。

        把模型一轮返回的工具分成 Cognitive、TaskResult、延迟动作和 Inline 四条通道：
        - Inline 工具立即进线程池，和后台任务准备并行跑；
        - Cognitive 工具整批提交，合并成后台认知任务；
        - TaskResult 工具只取第一个建主任务，其余重复调用静默忽略。
        - 延迟动作只占用已有资源，成功入队后才真正提交副作用。
        最终返回一个对象：后台结果已经归档，Inline future 已经开跑但还没收结果。

        Args:
            tool_calls: Foreground 模型返回的原始工具调用。
            is_cognitive_tool: 判断工具是否属于认知意图通道。
            is_task_result_tool: 判断工具是否属于 TaskResult 通道。
            execute_cognitive_calls: 一次接收全部 Cognitive 调用的准备入口。
            execute_task_result_call: 准备本轮唯一 TaskResult 调用的入口。
            execute_inline_call: 执行单个 Inline 调用的入口。
            is_deferred_action_tool: 判断工具是否属于已有资源上的延迟动作；没有时为空。
            execute_deferred_action_call: 准备一个不创建 Activity 的延迟动作；没有时为空。
            inline_timeout_resolver: 按工具名返回执行时限；空值表示依赖工具自身时限。
            cancellation_event: 当前 generation 的协作式取消信号。

        Returns:
            可先交付后台接受事件、随后收集 Inline 结果的准备对象。
        """

        if is_deferred_action_tool is not None and execute_deferred_action_call is None:
            raise ValueError("延迟动作分类器和执行入口必须同时提供")
        deferred_action_predicate = is_deferred_action_tool or (lambda _name: False)

        # 给每个调用保留原始下标，后续并发、分流、合并之后还能按模型顺序装回去。
        indexed_calls = tuple(enumerate(tool_calls))
        cognitive_calls = tuple(
            (index, call)
            for index, call in indexed_calls
            if is_cognitive_tool(call.name)
        )
        task_result_calls = tuple(
            (index, call)
            for index, call in indexed_calls
            if is_task_result_tool(call.name)
        )
        deferred_action_calls = tuple(
            (index, call)
            for index, call in indexed_calls
            if deferred_action_predicate(call.name)
        )
        inline_calls = tuple(
            (index, call)
            for index, call in indexed_calls
            if not is_cognitive_tool(call.name)
            and not is_task_result_tool(call.name)
            and not deferred_action_predicate(call.name)
        )

        # Inline 先进入线程池，使其等待外部服务的时间与后台任务准备阶段重叠。
        # 每个调用的预算从提交时开始，避免按结果收集顺序重新获得一整段超时时间。
        timeout_resolver = inline_timeout_resolver or (lambda _name: 1.0)
        inline_futures: dict[int, Future[_InlineToolOutcome]] = {}
        inline_deadlines: dict[int, float | None] = {}
        inline_execution_contexts: dict[int, InlineExecutionContext] = {}
        submitted_inline_calls: list[tuple[int, ForegroundToolCall]] = []
        result_by_index: dict[int, ExecutedToolCall] = {}

        def finish_preparation(*, cancelled: bool) -> PreparedForegroundToolExecution:
            """把当前已归档的结果和已提交的 Inline future 封装成准备对象。

            调用点很多：每个可能取消的位置都要立刻收口，防止后面继续提交新任务。
            """

            if cancelled:
                for execution_context in inline_execution_contexts.values():
                    execution_context.cancel()
                for future in inline_futures.values():
                    future.cancel()
            return PreparedForegroundToolExecution(
                tool_calls=tuple(tool_calls),
                prepared_by_index=dict(result_by_index),
                inline_calls=tuple(submitted_inline_calls),
                inline_futures=dict(inline_futures),
                inline_deadlines=dict(inline_deadlines),
                inline_execution_contexts=dict(inline_execution_contexts),
                cancelled_before_commit=cancelled,
            )

        # 一上来就发现 generation 已被取消，直接返回，不启动任何任务。
        if cancellation_event is not None and cancellation_event.is_set():
            return finish_preparation(cancelled=True)

        for index, call in inline_calls:
            # 每提交一个 Inline 前都检查取消：如果已经取消，已提交的部分仍保留，未提交的部分不再继续。
            if cancellation_event is not None and cancellation_event.is_set():
                return finish_preparation(cancelled=True)
            timeout = timeout_resolver(call.name)
            submitted_at = monotonic()
            deadline = submitted_at + timeout if timeout is not None else None
            execution_context = InlineExecutionContext(
                completion_deadline=deadline,
                generation_cancellation_event=cancellation_event,
            )
            inline_execution_contexts[index] = execution_context
            inline_futures[index] = self._inline_executor.submit(
                _execute_inline_call,
                execute_inline_call,
                call,
                execution_context,
            )
            submitted_inline_calls.append((index, call))
            # 截止时间在提交瞬间就固定，避免 collect 阶段因为排队晚而白赚时间。
            inline_deadlines[index] = deadline
            # 提交完最后一个也检查一次，确保刚提交完就取消时同样能立即收口。
            if cancellation_event is not None and cancellation_event.is_set():
                return finish_preparation(cancelled=True)

        for index, call in deferred_action_calls:
            if cancellation_event is not None and cancellation_event.is_set():
                return finish_preparation(cancelled=True)
            try:
                if execute_deferred_action_call is None:
                    raise RuntimeError("延迟动作执行入口缺失")
                deferred_result = execute_deferred_action_call(call)
            except Exception:
                if cancellation_event is not None and cancellation_event.is_set():
                    return finish_preparation(cancelled=True)
                logger.exception(
                    "延迟动作准备失败 tool_call_id=%s",
                    call.tool_call_id,
                )
                deferred_result = {
                    "state": "failed",
                    "reason": "deferred_action_preparation_failed",
                    "safe_message": "这次补充信息没有成功提交。",
                }
            deferred_outcome = tool_outcome_from_payload(
                deferred_result,
                channel="deferred",
            )
            result_by_index[index] = ExecutedToolCall(
                call=call,
                outcome=deferred_outcome,
                reply_requirement=_deferred_reply_requirement(deferred_outcome),
                channel="deferred",
            )
            if cancellation_event is not None and cancellation_event.is_set():
                return finish_preparation(cancelled=True)

        if cognitive_calls:
            if cancellation_event is not None and cancellation_event.is_set():
                return finish_preparation(cancelled=True)
            # 同一轮认知调用要作为一批一起准备，这样才能合并成统一的后台认知任务。
            raw_calls = tuple(call for _, call in cognitive_calls)
            try:
                cognitive_results = execute_cognitive_calls(raw_calls)
                if cancellation_event is not None and cancellation_event.is_set():
                    # 取消后 cognitive 可能已经返回了 accepted/merged 结果；这些结果需要归档，
                    # 上层会按 activity_id 在后续做回滚，不能直接丢弃。
                    for (index, call), result in zip(
                        cognitive_calls,
                        cognitive_results,
                    ):
                        outcome = tool_outcome_from_payload(
                            result,
                            channel="background",
                        )
                        result_by_index[index] = ExecutedToolCall(
                            call=call,
                            outcome=outcome,
                            reply_requirement=_background_reply_requirement(outcome),
                            channel="background",
                        )
                    return finish_preparation(cancelled=True)
                if len(cognitive_results) != len(raw_calls):
                    raise RuntimeError("Cognitive 执行结果数量必须与原始调用数量一致")
            except Exception:
                # 取消期间抛异常，直接收口；非取消期间抛异常，则把整批 Cognitive 标记为失败，
                # 让后续流程按失败解释而不是把整个 prepare 阶段炸掉。
                if cancellation_event is not None and cancellation_event.is_set():
                    return finish_preparation(cancelled=True)
                logger.exception("Cognitive 工具批次准备失败")
                cognitive_results = tuple(
                    {
                        "state": "failed",
                        "reason": "cognitive_tool_preparation_failed",
                        "safe_message": "认知任务没有启动。",
                    }
                    for _ in raw_calls
                )
            for (index, call), result in zip(
                cognitive_calls,
                cognitive_results,
                strict=True,
            ):
                # 即便多条认知调用合并到同一个后台任务，这里也要按每个原始调用回填结果，保证顺序不乱。
                outcome = tool_outcome_from_payload(result, channel="background")
                result_by_index[index] = ExecutedToolCall(
                    call=call,
                    outcome=outcome,
                    reply_requirement=_background_reply_requirement(outcome),
                    channel="background",
                )

        if task_result_calls:
            if cancellation_event is not None and cancellation_event.is_set():
                return finish_preparation(cancelled=True)
            # 后台工作一轮只允许一个主调用：第一个真正建任务，后面的同类调用直接静默忽略。
            primary_index, primary_task_result = task_result_calls[0]
            try:
                primary_result = execute_task_result_call(primary_task_result)
            except Exception:
                # 取消期间异常直接收口；非取消期间异常则把主后台工作调用标记为失败，流程继续。
                if cancellation_event is not None and cancellation_event.is_set():
                    return finish_preparation(cancelled=True)
                logger.exception(
                    "TaskResult 工具准备失败 tool_call_id=%s",
                    primary_task_result.tool_call_id,
                )
                primary_result = {
                    "state": "failed",
                    "reason": "task_result_tool_preparation_failed",
                    "safe_message": "后台工作没有启动。",
                }
            primary_outcome = tool_outcome_from_payload(
                primary_result,
                channel="background",
            )
            result_by_index[primary_index] = ExecutedToolCall(
                call=primary_task_result,
                outcome=primary_outcome,
                reply_requirement=_background_reply_requirement(primary_outcome),
                channel="background",
            )
            if cancellation_event is not None and cancellation_event.is_set():
                return finish_preparation(cancelled=True)
            for index, call in task_result_calls[1:]:
                # 重复的 task_result 调用不再起第二个任务，也不提示用户“你重复调了”，避免制造无意义噪音。
                result_by_index[index] = ExecutedToolCall(
                    call=call,
                    outcome=ToolRejected(
                        state="ignored_duplicate",
                        reason="duplicate_tool_call",
                        safe_message="",
                        payload={
                            "state": "ignored_duplicate",
                            "reason": "duplicate_tool_call",
                        },
                    ),
                    reply_requirement=ToolReplyRequirement.SILENT,
                    channel="background",
                )

        # prepare 阶段只返回“已经确定的后台结果”和“已经开跑的 inline future”；
        # 真正等待 inline 完成，要到后面的 collect 再做。
        return finish_preparation(cancelled=False)

    def shutdown(self, timeout: float = 1.0) -> None:
        """关闭 Inline 工具共享线程池。

        Args:
            timeout: 为统一运行时关闭接口保留的超时参数。
        """

        del timeout
        # Python 无法安全终止正在运行的线程；关闭阶段取消未启动任务，运行中任务由自身超时收口。
        self._inline_executor.shutdown(wait=False, cancel_futures=True)


def _collect_inline_result(
    *,
    future: Future[_InlineToolOutcome],
    call: ForegroundToolCall,
    deadline: float | None,
    cancellation_event: Event | None,
    execution_context: InlineExecutionContext,
) -> ToolOutcome:
    """可取消地等待单个 Inline future。

    Args:
        future: 已经启动的 Inline future。
        call: 对应的原始工具调用。
        deadline: future 提交时确定的绝对截止时间；空值表示只依赖工具自身超时。
        cancellation_event: 当前 generation 的协作式取消信号。
        execution_context: 与工作线程共享的取消与截止时间门禁。

    Returns:
        工具结果，或不暴露内部异常的稳定失败载荷。
    """

    while True:
        if cancellation_event is not None and cancellation_event.is_set():
            execution_context.cancel()
            future.cancel()
            return ToolFailed(
                reason="generation_cancelled",
                safe_message="当前回复已经取消，即时查询结果不会继续使用。",
                payload={
                    "state": "failed",
                    "reason": "generation_cancelled",
                    "safe_message": "当前回复已经取消，即时查询结果不会继续使用。",
                },
            )

        # 原始顺序可能让已完成 Future 晚一点才被读取，必须按真实完成时刻判断是否超时。
        if future.done():
            try:
                return _resolve_inline_outcome(future.result(), deadline=deadline)
            except Exception:
                return _inline_future_failed_result(call)

        wait_timeout = 0.05
        if deadline is not None:
            remaining = deadline - monotonic()
            if remaining <= 0:
                execution_context.cancel()
                future.cancel()
                return _inline_timeout_result()
            wait_timeout = min(wait_timeout, remaining)

        try:
            outcome = future.result(timeout=wait_timeout)
            return _resolve_inline_outcome(outcome, deadline=deadline)
        except FutureTimeoutError:
            continue
        except Exception:
            return _inline_future_failed_result(call)


def _execute_inline_call(
    execute: ExecuteInlineTool,
    call: ForegroundToolCall,
    execution_context: InlineExecutionContext,
) -> _InlineToolOutcome:
    """执行单个 Inline 工具，并在函数真正结束时冻结完成时间。

    Args:
        execute: 项目提供的 Inline 工具执行入口。
        call: 当前原始工具调用。
        execution_context: 从提交时开始计时并可被收集方取消的执行门禁。

    Returns:
        带真实完成时刻的成功或脱敏失败结果。
    """

    try:

        result = execute(call, execution_context)
    except Exception:
        completed_at = monotonic()
        logger.exception(
            "Inline 工具执行失败 tool_call_id=%s name=%s",
            call.tool_call_id,
            call.name,
        )
        return _InlineToolOutcome(
            outcome=ToolFailed(
                reason="inline_tool_execution_failed",
                safe_message="这项即时查询失败。",
                payload={
                    "state": "failed",
                    "reason": "inline_tool_execution_failed",
                    "safe_message": "这项即时查询失败。",
                },
            ),
            completed_at=completed_at,
        )
    mounted_visual_input = FrozenVisualInput()
    payload: RawToolPayload
    if isinstance(result, InlineToolResult):
        payload = result.payload
        mounted_visual_input = result.mounted_visual_input
    else:
        payload = result
    return _InlineToolOutcome(
        outcome=tool_outcome_from_payload(
            payload,
            channel="inline",
            mounted_visual_input=mounted_visual_input,
        ),
        completed_at=monotonic(),
    )


def _resolve_inline_outcome(
    outcome: _InlineToolOutcome,
    *,
    deadline: float | None,
) -> ToolOutcome:
    """按真实完成时刻判定 Inline 结果是否超过执行预算。

    Args:
        outcome: 工具线程冻结的执行结果和完成时刻。
        deadline: future 提交时确定的绝对截止时间。

    Returns:
        按时完成的原始结果，或稳定超时结果。
    """

    if deadline is not None and outcome.completed_at > deadline:
        return _inline_timeout_result()
    return outcome.outcome


def _inline_timeout_result() -> ToolFailed:
    """返回不暴露内部细节的 Inline 超时结果。"""

    return ToolFailed(
        reason="inline_tool_timeout",
        safe_message="这项即时查询超时，请稍后再试。",
        payload={
            "state": "failed",
            "reason": "inline_tool_timeout",
            "safe_message": "这项即时查询超时，请稍后再试。",
        },
    )


def _inline_future_failed_result(call: ForegroundToolCall) -> ToolFailed:
    """记录 Future 层异常并返回稳定失败结果。

    Args:
        call: 发生异常的原始工具调用。

    Returns:
        不包含内部异常正文的失败结果。
    """

    logger.exception(
        "Inline Future 读取失败 tool_call_id=%s name=%s",
        call.tool_call_id,
        call.name,
    )
    return ToolFailed(
        reason="inline_tool_execution_failed",
        safe_message="这项即时查询失败。",
        payload={
            "state": "failed",
            "reason": "inline_tool_execution_failed",
            "safe_message": "这项即时查询失败。",
        },
    )


def _background_reply_requirement(outcome: ToolOutcome) -> ToolReplyRequirement:
    """根据后台准备结果确定用户回复要求。

    Args:
        outcome: TaskResult 或 Cognitive 的明确准备结果。

    Returns:
        成功接收需要确认，其它状态需要解释。
    """

    return (
        ToolReplyRequirement.ACKNOWLEDGE
        if isinstance(outcome, BackgroundActivityPrepared)
        else ToolReplyRequirement.EXPLAIN
    )


def _deferred_reply_requirement(outcome: ToolOutcome) -> ToolReplyRequirement:
    """根据已有资源上的动作结果确定用户回复要求。

    Args:
        outcome: 延迟动作的明确准备结果。

    Returns:
        成功提交需要自然确认，拒绝或失败需要解释。
    """

    return (
        ToolReplyRequirement.ACKNOWLEDGE
        if isinstance(outcome, DeferredActionPrepared)
        else ToolReplyRequirement.EXPLAIN
    )


def _inline_reply_requirement(outcome: ToolOutcome) -> ToolReplyRequirement:
    """根据 Inline 执行结果确定用户回复要求。

    Args:
        outcome: Inline 工具的当前轮结果。

    Returns:
        只有明确完成的结果可以展示，其它状态均按失败解释。
    """

    if isinstance(outcome, InlineToolCompleted):
        return ToolReplyRequirement.PRESENT_RESULT
    return ToolReplyRequirement.EXPLAIN
