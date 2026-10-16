from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Literal

from companion.activities.notice.delivery_coordinator import (
    NoticeDeliveryCoordinator,
    NoticeReadMode,
)
from companion.activities.notice.event_mapper import map_activity_event
from companion.activities.notice.models import (
    ActivityEventApplyResult,
    ActivityNoticeInterruptionStrategy,
    NoticeContext,
    NoticeDeliveryCommand,
    NoticeDeliveryResult,
)
from companion.activities.notice.activity_result_context import (
    ActivityNoticeResultContextProjector,
)
from companion.activities.progress_ledger import ActivityProgressLedger
from companion.core.application.assistant_request_models import (
    AssistantRequestContextSnapshot,
    CompositeAssistantRequestContextLease,
    PreparedAssistantRequestContext,
    ForegroundContext,
)
from companion.core.broadcast import BroadcastHub
from companion.core.realtime_input_runtime_owner import RealtimeInputRuntimeOwner
from companion.environment.models import FrozenEnvironmentSnapshot
from companion.materials.foreground_images import load_foreground_visual_input
from companion.materials.foreground_view import build_foreground_turn_material_summary
from companion.materials.models import MaterialWorkloadProfile, TurnMaterialContext
from companion.observability.logging_config import get_backend_logger
from companion.runtime_memory.store import RuntimeMemoryStore
from companion.realtime.assistant_output_contracts import (
    resolve_assistant_output_contract,
)
from companion.timed_reminders.delivery_coordinator import (
    TimedReminderDeliveryCoordinator,
    TimedReminderNoticeBatch,
)
from companion.timed_reminders.service import TimedReminderService


logger = get_backend_logger(__name__)


class AssistantRequestContextPreparationError(RuntimeError):
    """保存 final ASR 请求资料构造失败前已经创建的材料上下文身份。"""

    def __init__(self, turn_material_context_id: str) -> None:
        """初始化可供输入线程定向回收材料的内部异常。

        Args:
            turn_material_context_id: 失败请求已经创建的材料上下文编号。
        """

        super().__init__("assistant request context preparation failed")
        self.turn_material_context_id = turn_material_context_id


def _inactive_material_profile() -> MaterialWorkloadProfile:
    """构造材料预检不可用时的保守默认画像。

    Returns:
        禁止后台工作读取当前材料的工作负载画像。
    """

    return MaterialWorkloadProfile(
        task_result_material_eligible=False,
        local_file_count=0,
        visual_unit_count=0,
        text_byte_count=0,
        local_total_bytes=0,
        contains_mixed_material=False,
        ignored_file_count=0,
        blocking_reasons=("preflight_unavailable",),
    )


def local_iso_datetime_text() -> str:
    """生成当前本地时间的 ISO 字符串。

    Returns:
        带本地时区信息的 ISO 时间文本。
    """

    return datetime.now().astimezone().isoformat()


