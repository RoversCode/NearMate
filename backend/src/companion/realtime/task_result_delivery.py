"""协调任务结果交付边界与 turn 材料租约。"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from companion.observability.logging_config import get_backend_logger
from companion.realtime.assistant_events import RuntimeEvent


logger = get_backend_logger(__name__)

_DEFAULT_RECENT_IDENTITY_LIMIT = 1024
_TaskResultActivityKey = tuple[int, str]
_TaskResultDeliverySource = Literal["background_agent"]
_TASK_RESULT_RESULT_CARD_EVENTS = frozenset(
    {
        "task_result.completed",
        "task_result.failed",
        "task_result.cancelled",
    }
)


@dataclass(frozen=True, slots=True)
class TaskResultTerminalDelivery:
    """保存已经越过交付边界的 Activity 终态。

    Args:
        activity_key: 运行时世代与 Activity 编号组成的稳定身份。
        delivery_token: 本次交付领取凭据；运行时中断后立即失效。
        source_event: 用于 Notice 和材料收口的原始 Activity 终态。
        public_event: 需要发送给 Desktop 的结果卡事件；无需展示成果时为空。
    """

    activity_key: _TaskResultActivityKey
    delivery_token: int
    source_event: RuntimeEvent
    public_event: RuntimeEvent | None


@dataclass(frozen=True, slots=True)
class TaskResultAcceptanceTrackingResult:
    """描述一次工具 acceptance 是否建立了可交付任务与材料租约。

    Args:
        accepted: 事件身份有效，且已经绑定到目标 Background Activity 时为真。
        first_acceptance: 本次是否首次绑定该 Activity 的 acceptance。
        material_context_id: 本次 acceptance 声明的材料上下文编号；无材料时为空。
        material_retained: Background Activity 是否已经持有这份材料自己的租约。
        terminal_delivery: 本次处理后刚好满足全部条件的权威终态。
    """

    accepted: bool
    first_acceptance: bool
    material_context_id: str
    material_retained: bool
    terminal_delivery: TaskResultTerminalDelivery | None = None


@dataclass(frozen=True, slots=True)
class _TaskResultAcceptanceIdentity:
    """保存首次工具接受事件绑定的不可变身份。

    Args:
        acknowledgement_id: 发起 Activity 的 Foreground 请求编号。
        material_context_id: Activity 读取的可选 turn 材料上下文编号。
        source: Activity 的执行来源。
        deliverable: 是否需要登记并展示交付结果。
        based_on_activity_id: 纠正任务父 Activity 编号；普通派生不在这里继承租约。
    """

    acknowledgement_id: str
    material_context_id: str
    source: _TaskResultDeliverySource
    deliverable: bool
    based_on_activity_id: str = ""


@dataclass(slots=True)
class _TaskResultDeliveryState:
    """保存单个 TaskResult 从启动到公开终态的交付状态。

    Args:
        activity_key: 该状态所属的运行时世代与 Activity 编号。
        started: 是否已经收到合法的公开启动事实。
        started_source: 启动事实所属执行来源。
        acceptance: 首次工具接受事件绑定的不可变身份。
        acknowledged: 发起任务的 Foreground 是否已经结束当前回复。
        pending_terminal: 首条权威终态。
        delivery_claimed: 权威终态是否已经交给事件 Handler 交付。
        result_card_broadcasted: 结果卡是否已经成功发送到实时控制通道。
        closing: 是否已经关闭交付资格并正在释放材料。
        attempted_material_context_ids: 已经尝试取得租约的材料上下文编号。
        retained_material_context_ids: 实际取得租约的全部材料上下文编号。
        pending_release_events: 部分材料释放成功但尚未交给调用方的事件。
    """

    activity_key: _TaskResultActivityKey | None = None
    started: bool = False
    started_source: _TaskResultDeliverySource | None = None
    execution_phase: str | None = None
    acceptance: _TaskResultAcceptanceIdentity | None = None
    acknowledged: bool = False
    pending_terminal: TaskResultTerminalDelivery | None = None
    delivery_claimed: bool = False
    delivery_token: int = 0
    result_card_broadcasted: bool = False
    closing: bool = False
    attempted_material_context_ids: set[str] = field(default_factory=set)
    retained_material_context_ids: list[str] = field(default_factory=list)
    pending_release_events: list[dict[str, object]] = field(default_factory=list)


class TaskResultDeliveryCoordinator:
    """维护 TaskResult 启动证明、确认边界、权威终态与材料租约。"""

    def __init__(
        self,
        *,
        retain_material: Callable[[str], bool] | None = None,
        release_material: Callable[[str], list[dict[str, object]]] | None = None,
        recent_identity_limit: int = _DEFAULT_RECENT_IDENTITY_LIMIT,
    ) -> None:
        """初始化 TaskResult 交付状态机。

        Args:
            retain_material: 工具接受后取得 turn 材料租约的函数。
            release_material: 任务终态后释放 turn 材料租约的函数。
            recent_identity_limit: 最近确认请求和关闭任务的有界记录数量。
        """

        if recent_identity_limit <= 0:
            raise ValueError("recent_identity_limit 必须大于零")
        self._retain_material = retain_material
        self._release_material = release_material
        self._recent_identity_limit = recent_identity_limit
        self._deliveries: dict[_TaskResultActivityKey, _TaskResultDeliveryState] = {}
        self._epoch_release_events: dict[int, list[dict[str, object]]] = {}
        self._acknowledged_requests: OrderedDict[tuple[int, str], None] = OrderedDict()
        # 结果卡是已经完成过公开交付的 UI 状态快照，只保留本次 Backend 进程最近几项。
        self._committed_result_cards: OrderedDict[
            _TaskResultActivityKey, RuntimeEvent
        ] = OrderedDict()
        # 已关闭 Activity 墓碑阻止迟到 started、acceptance 或 terminal 重新取得材料租约。
        self._closed_activities: OrderedDict[_TaskResultActivityKey, None] = OrderedDict()
        self._next_delivery_token = 0

    def register_started(
        self,
        event: RuntimeEvent,
    ) -> tuple[bool, TaskResultTerminalDelivery | None]:
        """在公开 started 前登记任务来源证明并重新求值交付条件。

        Args:
            event: 后台工作的 started 事件。

        Returns:
            是否应公开本次 started，以及可能刚刚满足条件的权威终态。
        """

        activity_key = _task_result_activity_key(event)
        source = _delivery_source_from_event(event.type)
        if activity_key is None or source is None or activity_key in self._closed_activities:
            return False, None
        state = self._deliveries.setdefault(
            activity_key,
            _TaskResultDeliveryState(activity_key=activity_key),
        )
        if state.closing:
            return False, None
        if state.started:
            if state.started_source != source:
                logger.warning(
                    "拒绝冲突交付 started runtime_epoch=%s activity_id=%s "
                    "existing_source=%s incoming_source=%s",
                    activity_key[0],
                    activity_key[1],
                    state.started_source,
                    source,
                )
            return False, None
        state.started = True
        state.started_source = source
        return True, self._claim_ready_terminal(state)

    def track_tool_acceptance(
        self,
        event: RuntimeEvent,
    ) -> TaskResultAcceptanceTrackingResult:
        """绑定首次工具接受身份、取得一次材料租约并重新求值终态。

        Args:
            event: 带运行时世代的 Foreground 工具完成事件。

        Returns:
            acceptance 是否有效、材料是否真实 retain，以及可能刚满足条件的终态。
        """

        payload = event.payload
        source_and_deliverable = _acceptance_delivery_identity(payload)
        if payload.get("state") != "accepted" or source_and_deliverable is None:
            return _rejected_acceptance_tracking_result()
        source, deliverable, inherits_parent_materials = source_and_deliverable
        activity_key = _task_result_activity_key(event)
        acknowledgement_id = _clean_text(payload.get("request_id"))
        if activity_key is None or not acknowledgement_id:
            return _rejected_acceptance_tracking_result()
        if activity_key in self._closed_activities:
            logger.info(
                "忽略已关闭 TaskResult 的迟到 acceptance runtime_epoch=%s activity_id=%s",
                activity_key[0],
                activity_key[1],
            )
            return _rejected_acceptance_tracking_result()

        acceptance = _TaskResultAcceptanceIdentity(
            acknowledgement_id=acknowledgement_id,
            material_context_id=_clean_text(payload.get("turn_material_context_id")),
            source=source,
            deliverable=deliverable,
            based_on_activity_id=(
                _clean_text(payload.get("based_on_activity_id"))
                if inherits_parent_materials
                else ""
            ),
        )
        state = self._deliveries.setdefault(
            activity_key,
            _TaskResultDeliveryState(activity_key=activity_key),
        )
        if state.closing:
            return _rejected_acceptance_tracking_result()
        if state.acceptance is not None:
            if state.acceptance != acceptance:
                logger.warning(
                    "拒绝冲突 TaskResult acceptance runtime_epoch=%s activity_id=%s "
                    "existing_request_id=%s incoming_request_id=%s "
                    "existing_context_id=%s incoming_context_id=%s",
                    activity_key[0],
                    activity_key[1],
                    state.acceptance.acknowledgement_id,
                    acceptance.acknowledgement_id,
                    state.acceptance.material_context_id,
                    acceptance.material_context_id,
                )
                return _rejected_acceptance_tracking_result()
            return self._acceptance_tracking_result(
                state,
                first_acceptance=False,
            )

        state.acceptance = acceptance
        state.acknowledged = (
            activity_key[0],
            acceptance.acknowledgement_id,
        ) in self._acknowledged_requests
        self._retain_state_material(state)
        self._retain_parent_state_material(
            activity_key=activity_key,
            state=state,
        )
        return self._acceptance_tracking_result(
            state,
            first_acceptance=True,
        )

    def _acceptance_tracking_result(
        self,
        state: _TaskResultDeliveryState,
        *,
        first_acceptance: bool,
    ) -> TaskResultAcceptanceTrackingResult:
        """冻结 acceptance 处理结果，避免调用方重新推断租约状态。

        Args:
            state: 已经绑定 acceptance 的 TaskResult 状态。
            first_acceptance: 本次是否首次建立 acceptance 身份。

        Returns:
            包含材料 retain 证明和可能终态的不可变结果。
        """

        acceptance = state.acceptance
        if acceptance is None:
            return _rejected_acceptance_tracking_result()
        material_context_id = acceptance.material_context_id
        return TaskResultAcceptanceTrackingResult(
            accepted=True,
            first_acceptance=first_acceptance,
            material_context_id=material_context_id,
            material_retained=(
                bool(material_context_id)
                and material_context_id in state.retained_material_context_ids
            ),
            terminal_delivery=self._claim_ready_terminal(state),
        )

    def track_supplemental_material(self, event: RuntimeEvent) -> bool:
        """为已存在的后台 Activity 追加保留一轮回答材料。

        Args:
            event: 后台真实接收 `respond_to_work_interaction` 后的权威回执事件。

        Returns:
            事件身份有效并已完成幂等处理时返回真。
        """

        payload = event.payload
        if (
            payload.get("name") != "respond_to_work_interaction"
            or payload.get("state") != "accepted"
        ):
            return False
        activity_key = _task_result_activity_key(event)
        if activity_key is None or activity_key in self._closed_activities:
            return False
        state = self._deliveries.get(activity_key)
        if state is None:
            logger.warning(
                "忽略没有交付状态的补充材料 runtime_epoch=%s activity_id=%s",
                activity_key[0],
                activity_key[1],
            )
            return False
        if state.closing or (state.acceptance is not None and state.acceptance.source != "background_agent"):
            return False
        context_id = _clean_text(payload.get("turn_material_context_id"))
        if context_id:
            self._retain_material_context(state, context_id)
        return True

    def adopt_material_lease(self, event: RuntimeEvent, context_id: str) -> bool:
        """让已绑定的 TaskResult 接管调用方现存的一份材料租约。

        本方法不会调用 retain。它只在 acceptance 身份和材料编号完全一致时，把
        一份已经存在的引用登记到 TaskResult 名下，供任务终态统一 release。

        Args:
            event: 已经通过 `track_tool_acceptance` 的 canonical acceptance 事件。
            context_id: 调用方准备移交所有权的材料上下文编号。

        Returns:
            TaskResult 已持有或成功接管该租约时返回真。
        """

        activity_key = _task_result_activity_key(event)
        normalized_context_id = context_id.strip()
        if (
            activity_key is None
            or not normalized_context_id
            or activity_key in self._closed_activities
        ):
            return False
        state = self._deliveries.get(activity_key)
        if (
            state is None
            or state.closing
            or state.acceptance is None
            or state.acceptance.material_context_id != normalized_context_id
        ):
            return False
        if normalized_context_id not in state.retained_material_context_ids:
            state.retained_material_context_ids.append(normalized_context_id)
        return True

    def track_terminal(
        self,
        event: RuntimeEvent,
    ) -> TaskResultTerminalDelivery | None:
        """保存首条权威终态，并在四项条件齐全时开放交付。

        Args:
            event: 后台工作的 completed、failed 或 cancelled 事件。

        Returns:
            可以立即公开的首条权威终态；需要等待或已吸收时返回空。
        """

        activity_key = _task_result_activity_key(event)
        source = _delivery_source_from_event(event.type)
        if activity_key is None or source is None:
            return None
        if activity_key in self._closed_activities:
            return None
        # 终态可能比 acceptance 或 started 更早回流，先保存下来，等其余证明补齐。
        state = self._deliveries.setdefault(
            activity_key,
            _TaskResultDeliveryState(activity_key=activity_key),
        )
        if event.payload.get("execution_phase") == "before_start":
            state.execution_phase = "before_start"
        if state.pending_terminal is not None:
            logger.info(
                "忽略 TaskResult 重复终态 runtime_epoch=%s activity_id=%s "
                "authoritative_type=%s duplicate_type=%s",
                activity_key[0],
                activity_key[1],
                state.pending_terminal.source_event.type,
                event.type,
            )
            # Notice 写入失败会放弃旧 claim，但首条终态仍是权威事实。
            # 重放同一来源事件时只重领这条终态，绝不用迟到载荷覆盖它。
            return self._claim_ready_terminal(state)
        if state.acceptance is not None and state.acceptance.source != source:
            logger.warning(
                "拒绝来源冲突的交付终态 runtime_epoch=%s activity_id=%s "
                "accepted_source=%s terminal_source=%s",
                activity_key[0],
                activity_key[1],
                state.acceptance.source,
                source,
            )
            return None

        state.pending_terminal = TaskResultTerminalDelivery(
            activity_key=activity_key,
            delivery_token=0,
            source_event=event,
            public_event=None,
        )
        return self._claim_ready_terminal(state)

    def acknowledge_request(
        self,
        runtime_epoch: int,
        request_id: str,
    ) -> list[TaskResultTerminalDelivery]:
        """记录 Foreground 请求终态，并开放所有刚满足条件的 TaskResult。

        Args:
            runtime_epoch: 已完成 Foreground 回复所属的助手运行时世代。
            request_id: 已完成、被中断或被 supersede 的 Foreground 请求编号。

        Returns:
            此次确认后可以公开交付的权威终态。
        """

        normalized_request_id = request_id.strip()
        if not normalized_request_id:
            return []
        acknowledgement_key = (runtime_epoch, normalized_request_id)
        self._acknowledged_requests[acknowledgement_key] = None
        self._acknowledged_requests.move_to_end(acknowledgement_key)
        self._trim_ordered_identity_map(self._acknowledged_requests)

        ready: list[TaskResultTerminalDelivery] = []
        for (state_epoch, _activity_id), state in self._deliveries.items():
            if state_epoch != runtime_epoch or state.acceptance is None:
                continue
            if state.acceptance.acknowledgement_id != normalized_request_id:
                continue
            state.acknowledged = True
            delivery = self._claim_ready_terminal(state)
            if delivery is not None:
                ready.append(delivery)
        return ready

    def release_terminal(self, event: RuntimeEvent) -> list[dict[str, object]]:
        """关闭已交付任务并只释放它实际保留的同一材料上下文。

        Args:
            event: 已经完成公开交付的 TaskResult 终态事件。

        Returns:
            需要通过瞬时控制通道发送的材料释放事件。
        """

        activity_key = _task_result_activity_key(event)
        if activity_key is None:
            return []
        return self._close_activity(activity_key)

    def commit_result_card(self, event: RuntimeEvent) -> None:
        """提交一张结果卡快照，供实时广播和控制 WebSocket 重连读取。

        提交先于实时 WebSocket 广播。单条连接故障不会回滚这份主进程快照，也不会阻止
        后续终态 Notice；这里只记录公开的 `task_result.*` 终态，不触发其它副作用。

        Args:
            event: 已越过四项交付证明边界的结果卡终态事件。
        """

        activity_key = _task_result_activity_key(event)
        if activity_key is None or event.type not in _TASK_RESULT_RESULT_CARD_EVENTS:
            return
        self._committed_result_cards[activity_key] = event
        self._committed_result_cards.move_to_end(activity_key)
        self._trim_ordered_identity_map(self._committed_result_cards)

    def should_broadcast_result_card(self, delivery: TaskResultTerminalDelivery) -> bool:
        """判断当前 claim 的结果卡是否仍需要实时广播。

        Args:
            delivery: 已领取的 TaskResult 终态交付对象。

        Returns:
            claim 仍有效、存在结果卡且尚未广播成功时返回真。
        """

        state = self._deliveries.get(delivery.activity_key)
        return bool(
            delivery.public_event is not None
            and state is not None
            and not state.closing
            and state.delivery_claimed
            and state.delivery_token == delivery.delivery_token
            and not state.result_card_broadcasted
        )

    def mark_result_card_broadcasted(self, delivery: TaskResultTerminalDelivery) -> bool:
        """记录当前 claim 的结果卡已经实时广播成功。

        Args:
            delivery: 已完成实时广播的 TaskResult 终态交付对象。

        Returns:
            claim 仍有效并首次记录成功时返回真。
        """

        state = self._deliveries.get(delivery.activity_key)
        if (
            state is None
            or not state.delivery_claimed
            or state.delivery_token != delivery.delivery_token
            or state.result_card_broadcasted
        ):
            return False
        state.result_card_broadcasted = True
        return True

    def abandon_terminal_delivery(self, delivery: TaskResultTerminalDelivery) -> bool:
        """放弃一次未完成的交付 claim，保留终态和材料等待重领。

        Args:
            delivery: Notice 尚未成功写入的 TaskResult 交付对象。

        Returns:
            claim 仍有效并已被释放时返回真。
        """

        state = self._deliveries.get(delivery.activity_key)
        if (
            state is None
            or not state.delivery_claimed
            or state.delivery_token != delivery.delivery_token
        ):
            return False
        state.delivery_claimed = False
        return True

    def complete_terminal_delivery(
        self,
        delivery: TaskResultTerminalDelivery,
    ) -> list[dict[str, object]]:
        """在结果卡与 Notice 都完成后关闭当前 claim 并释放材料。

        Args:
            delivery: 已完成全部必达副作的 TaskResult 交付对象。

        Returns:
            该 Activity 持有的材料真正释放后生成的瞬时事件。
        """

        state = self._deliveries.get(delivery.activity_key)
        if (
            state is None
            or not state.delivery_claimed
            or state.delivery_token != delivery.delivery_token
        ):
            return []
        return self._close_activity(delivery.activity_key)

    def committed_result_card_events(self) -> list[dict[str, object]]:
        """读取当前进程内已经提交的结果卡控制事件快照。

        Returns:
            按最近交付顺序排列的 `task_result.*` 终态事件。
        """

        return [event.to_control_event() for event in self._committed_result_cards.values()]

    def is_delivery_current(self, delivery: TaskResultTerminalDelivery) -> bool:
        """判断一份已领取的终态交付是否仍属于未中断的 TaskResult。

        运行时替换会同步关闭旧世代状态。结果卡协程在网络广播 ``await`` 后恢复时，必须
        再检查这份凭据，避免旧世代继续创建 Notice 或结果卡。

        Args:
            delivery: 已由本协调器领取的终态交付对象。

        Returns:
            状态仍存在且 token 未被替换或关闭时返回真。
        """

        state = self._deliveries.get(delivery.activity_key)
        return bool(
            state is not None
            and not state.closing
            and state.delivery_claimed
            and state.delivery_token == delivery.delivery_token
        )

    def discard_activity(self, event: RuntimeEvent) -> list[dict[str, object]]:
        """丢弃未被权威 Registry 接受的 Activity 并释放其材料租约。

        Args:
            event: 未能进入公共 Activity 的 started 事件。

        Returns:
            Activity 曾取得材料租约时产生的释放事件。
        """

        activity_key = _task_result_activity_key(event)
        if activity_key is None:
            return []
        return self._close_activity(activity_key)

    def interrupt_runtime_epoch(self, runtime_epoch: int) -> list[dict[str, object]]:
        """关闭指定失效世代的 provisional 与 active 状态并释放真实租约。

        Args:
            runtime_epoch: 已失效的助手运行时世代。

        Returns:
            对应旧任务材料真正失去所有权后产生的瞬时事件。
        """

        interrupted_activity_keys = [
            activity_key
            for activity_key in self._deliveries
            if activity_key[0] == runtime_epoch
        ]
        release_events = self._epoch_release_events.setdefault(runtime_epoch, [])
        # 整批先失效，任意释放端口失败都不能让后续 Activity 继续公开交付。
        for activity_key in interrupted_activity_keys:
            self._deliveries[activity_key].closing = True
            self._remember_closed_activity(activity_key)
        failure: Exception | None = None
        for activity_key in interrupted_activity_keys:
            try:
                release_events.extend(self._close_activity(activity_key))
            except Exception as error:
                if failure is None:
                    failure = error
        if failure is not None:
            raise failure
        return self._epoch_release_events.pop(runtime_epoch)

    def closing_activities_for_request(self, runtime_epoch: int, request_id: str) -> list[str]:
        """定位本轮确认交付后尚未释放完的任务，避免重试阻塞无关轮次。

        Args:
            runtime_epoch: 请求所属的助手世代。
            request_id: 发起任务的 Foreground 请求编号。

        Returns:
            只属于该请求且正在关闭的任务编号。
        """
        return [key[1] for key, state in self._deliveries.items()
                if key[0] == runtime_epoch and state.closing and state.acceptance is not None
                and state.acceptance.acknowledgement_id == request_id.strip()]

    def retry_material_release(self, runtime_epoch: int, activity_id: str) -> list[dict[str, object]]:
        """只重试目标任务已经开始的材料释放，不改变其它交付的状态。

        Args:
            runtime_epoch: 目标任务所属的助手世代。
            activity_id: 目标任务编号。

        Returns:
            目标任务成功释放后尚未交出的全部材料事件。
        """
        activity_key = (runtime_epoch, activity_id)
        state = self._deliveries.get(activity_key)
        if state is None or not state.closing:
            return []
        return self._close_activity(activity_key)

    def _close_activity(self, activity_key: _TaskResultActivityKey) -> list[dict[str, object]]:
        """先阻止迟到交付，真实释放成功后才移除重试所需的状态。

        Args:
            activity_key: 需要关闭的助手世代与任务编号。

        Returns:
            该任务尚未交给调用方的材料释放事件。
        """
        self._remember_closed_activity(activity_key)
        state = self._deliveries.get(activity_key)
        if state is None:
            return []
        state.closing = True
        events = self._release_state_material(state)
        self._deliveries.pop(activity_key, None)
        return events

    def _claim_ready_terminal(
        self,
        state: _TaskResultDeliveryState,
    ) -> TaskResultTerminalDelivery | None:
        """在四项条件齐全时仅认领一次首条权威终态。

        Args:
            state: 当前 TaskResult 的交付状态。

        Returns:
            首次满足条件时返回权威终态，否则返回空。
        """

        activity_key = state.activity_key
        if (
            activity_key is None
            or state.closing
            or state.delivery_claimed
            or state.acceptance is None
            or state.pending_terminal is None
        ):
            return None
        before_start_terminal = (
            state.pending_terminal.source_event.type
            in {"background_agent.failed", "background_agent.cancelled"}
            and state.execution_phase == "before_start"
            and not state.started
        )
        if state.acceptance.deliverable and (
            (not state.started and not before_start_terminal) or not state.acknowledged
        ):
            return None
        if state.started_source is not None and state.started_source != state.acceptance.source:
            return None
        self._next_delivery_token += 1
        state.delivery_claimed = True
        state.delivery_token = self._next_delivery_token
        source_event = state.pending_terminal.source_event
        public_event = (
            _public_task_result_terminal(source_event, state.acceptance.source)
            if state.acceptance.deliverable
            else None
        )
        return TaskResultTerminalDelivery(
            activity_key=activity_key,
            delivery_token=state.delivery_token,
            source_event=source_event,
            public_event=public_event,
        )

    def _retain_state_material(self, state: _TaskResultDeliveryState) -> None:
        """只按首次 acceptance 尝试一次材料租约。

        Args:
            state: 已绑定不可变 acceptance 身份的交付状态。
        """

        if state.acceptance is None:
            return
        context_id = state.acceptance.material_context_id
        if not context_id:
            return
        self._retain_material_context(state, context_id)

    def _retain_parent_state_material(
        self,
        *,
        activity_key: _TaskResultActivityKey,
        state: _TaskResultDeliveryState,
    ) -> None:
        """为纠正后的 B 续接父 Activity 已持有的全部材料租约。

        Args:
            activity_key: B 的运行时世代和 Activity 编号。
            state: B 已经绑定 acceptance 的交付状态。

        Returns:
            无。父状态缺失时不伪造租约，只记录诊断日志。
        """

        if state.acceptance is None or not state.acceptance.based_on_activity_id:
            return
        parent_key = (activity_key[0], state.acceptance.based_on_activity_id)
        parent_state = self._deliveries.get(parent_key)
        if parent_state is None:
            logger.warning(
                "纠正任务无法续接父材料租约 runtime_epoch=%s activity_id=%s parent_activity_id=%s",
                activity_key[0],
                activity_key[1],
                parent_key[1],
            )
            return
        # 只复制父 Activity 真正 retain 成功的 context。B 为每一项重新取得自己的租约，
        # 因此 A 随后取消并 release 时，受管材料仍会继续存活到 B 的终态。
        for context_id in tuple(parent_state.retained_material_context_ids):
            self._retain_material_context(state, context_id)

    def _retain_material_context(
        self,
        state: _TaskResultDeliveryState,
        context_id: str,
    ) -> None:
        """为一个 Activity 幂等取得指定材料上下文租约。

        Args:
            state: 当前 Activity 的交付状态。
            context_id: 初始派单或后续回答轮的材料上下文编号。

        Returns:
            无。
        """

        if context_id in state.attempted_material_context_ids:
            return
        state.attempted_material_context_ids.add(context_id)
        if self._retain_material is None:
            return
        try:
            retained = self._retain_material(context_id)
        except Exception:
            logger.exception("TaskResult 取得材料租约失败 context_id=%s", context_id)
            return
        if retained:
            state.retained_material_context_ids.append(context_id)

    def _release_state_material(
        self,
        state: _TaskResultDeliveryState,
    ) -> list[dict[str, object]]:
        """释放状态实际取得的材料租约。

        Args:
            state: 即将关闭的 TaskResult 交付状态。

        Returns:
            材料服务产生的瞬时释放事件。
        """

        if self._release_material is None:
            return []
        for context_id in tuple(state.retained_material_context_ids):
            state.pending_release_events.extend(self._release_material(context_id))
            state.retained_material_context_ids.remove(context_id)
        release_events = state.pending_release_events
        state.pending_release_events = []
        return release_events

    def _remember_closed_activity(
        self,
        activity_key: _TaskResultActivityKey,
    ) -> None:
        """有界记录关闭 Activity，吸收迟到和重复事件。

        Args:
            activity_key: 由运行时世代和 Activity 编号组成的稳定身份。
        """

        self._closed_activities[activity_key] = None
        self._closed_activities.move_to_end(activity_key)
        self._trim_ordered_identity_map(self._closed_activities)

    def _trim_ordered_identity_map(self, values: OrderedDict[object, None]) -> None:
        """把最近身份记录裁剪到固定上限。

        Args:
            values: 按最近使用顺序保存的身份映射。
        """

        while len(values) > self._recent_identity_limit:
            values.popitem(last=False)


def _task_result_activity_key(event: RuntimeEvent) -> _TaskResultActivityKey | None:
    """从运行时事件提取不可跨世代复用的 TaskResult 身份。

    Args:
        event: 可能属于任务结果链的运行时事件。

    Returns:
        有效的运行时世代和 Activity 编号；字段缺失时返回空。
    """

    activity_id = _clean_text(event.payload.get("activity_id"))
    if event.runtime_epoch is None or not activity_id:
        return None
    return event.runtime_epoch, activity_id


def _rejected_acceptance_tracking_result() -> TaskResultAcceptanceTrackingResult:
    """构造一份没有建立 TaskResult 身份的确定性结果。

    Returns:
        所有资格与租约证明都为假的不可变结果。
    """

    return TaskResultAcceptanceTrackingResult(
        accepted=False,
        first_acceptance=False,
        material_context_id="",
        material_retained=False,
    )


def _acceptance_delivery_identity(
    payload: dict[str, object],
) -> tuple[_TaskResultDeliverySource, bool, bool] | None:
    """从 Foreground 工具完成事实读取执行来源和交付规则。

    Args:
        payload: `llm.foreground.tool_call_completed` 的内部载荷。

    Returns:
        可进入交付协调器的来源与交付标记；其它工具或字段无效时返回空。
    """

    tool_name = payload.get("name")
    if tool_name not in {"start_background_work", "correct_background_work"}:
        return None
    deliverable = payload.get("deliverable")
    if not isinstance(deliverable, bool):
        return None
    return "background_agent", deliverable, tool_name == "correct_background_work"


def _delivery_source_from_event(event_type: str) -> _TaskResultDeliverySource | None:
    """识别能参与结果交付的 Activity 生命周期来源。

    Args:
        event_type: 助手子进程回流的事件名称。

    Returns:
        后台工作来源；其它事件返回空。
    """

    if event_type.startswith("background_agent."):
        return "background_agent"
    return None


def _public_task_result_terminal(
    event: RuntimeEvent,
    source: _TaskResultDeliverySource,
) -> RuntimeEvent:
    """把权威终态投影成 Desktop 使用的任务结果卡事件。

    Args:
        event: 执行来源产生的原始终态事件。
        source: 首次 acceptance 冻结的执行来源。

    Returns:
        可通过控制 WebSocket 公开的 `task_result.*` 事件。
    """

    lifecycle = event.type.removeprefix("background_agent.")
    public_type = f"task_result.{lifecycle}"
    payload = event.payload
    base_payload: dict[str, object] = {
        "activity_id": _clean_text(payload.get("activity_id")),
        "interaction_session_id": _clean_text(payload.get("interaction_session_id")),
        "goal": _clean_text(payload.get("goal")),
        "source_materials": _clean_text_list(payload.get("source_materials")),
        "created_at": payload.get("created_at", 0.0),
    }
    if lifecycle == "completed":
        base_payload["display_content"] = _clean_text(payload.get("display_content"))
        base_payload["output_paths"] = _clean_text_list(payload.get("output_paths"))
    elif lifecycle == "failed":
        base_payload["failure"] = payload.get("failure", {})
    elif lifecycle == "cancelled":
        base_payload["message"] = "后台任务已取消。"
        base_payload["reason"] = _clean_text(payload.get("reason")) or "agent_cancelled"
    return RuntimeEvent(
        type=public_type,
        source=event.source,
        payload=base_payload,
        produced_at=event.produced_at,
        runtime_epoch=event.runtime_epoch,
    )


def public_background_agent_started(event: RuntimeEvent) -> RuntimeEvent:
    """把后台智能体启动事实投影成结果卡使用的公开启动事件。

    Args:
        event: `background_agent.started` 原始事件。

    Returns:
        与终态结果卡使用同一 Activity 身份的 `task_result.started` 事件。
    """

    payload = event.payload
    return RuntimeEvent(
        type="task_result.started",
        source=event.source,
        payload={
            "activity_id": _clean_text(payload.get("activity_id")),
            "interaction_session_id": _clean_text(payload.get("interaction_session_id")),
            "goal": _clean_text(payload.get("goal")),
            "created_at": payload.get("created_at", 0.0),
        },
        produced_at=event.produced_at,
        runtime_epoch=event.runtime_epoch,
    )


def _clean_text_list(value: object) -> list[str]:
    """清洗结果卡中的路径或材料名称列表。

    Args:
        value: 尚未确认类型的来源字段。

    Returns:
        去除空白项后的字符串列表；非列表返回空列表。
    """

    if not isinstance(value, (list, tuple)):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _clean_text(value: object) -> str:
    """清洗内部事件中的字符串标识。

    Args:
        value: 待校验的任意事件字段。

    Returns:
        去除首尾空白的字符串；非字符串返回空串。
    """

    return value.strip() if isinstance(value, str) else ""
