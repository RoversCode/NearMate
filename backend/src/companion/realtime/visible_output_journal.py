"""在 Backend 主进程记录真正成功广播的助手文字。"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from threading import RLock
from uuid import uuid4

from companion.realtime.assistant_output_contracts import (
    ActivityProgressReference,
    AssistantHistoryPolicy,
    AssistantOutputSettlementOutcome,
    normalize_progress_references,
)
from companion.realtime.runtime_identity import AssistantRequestIdentityConflict
from companion.runtime_memory.models import MemoryCommit


@dataclass(frozen=True, slots=True)
class VisibleOutputSettlement:
    """描述一次助手请求已经不能再产生可见正文的事实。

    Args:
        settlement_id: 本次结算的幂等编号。
        request_id: 被结算的助手请求编号。
        generation_id: 子进程实际执行本请求的 generation 编号。
        history_policy: 可见正文应进入稳定对话、Progress 临时账本，
            还是未处理输入 Episode。
        outcome: 本请求停止输出的稳定原因分类。
        visible_text: 成功广播且尚未被稳定 MemoryCommit 覆盖的模型正文。
        progress_references: Progress 正文关联的 Activity 冻结进度。
        derived_from_activity_ids: 本次可见表达依赖的 Cognitive Result Activity 编号。
        settled_at: 结算事件在运行时产生的单调时间。
        request_source: 本次输出所属的 AssistantRequest 来源。
        uncommitted_visible_text: 完整 MemoryCommit 尚未覆盖的可见正文尾部。
        memory_commit: 广播后尚未成功写入主进程的协议完整历史提交。
    """

    settlement_id: str
    request_id: str
    generation_id: str | None
    history_policy: AssistantHistoryPolicy
    outcome: AssistantOutputSettlementOutcome
    visible_text: str
    progress_references: tuple[ActivityProgressReference, ...]
    derived_from_activity_ids: tuple[str, ...]
    settled_at: float
    request_source: str = ""
    uncommitted_visible_text: str | None = None
    memory_commit: MemoryCommit | None = None
    self_reflection_id: str | None = None
    episode_id: str | None = None
    trigger_input_identity: str | None = None
    runtime_epoch: int | None = None


@dataclass(frozen=True, slots=True)
class VisibleOutputAdmission:
    """表示一条正文已经通过广播前身份门禁，等待提交结果。"""

    token: str
    event_type: str
    payload: dict[str, object]


@dataclass(slots=True)
class _VisibleOutputRecord:
    """保存单个请求尚未结算的主进程可见正文。

    Args:
        request_id: 当前助手请求编号。
        generation_id: 子进程实际执行请求的 generation 编号。
        history_policy: 正文进入稳定对话、Progress 临时账本，
            还是未处理输入 Episode。
        progress_references: 纯 Progress 请求关联的 Activity 身份。
        derived_from_activity_ids: 本请求表达依赖的 Cognitive Result Activity 编号。
        visible_text: 当前已经成功广播的完整模型正文。
        pending_memory_commit: 尚未成功写入主进程的最新完整历史提交。
        pending_commit_visible_text: 最新提交已经覆盖到的可见正文位置。
    """

    request_id: str
    generation_id: str | None
    history_policy: AssistantHistoryPolicy
    progress_references: tuple[ActivityProgressReference, ...]
    derived_from_activity_ids: tuple[str, ...]
    visible_text: str = ""
    pending_memory_commit: MemoryCommit | None = None
    pending_commit_visible_text: str = ""
    self_reflection_id: str | None = None
    episode_id: str | None = None
    trigger_input_identity: str | None = None
    runtime_epoch: int | None = None


@dataclass(frozen=True, slots=True)
class _SettledOutputIdentity:
    """保存永久结算墓碑需要继续校验的完整身份。

    Args:
        generation_id: 子进程实际执行本请求的 generation 编号。
        history_policy: 正文进入稳定对话还是 Progress 临时账本。
        progress_references: 本请求关联的 Activity Progress 引用。
        derived_from_activity_ids: 本请求依赖的 Cognitive Result 编号。
        self_reflection_id: 所属 Self-Reflection 编号。
        episode_id: 所属未处理插话片段编号。
        trigger_input_identity: 触发辅助回应的输入身份。
        runtime_epoch: 所属 Assistant Runtime 世代。
    """

    generation_id: str | None
    history_policy: AssistantHistoryPolicy
    progress_references: tuple[ActivityProgressReference, ...]
    derived_from_activity_ids: tuple[str, ...]
    self_reflection_id: str | None
    episode_id: str | None
    trigger_input_identity: str | None
    runtime_epoch: int | None


class VisibleOutputJournal:
    """记录广播成功的文本，并为每个请求只生成一次结算。

    Args:
        max_open_requests: 异常情况下允许同时保留的未结算请求数量。
        settled_request_ids: Backend 进程生命周期内永久保留的已结算请求身份。
    """

    def __init__(
        self,
        *,
        max_open_requests: int = 256,
    ) -> None:
        """初始化空的可见输出账本。

        Args:
            max_open_requests: 未结算请求的容量上限。
        Raises:
            ValueError: 任一容量不是正整数时抛出。
        """

        if max_open_requests <= 0:
            raise ValueError("max_open_requests 必须大于零")
        self._max_open_requests = max_open_requests
        self._records: OrderedDict[str, _VisibleOutputRecord] = OrderedDict()
        # 请求编号在 Backend 进程内不会复用。永久 tombstone 防止极迟 settlement 在容量
        # 淘汰后重新生成一条空结算并再次释放 Turn。
        self._settled_request_ids: set[str] = set()
        self._settled_identities: dict[str, _SettledOutputIdentity] = {}
        self._pending_admissions: dict[str, VisibleOutputAdmission] = {}
        self._lock = RLock()

    def prepare_public_text_event(
        self,
        event_type: str,
        payload: dict[str, object],
    ) -> VisibleOutputAdmission | None:
        """在正文广播前校验身份，迟到正文直接返回空。"""

        if event_type not in {
            "assistant.text_chunk",
            "assistant.text_segment_final",
            "assistant.text_final",
        }:
            return None
        request_id = _clean_text(payload.get("request_id"))
        if not request_id:
            return None
        history_policy = _history_policy(payload.get("history_policy"))
        generation_id = _clean_text(payload.get("generation_id")) or None
        references = normalize_progress_references(
            payload.get("activity_progress_references")
        )
        derived_from_activity_ids = _normalize_activity_ids(
            payload.get("derived_from_activity_ids")
        )
        self_reflection_id = _optional_text(payload.get("self_reflection_id"))
        episode_id = _optional_text(payload.get("episode_id"))
        trigger_input_identity = _optional_text(
            payload.get("trigger_input_identity")
        )
        runtime_epoch = _optional_int(payload.get("runtime_epoch"))
        memory_commit = _memory_commit(
            payload.get("memory_commit"),
            expected_request_id=request_id,
            derived_from_activity_ids=derived_from_activity_ids,
        )
        if memory_commit is not None and history_policy != "stable_dialogue":
            raise ValueError("非稳定对话输出不能携带稳定 MemoryCommit")
        with self._lock:
            if request_id in self._settled_request_ids:
                self._validate_settled_identity(
                    request_id,
                    generation_id=generation_id,
                    history_policy=history_policy,
                    progress_references=references,
                    derived_from_activity_ids=derived_from_activity_ids,
                    self_reflection_id=self_reflection_id,
                    episode_id=episode_id,
                    trigger_input_identity=trigger_input_identity,
                    runtime_epoch=runtime_epoch,
                )
                return None
            record = self._records.get(request_id)
            if record is not None:
                self._validate_identity(
                    record,
                    generation_id=generation_id,
                    history_policy=history_policy,
                    progress_references=references,
                    derived_from_activity_ids=derived_from_activity_ids,
                )
                self._validate_reflection_identity(
                    record,
                    self_reflection_id=self_reflection_id,
                    episode_id=episode_id,
                    trigger_input_identity=trigger_input_identity,
                    runtime_epoch=runtime_epoch,
                )
            token = f"visible-output-admission:{uuid4()}"
            admission = VisibleOutputAdmission(
                token=token,
                event_type=event_type,
                payload=deepcopy(payload),
            )
            self._pending_admissions[token] = admission
            return admission

    def commit_public_text_event(self, admission: VisibleOutputAdmission) -> bool:
        """在正文广播成功后提交 admission，写入可见正文账本。"""

        with self._lock:
            # 只有确定得到成功或拒绝结果后才消费 admission。若容量或账本写入出现
            # 临时异常，Coordinator 必须能用同一令牌重试，不能让已广播正文失去归档依据。
            pending = self._pending_admissions.get(admission.token)
            if pending is None:
                return False
            recorded = self._record_public_text_event_locked(
                pending.event_type,
                pending.payload,
            )
            self._pending_admissions.pop(admission.token, None)
            return recorded

    def abort_public_text_event(self, admission: VisibleOutputAdmission) -> None:
        """广播失败时撤销 admission，不留下任何可见正文记录。"""

        with self._lock:
            self._pending_admissions.pop(admission.token, None)

    def record_public_text_event(
        self,
        event_type: str,
        payload: dict[str, object],
    ) -> bool:
        """在公共广播成功后记录一条 assistant 文本事件。

        Args:
            event_type: 已完成广播的 assistant 事件类型。
            payload: 广播前保留内部结算字段的事件载荷。

        Returns:
            事件身份和正文有效且已经写入账本时返回真。
        """

        admission = self.prepare_public_text_event(event_type, payload)
        if admission is None:
            return False
        return self.commit_public_text_event(admission)

    def validate_settlement(self, payload: dict[str, object]) -> None:
        """只读校验 settlement 身份，不创建墓碑或释放任何业务状态。"""

        request_id = _clean_text(payload.get("request_id"))
        if not request_id:
            return
        history_policy = _history_policy(payload.get("history_policy"))
        generation_id = _clean_text(payload.get("generation_id")) or None
        references = normalize_progress_references(
            payload.get("activity_progress_references")
        )
        derived_from_activity_ids = _normalize_activity_ids(
            payload.get("derived_from_activity_ids")
        )
        self_reflection_id = _optional_text(payload.get("self_reflection_id"))
        episode_id = _optional_text(payload.get("episode_id"))
        trigger_input_identity = _optional_text(
            payload.get("trigger_input_identity")
        )
        runtime_epoch = _optional_int(payload.get("runtime_epoch"))
        with self._lock:
            if request_id in self._settled_request_ids:
                self._validate_settled_identity(
                    request_id,
                    generation_id=generation_id,
                    history_policy=history_policy,
                    progress_references=references,
                    derived_from_activity_ids=derived_from_activity_ids,
                    self_reflection_id=self_reflection_id,
                    episode_id=episode_id,
                    trigger_input_identity=trigger_input_identity,
                    runtime_epoch=runtime_epoch,
                )
                return
            record = self._records.get(request_id)
            if record is not None:
                self._validate_identity(
                    record,
                    generation_id=generation_id,
                    history_policy=history_policy,
                    progress_references=references,
                    derived_from_activity_ids=derived_from_activity_ids,
                )
                self._validate_reflection_identity(
                    record,
                    self_reflection_id=self_reflection_id,
                    episode_id=episode_id,
                    trigger_input_identity=trigger_input_identity,
                    runtime_epoch=runtime_epoch,
                )

    def mark_stable_history_committed(self, request_id: str) -> bool:
        """清除已经由成功 MemoryCommit 覆盖的可见正文。

        Args:
            request_id: MemoryCommit 所属的助手请求编号。

        Returns:
            找到未结算请求并清除了待补偿正文时返回真。
        """

        normalized_request_id = request_id.strip()
        if not normalized_request_id:
            return False
        with self._lock:
            record = self._records.get(normalized_request_id)
            if record is None:
                return False
            record.visible_text = ""
            record.pending_memory_commit = None
            record.pending_commit_visible_text = ""
            return True

    def settle(
        self,
        payload: dict[str, object],
        *,
        settled_at: float,
        commit_settlement: Callable[[VisibleOutputSettlement], None],
    ) -> VisibleOutputSettlement | None:
        """归档完成后关闭一次请求的可见输出。

        Args:
            payload: 子进程或 Bridge 生成的结算事件载荷。
            settled_at: 结算事件的单调时间。
            commit_settlement: 把正文归入正确账本并释放 Turn 屏障的同步事务。

        Returns:
            首次合法结算产生的不可变结果；重复或身份无效时返回空。
        """

        request_id = _clean_text(payload.get("request_id"))
        if not request_id:
            return None
        settlement_id = _clean_text(payload.get("settlement_id"))
        if not settlement_id:
            settlement_id = f"output-settlement:{request_id}"
        history_policy = _history_policy(payload.get("history_policy"))
        outcome = _settlement_outcome(payload.get("outcome"))
        generation_id = _clean_text(payload.get("generation_id")) or None
        request_source = _clean_text(payload.get("source"))
        references = normalize_progress_references(
            payload.get("activity_progress_references")
        )
        derived_from_activity_ids = _normalize_activity_ids(
            payload.get("derived_from_activity_ids")
        )
        self_reflection_id = _optional_text(payload.get("self_reflection_id"))
        episode_id = _optional_text(payload.get("episode_id"))
        trigger_input_identity = _optional_text(
            payload.get("trigger_input_identity")
        )
        runtime_epoch = _optional_int(payload.get("runtime_epoch"))

        with self._lock:
            if request_id in self._settled_request_ids:
                self._validate_settled_identity(
                    request_id,
                    generation_id=generation_id,
                    history_policy=history_policy,
                    progress_references=references,
                    derived_from_activity_ids=derived_from_activity_ids,
                    self_reflection_id=self_reflection_id,
                    episode_id=episode_id,
                    trigger_input_identity=trigger_input_identity,
                    runtime_epoch=runtime_epoch,
                )
                return None
            if any(
                _clean_text(admission.payload.get("request_id")) == request_id
                for admission in self._pending_admissions.values()
            ):
                # settlement 不能越过已经公开但尚未提交的正文 admission。即使未来有调用方
                # 绕开 Coordinator，Journal 这一层也必须守住永久墓碑的最终边界。
                raise RuntimeError("visible_output_commit_pending")
            record = self._records.get(request_id)
            if record is not None:
                self._validate_identity(
                    record,
                    generation_id=generation_id,
                    history_policy=history_policy,
                    progress_references=references,
                    derived_from_activity_ids=derived_from_activity_ids,
                )
                self._validate_reflection_identity(
                    record,
                    self_reflection_id=self_reflection_id,
                    episode_id=episode_id,
                    trigger_input_identity=trigger_input_identity,
                    runtime_epoch=runtime_epoch,
                )
                raw_visible_text = record.visible_text
                visible_text = raw_visible_text.strip()
                memory_commit = record.pending_memory_commit
                covered_text = record.pending_commit_visible_text
                if memory_commit is None:
                    uncommitted_visible_text = visible_text
                elif raw_visible_text.startswith(covered_text):
                    uncommitted_visible_text = raw_visible_text[
                        len(covered_text):
                    ].strip()
                else:
                    # 理论上 final 必须延续同一请求的公开正文。若 Provider 违反这个约束，
                    # 宁可补交完整可见文字造成幂等去重，也不能静默丢失用户实际看到的内容。
                    uncommitted_visible_text = visible_text
                history_policy = record.history_policy
                generation_id = record.generation_id or generation_id
                references = record.progress_references or references
                derived_from_activity_ids = record.derived_from_activity_ids
                self_reflection_id = record.self_reflection_id
                episode_id = record.episode_id
                trigger_input_identity = record.trigger_input_identity
                runtime_epoch = record.runtime_epoch
            else:
                visible_text = ""
                uncommitted_visible_text = ""
                memory_commit = None
                self_reflection_id = _optional_text(payload.get("self_reflection_id"))
                episode_id = _optional_text(payload.get("episode_id"))
                trigger_input_identity = _optional_text(payload.get("trigger_input_identity"))
                runtime_epoch = _optional_int(payload.get("runtime_epoch"))

            settlement = VisibleOutputSettlement(
                settlement_id=settlement_id,
                request_id=request_id,
                generation_id=generation_id,
                history_policy=history_policy,
                outcome=outcome,
                visible_text=visible_text,
                progress_references=references,
                derived_from_activity_ids=derived_from_activity_ids,
                settled_at=settled_at,
                request_source=request_source,
                uncommitted_visible_text=uncommitted_visible_text,
                memory_commit=memory_commit,
                self_reflection_id=self_reflection_id,
                episode_id=episode_id,
                trigger_input_identity=trigger_input_identity,
                runtime_epoch=runtime_epoch,
            )

            # 归档和 Turn 放行都成功后才消费 journal。任一步骤抛错时原正文仍在，
            # 同一 settlement 重新入队后可以安全重试，不能形成“已结算但未归档”的假事实。
            commit_settlement(settlement)
            self._records.pop(request_id, None)
            self._settled_request_ids.add(request_id)
            self._settled_identities[request_id] = _SettledOutputIdentity(
                generation_id=generation_id,
                history_policy=history_policy,
                progress_references=references,
                derived_from_activity_ids=derived_from_activity_ids,
                self_reflection_id=self_reflection_id,
                episode_id=episode_id,
                trigger_input_identity=trigger_input_identity,
                runtime_epoch=runtime_epoch,
            )
            return settlement

    def _record_public_text_event_locked(
        self,
        event_type: str,
        payload: dict[str, object],
    ) -> bool:
        """在已持有 Journal 锁时提交一条正文事件。"""

        request_id = _clean_text(payload.get("request_id"))
        if not request_id or request_id in self._settled_request_ids:
            return False
        history_policy = _history_policy(payload.get("history_policy"))
        generation_id = _clean_text(payload.get("generation_id")) or None
        references = normalize_progress_references(
            payload.get("activity_progress_references")
        )
        derived_from_activity_ids = _normalize_activity_ids(
            payload.get("derived_from_activity_ids")
        )
        memory_commit = _memory_commit(
            payload.get("memory_commit"),
            expected_request_id=request_id,
            derived_from_activity_ids=derived_from_activity_ids,
        )
        if memory_commit is not None and history_policy != "stable_dialogue":
            raise ValueError("非稳定对话输出不能携带稳定 MemoryCommit")
        record = self._get_or_create_record(
            request_id=request_id,
            generation_id=generation_id,
            history_policy=history_policy,
            progress_references=references,
            derived_from_activity_ids=derived_from_activity_ids,
            self_reflection_id=_optional_text(payload.get("self_reflection_id")),
            episode_id=_optional_text(payload.get("episode_id")),
            trigger_input_identity=_optional_text(payload.get("trigger_input_identity")),
            runtime_epoch=_optional_int(payload.get("runtime_epoch")),
        )
        if event_type == "assistant.text_chunk":
            text = payload.get("text")
            if not isinstance(text, str) or not text:
                return False
            record.visible_text += text
            return True
        model_text = payload.get("settlement_text", payload.get("text"))
        if not isinstance(model_text, str):
            return False
        if model_text or not record.visible_text:
            record.visible_text = model_text
        if memory_commit is not None:
            record.pending_memory_commit = memory_commit
            record.pending_commit_visible_text = record.visible_text
        return True

    def _validate_settled_identity(
        self,
        request_id: str,
        *,
        generation_id: str | None,
        history_policy: AssistantHistoryPolicy,
        progress_references: tuple[ActivityProgressReference, ...],
        derived_from_activity_ids: tuple[str, ...],
        self_reflection_id: str | None,
        episode_id: str | None,
        trigger_input_identity: str | None,
        runtime_epoch: int | None,
    ) -> None:
        """校验墓碑后的事件仍然属于原执行身份。

        Args:
            request_id: 已经结算的助手请求编号。
            generation_id: 新事件携带的 generation 编号。
            history_policy: 新事件携带的历史策略。
            progress_references: 新事件携带的 Progress 引用。
            derived_from_activity_ids: 新事件携带的 Cognitive Result 来源编号。
            self_reflection_id: 新事件携带的 Self-Reflection 编号。
            episode_id: 新事件携带的未处理插话片段编号。
            trigger_input_identity: 新事件携带的触发输入身份。
            runtime_epoch: 新事件携带的 Assistant Runtime 世代。

        Raises:
            AssistantRequestIdentityConflict: 事件身份与永久墓碑不一致时抛出。
        """

        settled_identity = self._settled_identities.get(request_id)
        if settled_identity is None:
            return
        expected_generation = settled_identity.generation_id
        expected_policy = settled_identity.history_policy
        expected_references = settled_identity.progress_references
        expected_derived_from_activity_ids = (
            settled_identity.derived_from_activity_ids
        )
        if (
            expected_generation is not None
            and generation_id is not None
            and expected_generation != generation_id
        ):
            raise AssistantRequestIdentityConflict(
                request_id=request_id,
                field="generation_id",
                expected=expected_generation,
                actual=generation_id,
            )
        if expected_policy != history_policy:
            raise AssistantRequestIdentityConflict(
                request_id=request_id,
                field="history_policy",
                expected=expected_policy,
                actual=history_policy,
            )
        if (
            expected_references
            and progress_references
            and expected_references != progress_references
        ):
            raise AssistantRequestIdentityConflict(
                request_id=request_id,
                field="activity_progress_references",
                expected=expected_references,
                actual=progress_references,
            )
        if expected_derived_from_activity_ids != derived_from_activity_ids:
            raise AssistantRequestIdentityConflict(
                request_id=request_id,
                field="derived_from_activity_ids",
                expected=expected_derived_from_activity_ids,
                actual=derived_from_activity_ids,
            )
        for field_name, expected, actual in (
            (
                "self_reflection_id",
                settled_identity.self_reflection_id,
                self_reflection_id,
            ),
            ("episode_id", settled_identity.episode_id, episode_id),
            (
                "trigger_input_identity",
                settled_identity.trigger_input_identity,
                trigger_input_identity,
            ),
            ("runtime_epoch", settled_identity.runtime_epoch, runtime_epoch),
        ):
            if expected != actual and (expected is not None or actual is not None):
                raise AssistantRequestIdentityConflict(
                    request_id=request_id,
                    field=field_name,
                    expected=expected,
                    actual=actual,
                )

    def _get_or_create_record(
        self,
        *,
        request_id: str,
        generation_id: str | None,
        history_policy: AssistantHistoryPolicy,
        progress_references: tuple[ActivityProgressReference, ...],
        derived_from_activity_ids: tuple[str, ...],
        self_reflection_id: str | None = None,
        episode_id: str | None = None,
        trigger_input_identity: str | None = None,
        runtime_epoch: int | None = None,
    ) -> _VisibleOutputRecord:
        """取得同一请求的记录，并拒绝跨 generation 或策略污染。

        Args:
            request_id: 当前 assistant 请求编号。
            generation_id: 当前 generation 编号。
            history_policy: 当前请求的历史写入策略。
            progress_references: 当前请求关联的 Activity Progress。
            derived_from_activity_ids: 当前请求依赖的 Cognitive Result Activity 编号。

        Returns:
            已存在或新创建的可见输出记录。
        """

        record = self._records.get(request_id)
        if record is not None:
            self._validate_identity(
                record,
                generation_id=generation_id,
                history_policy=history_policy,
                progress_references=progress_references,
                derived_from_activity_ids=derived_from_activity_ids,
            )
            self._validate_reflection_identity(
                record,
                self_reflection_id=self_reflection_id,
                episode_id=episode_id,
                trigger_input_identity=trigger_input_identity,
                runtime_epoch=runtime_epoch,
            )
            return record
        if len(self._records) >= self._max_open_requests:
            raise RuntimeError("可见输出未结算请求已达到容量上限")
        record = _VisibleOutputRecord(
            request_id=request_id,
            generation_id=generation_id,
            history_policy=history_policy,
            progress_references=progress_references,
            derived_from_activity_ids=derived_from_activity_ids,
            self_reflection_id=self_reflection_id,
            episode_id=episode_id,
            trigger_input_identity=trigger_input_identity,
            runtime_epoch=runtime_epoch,
        )
        self._records[request_id] = record
        return record

    def _validate_reflection_identity(
        self,
        record: _VisibleOutputRecord,
        *,
        self_reflection_id: str | None,
        episode_id: str | None,
        trigger_input_identity: str | None,
        runtime_epoch: int | None,
    ) -> None:
        """校验 Self-Reflection 输出没有混入另一条反思请求。

        Args:
            record: 已经登记的可见输出记录。
            self_reflection_id: 新事件携带的 Self-Reflection 编号。
            episode_id: 新事件携带的未处理插话片段编号。
            trigger_input_identity: 新事件携带的触发输入身份。
            runtime_epoch: 新事件携带的运行时世代。

        Raises:
            AssistantRequestIdentityConflict: 反思身份被删除或改写时抛出。
        """

        for field_name, actual in (
            ("self_reflection_id", self_reflection_id),
            ("episode_id", episode_id),
            ("trigger_input_identity", trigger_input_identity),
            ("runtime_epoch", runtime_epoch),
        ):
            expected = getattr(record, field_name)
            if expected != actual and (expected is not None or actual is not None):
                raise AssistantRequestIdentityConflict(
                    request_id=record.request_id,
                    field=field_name,
                    expected=expected,
                    actual=actual,
                )

    def _validate_identity(
        self,
        record: _VisibleOutputRecord,
        *,
        generation_id: str | None,
        history_policy: AssistantHistoryPolicy,
        progress_references: tuple[ActivityProgressReference, ...],
        derived_from_activity_ids: tuple[str, ...],
    ) -> None:
        """校验同一 request_id 的输出事实没有跨 generation 混写。

        Args:
            record: 已存在的请求记录。
            generation_id: 新事件携带的 generation 编号。
            history_policy: 新事件携带的历史策略。
            progress_references: 新事件携带的 Activity Progress 引用。
            derived_from_activity_ids: 新事件携带的 Cognitive Result 来源编号。

        Raises:
            AssistantRequestIdentityConflict: 已记录请求的执行或归档身份被改写时抛出。
        """

        if (
            record.generation_id is not None
            and generation_id is not None
            and record.generation_id != generation_id
        ):
            raise AssistantRequestIdentityConflict(
                request_id=record.request_id,
                field="generation_id",
                expected=record.generation_id,
                actual=generation_id,
            )
        if record.history_policy != history_policy:
            raise AssistantRequestIdentityConflict(
                request_id=record.request_id,
                field="history_policy",
                expected=record.history_policy,
                actual=history_policy,
            )
        if (
            record.progress_references
            and progress_references
            and record.progress_references != progress_references
        ):
            raise AssistantRequestIdentityConflict(
                request_id=record.request_id,
                field="activity_progress_references",
                expected=record.progress_references,
                actual=progress_references,
            )
        if record.derived_from_activity_ids != derived_from_activity_ids:
            raise AssistantRequestIdentityConflict(
                request_id=record.request_id,
                field="derived_from_activity_ids",
                expected=record.derived_from_activity_ids,
                actual=derived_from_activity_ids,
            )


def _clean_text(value: object) -> str:
    """把未知字段清洗为无首尾空白的字符串。

    Args:
        value: 可能来自跨进程事件的任意值。

    Returns:
        清洗后的字符串；类型不符时返回空串。
    """

    return value.strip() if isinstance(value, str) else ""


def _memory_commit(
    value: object,
    *,
    expected_request_id: str,
    derived_from_activity_ids: tuple[str, ...],
) -> MemoryCommit | None:
    """把跨进程历史提交复制成不可变的 Journal 事实。

    Args:
        value: 文本事件携带的原始 MemoryCommit 字典。
        expected_request_id: 外层 assistant 事件的请求编号。
        derived_from_activity_ids: 外层请求冻结的 Cognitive Result 来源编号。

    Returns:
        合法的 MemoryCommit；字段不存在时返回空。

    Raises:
        ValueError: 提交身份或消息结构不完整。
    """

    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("memory_commit 必须是对象")
    commit_id = _clean_text(value.get("commit_id"))
    request_id = _clean_text(value.get("request_id"))
    base_memory_epoch = value.get("base_memory_epoch")
    raw_messages = value.get("messages")
    if (
        not commit_id
        or request_id != expected_request_id
        or not isinstance(base_memory_epoch, int)
        or isinstance(base_memory_epoch, bool)
        or not isinstance(raw_messages, list)
        or not all(isinstance(message, dict) for message in raw_messages)
    ):
        raise ValueError("memory_commit 身份或消息结构非法")
    messages: list[dict[str, object]] = []
    for raw_message in raw_messages:
        message = deepcopy(raw_message)
        if message.get("role") == "assistant" and derived_from_activity_ids:
            raw_metadata = message.get("metadata")
            metadata = dict(raw_metadata) if isinstance(raw_metadata, dict) else {}
            metadata["derived_from_activity_ids"] = list(
                derived_from_activity_ids
            )
            message["metadata"] = metadata
        messages.append(message)
    return MemoryCommit(
        commit_id=commit_id,
        request_id=request_id,
        base_memory_epoch=base_memory_epoch,
        messages=tuple(messages),
    )


def _normalize_activity_ids(value: object) -> tuple[str, ...]:
    """把跨进程 Activity 编号清洗为有序不可变集合。

    Args:
        value: RuntimeEvent 中的 Activity 编号列表。

    Returns:
        按首次出现顺序去重后的非空编号。
    """

    if not isinstance(value, (list, tuple)):
        return ()
    normalized: list[str] = []
    seen: set[str] = set()
    for raw_activity_id in value:
        activity_id = _clean_text(raw_activity_id)
        if not activity_id or activity_id in seen:
            continue
        seen.add(activity_id)
        normalized.append(activity_id)
    return tuple(normalized)


def _history_policy(value: object) -> AssistantHistoryPolicy:
    """把跨进程字段收窄为合法历史策略。

    Args:
        value: RuntimeEvent 中的原始策略值。

    Returns:
        合法值原样返回；缺失时使用稳定对话策略。
    """

    if value == "activity_progress":
        return "activity_progress"
    if value == "unattended_episode":
        return "unattended_episode"
    return "stable_dialogue"


def _optional_text(value: object) -> str | None:
    """清洗可选的 episode 身份字段。

    Args:
        value: Runtime Event 中可能为空的身份值。

    Returns:
        去除首尾空白的非空文本；无效值返回空。
    """

    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _optional_int(value: object) -> int | None:
    """清洗可选的运行时世代。

    Args:
        value: Runtime Event 中可能无效的世代值。

    Returns:
        大于零的整数世代；无效值返回空。
    """

    if type(value) is int and value > 0:
        return value
    return None


def _settlement_outcome(value: object) -> AssistantOutputSettlementOutcome:
    """把跨进程字段收窄为合法结算结果。

    Args:
        value: RuntimeEvent 中的原始结果值。

    Returns:
        合法结果；未知值按失败处理。
    """

    if value in {
        "completed",
        "interrupted",
        "superseded",
        "failed",
        "output_unavailable",
    }:
        return value
    return "failed"