class AssistantRequestContextPreparer:
    """在请求离开主进程前冻结事实，并管理 Notice 的预留事务。

    它不发送助手请求，也不运行 Foreground LLM；它只把“此刻该请求允许看到什么”冻结为
    ``AssistantRequestContextSnapshot``，并返回必须在成功入队后提交、失败时回滚的 Notice
    lease。冻结内容包括角色资料、明确归属的本轮材料、环境快照和 Notice 批次。

    本类还有一条反向入口：活动事件从 ``LiveKitMediaSession`` 回流时，
    ``handle_activity_event()`` 将来源事件映射后写入同一个 ``NoticeDeliveryCoordinator``。
    Coordinator 只返回 supersession 事实；实际移除或取消过期 System Notice 请求仍由持有
    Bridge 队列的媒体会话完成。
    """

    def __init__(
        self,
        *,
        hub: BroadcastHub,
        realtime_input_runtime_owner: RealtimeInputRuntimeOwner,
        notice_coordinator: NoticeDeliveryCoordinator | None = None,
        activity_notice_result_context: ActivityNoticeResultContextProjector
        | None = None,
        pending_work_interactions_provider: Callable[
            [str], list[dict[str, object]]
        ]
        | None = None,
        runtime_memory_store: RuntimeMemoryStore | None = None,
        activity_progress_ledger: ActivityProgressLedger | None = None,
        timed_reminder_service: TimedReminderService | None = None,
        timed_reminder_delivery: TimedReminderDeliveryCoordinator | None = None,
    ) -> None:
        """初始化助手请求上下文服务。

        Args:
            hub: 控制面状态、材料上下文和环境快照的统一访问入口。
            realtime_input_runtime_owner: 当前实时输入运行时所有者。
            notice_coordinator: 统一活动提醒协调器；为空时使用默认实例。
            activity_notice_result_context: 主动提醒的通用 Activity 结果上下文投影器。
            pending_work_interactions_provider: 按会话读取当前安全权限和资料请求的函数。
            runtime_memory_store: Backend 进程级 Foreground 记忆所有者。
            activity_progress_ledger: Backend 进程级 Activity Progress 临时账本。
            timed_reminder_service: 当前运行期定时提醒的权威服务。
            timed_reminder_delivery: 到点批次与 Foreground 请求的交付协调器。
        """

        self.hub = hub
        self.realtime_input_runtime_owner = realtime_input_runtime_owner
        self.notice_coordinator = notice_coordinator or NoticeDeliveryCoordinator()
        self.activity_notice_result_context = activity_notice_result_context
        self.pending_work_interactions_provider = pending_work_interactions_provider
        self.runtime_memory_store = runtime_memory_store or RuntimeMemoryStore()
        self.activity_progress_ledger = (
            activity_progress_ledger or ActivityProgressLedger()
        )
        self.timed_reminder_service = timed_reminder_service
        self.timed_reminder_delivery = timed_reminder_delivery

    def handle_activity_event(
        self,
        event_type: str,
        payload: dict[str, object],
        runtime_epoch: int,
    ) -> ActivityEventApplyResult:
        """将助手子进程回流的活动事件映射并写入 Notice Coordinator。

        生产环境由 ``LiveKitMediaSession._handle_activity_event()`` 调用本方法。未知事件
        或缺失身份的事件在映射阶段被忽略；有效事件由 Coordinator 维护进度替换、终态
        候选与批次账本。本方法不会创建 System Notice 请求，也不会自己取消旧请求。

        Args:
            event_type: 认知或后台工作等来源事件类型。
            payload: 与事件对应的序列化载荷。
            runtime_epoch: 事件所属助手运行时世代。

        Returns:
            协调器应用结果，包含需要尝试移除或条件取消的请求编号。
        """
        # map只是映射处理成对应事件的数据结构
        event = map_activity_event(event_type, dict(payload))
        if event is None:
            return ActivityEventApplyResult()
        # 本层只产出 supersession 事实；请求是否仍在 Bridge 队列或已进入子进程，
        # 只有 LiveKitMediaSession 才能准确判断并采取对应的移除或条件取消动作。
        result = self.notice_coordinator.apply_event(event, runtime_epoch)
        self._release_superseded_terminal_progress(result)
        return result

    def _release_superseded_terminal_progress(
        self,
        result: ActivityEventApplyResult,
    ) -> None:
        """删除已经永久失去表达资格的 frozen Progress。

        Args:
            result: Notice 账本返回的替代与释放事实。
        """

        if result.released_terminal_activity_keys:
            self.activity_progress_ledger.release_frozen(
                result.released_terminal_activity_keys
            )

    def apply_notice_delivery(
        self,
        command: NoticeDeliveryCommand,
    ) -> NoticeDeliveryResult:
        """推进 Notice 交付并删除已经完成处置的 frozen Progress。

        Args:
            command: Bridge 或输出链路产生的请求级交付结果。

        Returns:
            Coordinator 接受结果和本次明确释放的终态 Activity 身份。
        """

        result = self.notice_coordinator.apply_delivery(command)
        if self.timed_reminder_delivery is not None:
            self.timed_reminder_delivery.apply_delivery(command)
        if result.released_terminal_activity_keys:
            self.activity_progress_ledger.release_frozen(
                result.released_terminal_activity_keys
            )
        return result

    def interrupt_activity_notices(
        self,
        runtime_epoch: int,
        strategy: ActivityNoticeInterruptionStrategy,
    ) -> ActivityEventApplyResult:
        """收口旧世代 Notice，并释放已经永久放弃的终态 Progress。

        Args:
            runtime_epoch: 即将失效的 Assistant Runtime 世代。
            strategy: 活跃 Activity 在热替换时采用的提醒收口策略。

        Returns:
            需要媒体会话条件取消的旧请求，以及已经释放的终态身份。
        """

        result = self.notice_coordinator.interrupt_active(runtime_epoch, strategy)
        self._release_superseded_terminal_progress(result)
        return result

    def prepare(
        self,
        *,
        request_id: str,
        request_source: str,
        turn_material_context_id: str | None = None,
        environment_snapshot_id: str | None = None,
        source_turn_id: str | None = None,
    ) -> PreparedAssistantRequestContext:
        """冻结一条用户或 Activity Notice 请求并创建 Notice 预留事务。

        调用方必须在助手请求成功进入输出队列后提交返回的 lease；若构造、入队或后续
        提交失败，则回滚 lease。这样 Notice 不会在尚未存在的助手请求上永久处于预留态。

        Args:
            request_id: 即将构造的助手请求编号。
            request_source: text_input、final_asr 或 system_notice。
            turn_material_context_id: 真实用户请求明确绑定的材料上下文编号。
            environment_snapshot_id: 文本输入显式绑定的环境快照编号。
            source_turn_id: final ASR 所属语音 turn 编号。

        Returns:
            同时持有冻结载荷与提醒批次 lease 的准备上下文。
        """
        # 先登记 request_id 的事务占位；真正选择并预留 Notice 发生在下方冻结上下文时。
        notice_lease = self.notice_coordinator.create_batch_lease(request_id)
        try:
            # 真实请求冻结时，Coordinator 会按来源模式把可交付 Notice 绑定到该 request_id，
            # 并把批次投影写入 foreground_context，供同一请求的 Foreground 读取。
            snapshot = self._freeze_request_context(
                reserve_notice_batch=True,
                request_id=request_id,
                request_source=request_source,
                turn_material_context_id=turn_material_context_id,
                environment_snapshot_id=environment_snapshot_id,
                source_turn_id=source_turn_id,
            )
        except Exception:
            # 冻结中途失败时，撤销可能已经绑定到该 request_id 的 Notice，避免产生孤儿预留。
            notice_lease.rollback()
            raise
        return PreparedAssistantRequestContext(
            snapshot=snapshot,
            lease=CompositeAssistantRequestContextLease(leases=(notice_lease,)),
            permission_interaction_request_ids=(
                self.notice_coordinator.permission_interaction_request_ids(request_id)
                if request_source == "system_notice"
                else ()
            ),
        )

    def _freeze_request_context(
        self,
        *,
        reserve_notice_batch: bool,
        request_id: str | None = None,
        request_source: str | None = None,
        turn_material_context_id: str | None = None,
        environment_snapshot_id: str | None = None,
        source_turn_id: str | None = None,
    ) -> AssistantRequestContextSnapshot:
        """冻结本轮助手请求可见的稳定事实，不读取后续变化的全局状态。

        用户请求必须携带入口已分配的材料上下文编号；System Notice 不读取环境、材料或
        全局活动预览，只读取它已预留的 Notice 批次。该差异防止主动提醒意外携带下一轮
        用户现场或材料。

        Args:
            reserve_notice_batch: 是否为真实助手请求预留提醒批次。
            request_id: 当前助手请求编号；为空时表示只读预览。
            request_source: 当前请求来源，例如 text_input 或 final_asr。
            turn_material_context_id: 真实用户请求明确绑定的材料上下文编号。
            environment_snapshot_id: 文本提交明确绑定的环境快照编号。
            source_turn_id: final ASR 所属的语音 turn 编号。

        Returns:
            不可变的助手请求上下文快照。
        """
        # Notice 的候选、预留与交付都按交互会话隔离，先读取本请求所属会话作为唯一主键。
        interaction_session_id = self.realtime_input_runtime_owner.current_interaction_state().interaction_session_id

        # 根据请求来源决定是预览、附着到用户请求，还是创建独立 System Notice 的预留批次。
        notice_context = self.prepare_notice_context(
            interaction_session_id=interaction_session_id,
            reserve_batch=reserve_notice_batch,
            request_id=request_id,
            request_source=request_source,
        )
        current_timed_reminder_snapshot = (
            self.timed_reminder_service.snapshot()
            if self.timed_reminder_service is not None
            and request_source in {"text_input", "final_asr", None}
            else None
        )
        current_timed_reminders = (
            [reminder.to_payload() for reminder in current_timed_reminder_snapshot.active]
            if current_timed_reminder_snapshot is not None
            else []
        )
        timed_reminder_runtime_id = (
            current_timed_reminder_snapshot.runtime_id
            if current_timed_reminder_snapshot is not None
            else ""
        )

        # 真实用户请求只能读取入口绑定的本轮材料上下文，禁止通过全局 latest 猜测并发轮次归属。
        is_user_request = request_source in {"text_input", "final_asr"}
        frozen_turn_context: TurnMaterialContext | None = None
        material_workload_profile = None
        if is_user_request:
            normalized_context_id = (
                turn_material_context_id.strip()
                if isinstance(turn_material_context_id, str)
                else ""
            )
            if not normalized_context_id:
                raise RuntimeError("user request requires turn material context")
            # 一次锁内同时取得材料与画像，保证二者属于同一轮；迟到预检由 closed 状态门控，
            # 此处不能取消无作用域任务，否则可能误伤下一批待提交材料。
            frozen_materials = self.hub.turn_materials.freeze_active_context(
                normalized_context_id
            )
            if frozen_materials is None:
                raise RuntimeError("turn material context does not exist")
            frozen_turn_context, material_workload_profile = frozen_materials

        # 冻结这次请求的环境快照，避免模型生成期间又读到已经变化的现场状态。
        logger.info(
            "冻结助手请求环境快照 environment_snapshot_id=%s", environment_snapshot_id
        )
        environment_snapshot = (
            self._freeze_environment_snapshot(
                request_source=request_source,
                environment_snapshot_id=environment_snapshot_id,
                source_turn_id=source_turn_id,
            )
            if request_source != "system_notice"
            else None
        )

        # System Notice 只携带已预留的 Notice 投影；它不能借机读取全局环境、材料或活动预览。
        foreground_context: ForegroundContext = (
            {**notice_context}
            if request_source == "system_notice"
            else self.build_foreground_context(
                notice_context={
                    **notice_context,
                    "timed_reminders": current_timed_reminders,
                    "timed_reminder_runtime_id": timed_reminder_runtime_id,
                },
                material_workload_profile=material_workload_profile,
                turn_material_context=frozen_turn_context,
                environment_snapshot=environment_snapshot,
                allow_global_preview_fallback=not is_user_request,
            )
        )
        notice_activity_keys = _notice_activity_keys(foreground_context)
        progress_snapshot = (
            self.activity_progress_ledger.snapshot_payload_for(notice_activity_keys)
            if request_source == "system_notice"
            else self.activity_progress_ledger.snapshot_payload_for_user_request(
                notice_activity_keys
            )
        )
        if progress_snapshot:
            # Progress 不属于稳定聊天历史，只通过本轮冻结的动态环境交给子进程。
            foreground_context["activity_progress_reports"] = progress_snapshot
        history_policy, activity_progress_references = (
            resolve_assistant_output_contract(
                request_source=request_source,
                foreground_context=foreground_context,
            )
        )
        derived_from_activity_ids = _cognitive_result_activity_ids(
            foreground_context
        )
        # 把角色资料、材料身份、环境快照和前台态势一次性封装；后续助手子进程只能读取
        # 这份快照，不能回头查询主进程的“当前值”。
        prompt_profile = self.hub.current_prompt_profile_payload()
        return AssistantRequestContextSnapshot(
            character_card=prompt_profile["character_card"],
            user_call_name=prompt_profile["user_call_name"],
            user_self_intro=prompt_profile["user_self_intro"],
            user_extra_notes=prompt_profile["user_extra_notes"],
            turn_material_context_id=(
                frozen_turn_context.context_id
                if frozen_turn_context is not None
                else None
            ),
            environment_snapshot=environment_snapshot,
            foreground_context=foreground_context,
            frozen_visual_input=(
                load_foreground_visual_input(frozen_turn_context, environment_snapshot)
                if is_user_request else None
            ),
            runtime_memory_snapshot=self.runtime_memory_store.snapshot(),
            history_policy=history_policy,
            activity_progress_references=activity_progress_references,
            derived_from_activity_ids=derived_from_activity_ids,
        )

    def prepare_timed_reminder_notice_snapshot(
        self,
        batch: TimedReminderNoticeBatch,
    ) -> AssistantRequestContextSnapshot:
        """冻结独立 Reminder Notice 的最小请求事实。

        这条路径只读取角色资料和 Coordinator 已冻结的事项，不读取聊天历史对应的
        Activity、材料、环境、普通 Notice 或完整提醒清单。

        Args:
            batch: Coordinator 在开始消费前冻结的本次响铃批次。

        Returns:
            可直接用于构造 ``timed_reminder_notice`` 请求的上下文快照。

        Raises:
            ValueError: 批次为空，或批次属于其它 Backend 运行期。
        """

        if not batch.reminders:
            raise ValueError("Reminder Notice 批次不能为空")
        current_snapshot = (
            self.timed_reminder_service.snapshot()
            if self.timed_reminder_service is not None
            else None
        )
        if current_snapshot is None or current_snapshot.runtime_id != batch.runtime_id:
            raise ValueError("Reminder Notice 批次不属于当前 Backend 运行期")

        prompt_profile = self.hub.current_prompt_profile_payload()
        return AssistantRequestContextSnapshot(
            character_card=prompt_profile["character_card"],
            user_call_name=prompt_profile["user_call_name"],
            user_self_intro=prompt_profile["user_self_intro"],
            user_extra_notes=prompt_profile["user_extra_notes"],
            turn_material_context_id=None,
            environment_snapshot=None,
            foreground_context={
                "timed_reminder_notice": {
                    "reminders": batch.prompt_payload(),
                    "interrupted_user": batch.interrupted_user,
                    "interrupted_assistant": batch.interrupted_assistant,
                }
            },
            runtime_memory_snapshot=self.runtime_memory_store.snapshot(),
            history_policy="stable_dialogue",
        )

    def prepare_system_output_snapshot(self) -> AssistantRequestContextSnapshot:
        """冻结不领取 Notice 的当前角色系统输出上下文。

        Returns:
            只包含当前角色、运行期记忆和空提醒环境的不可变请求快照。
        """

        return self._freeze_request_context(
            reserve_notice_batch=False,
            request_source="system_notice",
        )

    def prepare_notice_context(
        self,
        *,
        interaction_session_id: str,
        reserve_batch: bool,
        request_id: str | None,
        request_source: str | None,
    ) -> NoticeContext:
        """按请求来源选择 Notice 读取模式，并构造或预留本轮提醒批次。

        ``preview`` 只读；``user_request`` 仅把强提醒附着到用户正在发起的请求；
        ``system_notice`` 可预留独立播报所需的提醒。预留后的归属由 request_id 持有，
        后续必须通过 Notice lease 或交付状态机收口。

        Args:
            interaction_session_id: 当前交互会话编号。
            reserve_batch: 是否处于真实请求路径，影响读取模式。
            request_id: 当前助手请求编号；为空时表示预览态读取。
            request_source: 当前请求来源，用于区分用户请求和系统通知。

        Returns:
            描述本轮提醒批次的 NoticeContext。
        """

        # 默认仅预览，不把 Notice 绑定到请求，供只读上下文构造安全复用。
        mode: NoticeReadMode = "preview"
        normalized_source = (
            request_source.strip() if isinstance(request_source, str) else ""
        )

        # 真实请求才允许预留：系统主动提醒使用 system_notice；用户文本/语音请求只附着强提醒，
        # 避免弱进度打断用户主问题的回答。
        if reserve_batch and normalized_source == "system_notice":
            mode = "system_notice"
        elif reserve_batch and normalized_source in {"final_asr", "text_input"}:
            mode = "user_request"

        # Coordinator 维护候选、批次和请求占用；本类只选择读取模式，不复制选择规则。
        notice_context = self.notice_coordinator.build_notice_context(
            interaction_session_id=interaction_session_id,
            mode=mode,
            request_id=request_id,
        )
        if mode in {"system_notice", "user_request"}:
            projector = self.activity_notice_result_context
            if projector is not None:
                notice_context = projector.project(notice_context)
        notice_batch = notice_context.get("notice_batch")

        # 仅在发生预留或预览确实看到 Notice 时记录日志，避免普通空请求淹没运行日志。
        if reserve_batch or notice_batch:
            logger.info(
                "已为本次助手请求准备活动/提醒上下文 interaction_session_id=%s request_id=%s "
                "request_source=%s mode=%s reserve_batch=%s notice_count=%s",
                interaction_session_id,
                request_id,
                request_source,
                mode,
                reserve_batch,
                len(notice_batch) if isinstance(notice_batch, list) else 0,
            )
        return notice_context

    def freeze_pending_materials_for_final_asr_turn(
        self, *, source_turn_id: str | None
    ) -> str:
        """把当前待提交材料原子转入指定 Final ASR 轮次的材料上下文。

        Args:
            source_turn_id: final ASR 所属语音 turn 编号。

        Returns:
            本次 final ASR 明确绑定的材料上下文编号。
        """

        if not isinstance(source_turn_id, str) or not source_turn_id.strip():
            raise RuntimeError("final ASR requires source turn id")
        context = self.hub.turn_materials.activate_turn(
            turn_id=source_turn_id.strip(),
            created_at=local_iso_datetime_text(),
        )
        return context.context_id

    def build_foreground_context(
        self,
        *,
        notice_context: NoticeContext | None = None,
        material_workload_profile: MaterialWorkloadProfile | None = None,
        turn_material_context: TurnMaterialContext | None = None,
        environment_snapshot: FrozenEnvironmentSnapshot | None = None,
        allow_global_preview_fallback: bool = True,
    ) -> ForegroundContext:
        """构造 Foreground、工具和提示词可读取的产品态势投影。

        传入冻结事实时，本方法不得回退到全局 latest；只有显式的预览调用才允许读取当前
        活动材料和环境。这样真实请求不会在生成期间串入别的轮次的新现场或新材料。

        Args:
            notice_context: 已预先构造好的提醒批次；为空时按预览模式现场生成。
            material_workload_profile: 当前请求冻结后的材料工作负载画像。
            turn_material_context: 当前请求已经按明确编号读取的材料上下文。
            environment_snapshot: 当前请求已经冻结的环境快照。
            allow_global_preview_fallback: 没有冻结事实时是否允许回退读取全局环境或最新材料预览。

        Returns:
            供 Foreground 路由、轮次处理器和工具共享的瞬时产品态势载荷。
        """

        # 这是请求可见的产品态势投影，不是会话历史，也不是跨轮长期记忆。
        resolved_turn_material_context = turn_material_context
        if (
            resolved_turn_material_context is None
            and allow_global_preview_fallback
        ):
            resolved_turn_material_context = self.hub.turn_materials.active_context()
        resolved_environment_payload = (
            environment_snapshot.to_payload()
            if environment_snapshot is not None
            else None
        )
        if (
            resolved_environment_payload is None
            and allow_global_preview_fallback
        ):
            resolved_environment_payload = self.hub.environment.current_payload()
        interaction_session_id = self.realtime_input_runtime_owner.current_interaction_state().interaction_session_id
        if notice_context is None:
            # 调用方可复用已冻结的 Notice 批次；缺失时才按 preview 构造，避免重复预留。
            notice_context = self.prepare_notice_context(
                interaction_session_id=interaction_session_id,
                reserve_batch=False,
                request_id=None,
                request_source=None,
            )
        workload_profile = material_workload_profile or _inactive_material_profile()
        return {
            **notice_context,
            "pending_work_interactions": (
                self.pending_work_interactions_provider(interaction_session_id)
                if self.pending_work_interactions_provider is not None
                else []
            ),
            "environment_snapshot": resolved_environment_payload,
            "active_turn_material_context": (
                resolved_turn_material_context.to_payload()
                if resolved_turn_material_context is not None
                else None
            ),
            "turn_material_summary": build_foreground_turn_material_summary(
                resolved_turn_material_context
            ),
            # 近期路径能力与聊天历史分离；请求冻结后即使全局 LRU 变化，本轮也只读这份快照。
            "recent_materials": [
                item.to_foreground_payload()
                for item in self.hub.recent_materials.snapshot()
            ],
            "material_workload_profile": workload_profile.to_payload(),
            "has_base_material_inputs": bool(
                resolved_turn_material_context
                and (
                    resolved_turn_material_context.attachments
                    or resolved_turn_material_context.preflight_failures
                )
            ),
        }

    def prepare_final_asr_request_context(
        self,
        *,
        request_id: str,
        source_turn_id: str,
    ) -> PreparedAssistantRequestContext:
        """为 Final ASR 请求原子冻结材料、环境、角色和 Notice 事务。

        语音输入先把待提交材料绑定到 source turn，再进入通用 ``prepare``。若后者失败，
        抛出携带材料上下文编号的异常，使输入层能够精确释放本轮材料而不影响其它轮次。

        Args:
            request_id: Final ASR 生成的助手请求编号。
            source_turn_id: Final ASR 所属的语音轮次编号。

        Returns:
            带有提交或回滚语义的请求准备结果。
        """

        turn_material_context_id = self.freeze_pending_materials_for_final_asr_turn(
            source_turn_id=source_turn_id,
        )
        try:
            return self.prepare(
                request_id=request_id,
                request_source="final_asr",
                turn_material_context_id=turn_material_context_id,
                source_turn_id=source_turn_id,
            )
        except Exception as exception:
            raise AssistantRequestContextPreparationError(
                turn_material_context_id
            ) from exception

    def _freeze_environment_snapshot(
        self,
        *,
        request_source: str | None,
        environment_snapshot_id: str | None,
        source_turn_id: str | None,
    ) -> FrozenEnvironmentSnapshot | None:
        """为一次助手请求冻结与来源严格匹配的环境快照。

        Args:
            request_source: 文本、final ASR 或系统通知来源。
            environment_snapshot_id: 文本提交显式携带的快照编号。
            source_turn_id: final ASR 所属语音 turn。

        Returns:
            不可变环境快照；不存在时返回 None 或同 turn 超时快照。
        """

        if request_source == "text_input":
            return self.hub.environment.claim_snapshot_by_id(environment_snapshot_id)
        if request_source == "final_asr":
            # 语音 turn 的截图请求会在“用户开口”那一刻并行发给 Desktop；
            # 这里额外等待 0.2 秒，是为了吃到那张同 turn 快照的正常上传延迟，
            # 但又不能等太久，否则 final ASR 的前台响应会被桌面截图拖慢。
            snapshot = self.hub.environment.wait_and_claim_for_turn(
                source_turn_id, timeout_seconds=0.2
            )
            if snapshot is not None:
                return snapshot
            normalized_turn_id = source_turn_id.strip() if source_turn_id else None
            # 超时后只构造“本 turn 没拿到图”的占位快照，绝不回退去偷用别的 turn 的图片。
            return FrozenEnvironmentSnapshot(
                snapshot_id=f"environment_timeout_{normalized_turn_id or 'unknown'}",
                captured_at=local_iso_datetime_text(),
                active_window_title=None,
                screenshot_mime_type=None,
                screenshot_bytes=None,
                capture_error="environment_capture_timeout",
                turn_id=normalized_turn_id,
                reason="voice_turn_started",
            )
        return None


def _notice_activity_keys(
    foreground_context: ForegroundContext,
) -> set[tuple[str, str]]:
    """提取当前 Notice 批次明确引用的 Activity 身份。

    Args:
        foreground_context: 已冻结且尚未交给助手子进程的前台态势。

    Returns:
        清洗后的来源与 Activity 编号集合。
    """

    raw_notices = foreground_context.get("notice_batch")
    if not isinstance(raw_notices, list):
        return set()
    activity_keys: set[tuple[str, str]] = set()
    for notice in raw_notices:
        if not isinstance(notice, dict):
            continue
        raw_source = notice.get("source")
        raw_activity_id = notice.get("activity_id")
        source = raw_source.strip() if isinstance(raw_source, str) else ""
        activity_id = (
            raw_activity_id.strip()
            if isinstance(raw_activity_id, str)
            else ""
        )
        if source and activity_id:
            activity_keys.add((source, activity_id))
    return activity_keys


def _cognitive_result_activity_ids(
    foreground_context: ForegroundContext,
) -> tuple[str, ...]:
    """提取当前请求真正依赖的 Cognitive Result Activity 编号。

    Progress、纠正表达和其它来源的 Notice 都有各自的历史语义，不能混进这个字段。
    这里只冻结 Cognitive 成果回复的来源，后续任务替代时才能只淘汰基于旧成果的回答。

    Args:
        foreground_context: 已冻结且尚未交给助手子进程的前台态势。

    Returns:
        按 Notice 首次出现顺序去重后的 Activity 编号。
    """

    raw_notices = foreground_context.get("notice_batch")
    if not isinstance(raw_notices, list):
        return ()
    activity_ids: list[str] = []
    seen_activity_ids: set[str] = set()
    for notice in raw_notices:
        if not isinstance(notice, dict):
            continue
        if notice.get("source") != "cognitive" or notice.get("intent") != "cognitive_result":
            continue
        raw_activity_id = notice.get("activity_id")
        activity_id = (
            raw_activity_id.strip()
            if isinstance(raw_activity_id, str)
            else ""
        )
        if not activity_id or activity_id in seen_activity_ids:
            continue
        seen_activity_ids.add(activity_id)
        activity_ids.append(activity_id)
    return tuple(activity_ids)
