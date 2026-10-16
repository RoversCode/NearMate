"""主进程内保存 Foreground 聊天、摘要和任务详情。"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import json
from threading import RLock
from typing import Any
from uuid import uuid4

from companion.materials.foreground_view import render_material_history_note
from companion.core.local_clock import local_wall_datetime
from companion.llm.tool_protocol import project_tool_calls_for_runtime_memory
from companion.realtime.text_tokenizer import count_text_tokens
from companion.runtime_memory.compaction_pins import RecoveryCompactionPinRegistry
from companion.runtime_memory.models import (
    MemoryCommit,
    RuntimeMemoryMessage,
    RuntimeMemorySnapshot,
    RuntimeMemoryCompactionPlan,
    TaskMemoryLookupPurpose,
    TaskMemoryRecord,
    TaskRecoveryOutcome,
)
from companion.runtime_memory.task_memory import (
    render_task_memory,
    render_task_recovery,
    task_based_on_meaning,
    task_cancellation_meaning,
)
from companion.runtime_memory.token_accounting import protocol_message_token_text
from companion.runtime_memory.unattended_episode import (
    UNATTENDED_EPISODE_HISTORY_POLICY,
    UnattendedEpisodeIdentity,
    UnattendedInputAppendStatus,
    UnattendedInputEpisode,
    UnattendedInputItem,
    UnattendedInterruptionReply,
    UnattendedReplyAppendStatus,
    project_unattended_input_episode,
)
from companion.runtime_memory.unattended_episode_ledger import (
    UnattendedEpisodeLedger,
)


_TERMINAL_EVENT_STATES = {
    "cognitive_task.completed": ("cognitive", "completed"),
    "cognitive_task.failed": ("cognitive", "failed"),
    "cognitive_task.cancelled": ("cognitive", "cancelled"),
    "cognitive_task.interrupted": ("cognitive", "interrupted"),
    "background_agent.completed": ("background_agent", "completed"),
    "background_agent.failed": ("background_agent", "failed"),
    "background_agent.cancelled": ("background_agent", "cancelled"),
    "background_agent.interrupted": ("background_agent", "interrupted"),
}
class RuntimeMemoryStore:
    """保存 Backend 本次进程生命周期内的 Foreground 权威记忆。

    主进程只接受通过运行时世代和 generation 门禁的提交。助手子进程可以提出
    消息增量，但不能分配权威编号，也不能覆盖并发到达的 Activity 终态记忆。
    """

    def __init__(
        self,
        *,
        recovery_compaction_pins: RecoveryCompactionPinRegistry | None = None,
    ) -> None:
        """创建空的运行期记忆和幂等账本。

        Args:
            recovery_compaction_pins: 可选共享恢复关系压缩门禁。
        """

        self._memory_epoch = 0
        self._rolling_summary = ""
        self._summary_covered_from = None
        self._summary_covered_to = None
        self._summary_source_message_id: int | None = None
        self._messages: list[RuntimeMemoryMessage] = []
        self._next_message_id = 1
        self._context_token_count = 0
        self._committed_request_ids: set[str] = set()
        self._applied_commit_ids: set[str] = set()
        self._task_records: dict[str, TaskMemoryRecord] = {}
        self._task_origins: dict[str, dict[str, Any]] = {}
        self._task_interactions: dict[str, list[dict[str, Any]]] = {}
        self._recovery_source_by_activity: dict[str, str] = {}
        self._recovery_source_by_reflection: dict[str, str] = {}
        self._unattended_episode_ledger = UnattendedEpisodeLedger()
        self._active_compaction_plan: RuntimeMemoryCompactionPlan | None = None
        self._recovery_compaction_pins = (
            recovery_compaction_pins or RecoveryCompactionPinRegistry()
        )
        self._lock = RLock()

    @property
    def memory_epoch(self) -> int:
        """返回当前运行期记忆版本。

        Returns:
            每次权威突变后单调递增的版本号。
        """

        with self._lock:
            return self._memory_epoch

    def snapshot(self) -> RuntimeMemorySnapshot:
        """冻结一次可安全跨进程传递的记忆快照。

        Returns:
            包含滚动摘要和协议完整近期消息的不可变副本。
        """

        with self._lock:
            retained_messages = tuple(self._messages)
            return RuntimeMemorySnapshot(
                memory_epoch=self._memory_epoch,
                rolling_summary=self._rolling_summary,
                summary_covered_from=self._summary_covered_from,
                summary_covered_to=self._summary_covered_to,
                recent_messages=tuple(
                    RuntimeMemoryMessage(
                        message_id=message.message_id,
                        role=message.role,
                        content=message.content,
                        token_count=message.token_count,
                        created_at=message.created_at,
                        tool_calls=tuple(deepcopy(message.tool_calls)),
                        tool_call_id=message.tool_call_id,
                        llm_context_note=message.llm_context_note,
                        metadata=deepcopy(message.metadata),
                    )
                    for message in retained_messages
                ),
                summary_source_message_id=self._summary_source_message_id,
                context_token_count=self._context_token_count,
                latest_message_id=self._next_message_id - 1,
            )

    def retain_recovery_compaction_pin(self, self_reflection_id: str) -> bool:
        """在恢复关系可靠写入前阻止摘要越过半状态。

        Args:
            self_reflection_id: 当前前台自我反思编号。

        Returns:
            首次取得门禁时返回真。
        """

        # pin 与压缩计划必须共用 Store 的锁。否则摘要线程可能在恢复失败事实和
        # 最终恢复关系之间截断历史，把一个尚未收口的半状态写进滚动摘要。
        with self._lock:
            return self._recovery_compaction_pins.retain(self_reflection_id)

    def release_recovery_compaction_pin(self, self_reflection_id: str) -> bool:
        """释放一笔已经明确放弃、无需写入关系的恢复门禁。

        Args:
            self_reflection_id: 已经结束的前台自我反思编号。

        Returns:
            实际释放时返回真。
        """

        with self._lock:
            return self._recovery_compaction_pins.release(self_reflection_id)

    def open_unattended_input_episode(
        self,
        identity: UnattendedEpisodeIdentity,
        *,
        opened_at: datetime | None = None,
    ) -> bool:
        """幂等开启一段只收集未处理 Final 输入的片段。

        Args:
            identity: 自我反思会话、运行时世代和片段的联合身份。
            opened_at: Attention Admission 成立的本地时间。

        Returns:
            首次创建返回真，同一身份重复开启返回假。

        Raises:
            ValueError: 片段编号或同一反思会话已经绑定其它身份。
        """

        normalized_opened_at = opened_at or local_wall_datetime()
        with self._lock:
            return self._unattended_episode_ledger.open(
                identity,
                opened_at=normalized_opened_at,
            )

    def append_unattended_input(
        self,
        identity: UnattendedEpisodeIdentity,
        item: UnattendedInputItem,
    ) -> UnattendedInputAppendStatus:
        """按 Attention 锁分配的顺序幂等追加一项 Text 或 Final ASR。

        Args:
            identity: 当前未处理插话片段的联合身份。
            item: 已经越过输入提交边界的不可变用户输入。

        Returns:
            appended、duplicate、closed、not_open 或 ordinary_turn。

        Raises:
            ValueError: 同一稳定身份或顺序号被改写为其它内容。
        """

        if not isinstance(item, UnattendedInputItem):
            raise ValueError("item 必须是 UnattendedInputItem")
        with self._lock:
            existing_episode_identity = (
                self._unattended_episode_ledger.identity_for_input(
                    item.input_identity
                )
            )
            if (
                existing_episode_identity is None
                and item.input_identity in self._committed_request_ids
            ):
                # 普通 Turn 若先越过提交线性化点，就不能被事后倒改成 episode 输入。
                return "ordinary_turn"
            status = self._unattended_episode_ledger.append_input(identity, item)
            if status == "appended":
                # episode 输入已经成立，后续 settlement 或重连不能再把同一身份
                # 写进普通历史。
                self._committed_request_ids.add(item.input_identity)
            return status

    def close_unattended_input_episode(
        self,
        identity: UnattendedEpisodeIdentity,
        *,
        closed_at: datetime | None = None,
    ) -> UnattendedInputEpisode:
        """幂等关闭片段，并一次性把它投影成不可执行的稳定历史。

        Args:
            identity: 当前未处理插话片段的联合身份。
            closed_at: 输入 diversion lane 关闭的本地时间。

        Returns:
            首次关闭或重试读取到的同一份不可变片段。

        Raises:
            ValueError: 片段未开启、身份冲突或关闭时间无效。
        """

        normalized_closed_at = closed_at or local_wall_datetime()
        with self._lock:
            close_result = self._unattended_episode_ledger.preview_close(
                identity,
                closed_at=normalized_closed_at,
            )
            episode = close_result.episode
            if not close_result.newly_closed:
                return episode
            common_metadata: dict[str, object] = {
                "history_policy": UNATTENDED_EPISODE_HISTORY_POLICY,
                "self_reflection_id": identity.self_reflection_id,
                "runtime_epoch": identity.runtime_epoch,
                "episode_id": identity.episode_id,
                "actionable": False,
            }
            messages_to_commit: list[RuntimeMemoryMessage] = []
            for history_item in project_unattended_input_episode(episode):
                metadata = {
                    **common_metadata,
                    "episode_role": history_item.episode_role,
                }
                if history_item.source_identity is not None:
                    metadata["source_identity"] = history_item.source_identity
                if history_item.reply_settlement is not None:
                    metadata["reply_settlement"] = history_item.reply_settlement
                messages_to_commit.append(
                    self._build_message_locked(
                        role=history_item.role,
                        content=history_item.content,
                        created_at=history_item.occurred_at,
                        metadata=metadata,
                        message_id=self._next_message_id + len(messages_to_commit),
                    )
                )
            # 先把所有正文和 token 算完，再改变 ledger、消息列表和 memory epoch。
            # 这样材料异常或 token 计算失败时，下一次调用仍能从完整开放片段重试。
            self._unattended_episode_ledger.commit_close(episode)
            self._messages.extend(messages_to_commit)
            self._next_message_id += len(messages_to_commit)
            self._context_token_count += sum(
                message.token_count for message in messages_to_commit
            )
            self._memory_epoch += len(messages_to_commit)
            return episode

    def append_unattended_interruption_reply(
        self,
        identity: UnattendedEpisodeIdentity,
        reply: UnattendedInterruptionReply,
    ) -> UnattendedReplyAppendStatus:
        """幂等保存一条辅助回应的 committed settlement。

        Args:
            identity: 当前 Self-Reflection episode 的联合身份。
            reply: 已经停止输出和 TTS 的辅助回应事实。

        Returns:
            appended、duplicate、closed 或 not_open。
        """

        if not isinstance(reply, UnattendedInterruptionReply):
            raise ValueError("reply 必须是 UnattendedInterruptionReply")
        with self._lock:
            return self._unattended_episode_ledger.append_reply(identity, reply)

    def unattended_input_episode(
        self,
        identity: UnattendedEpisodeIdentity,
    ) -> UnattendedInputEpisode | None:
        """读取一段已经闭合的未处理插话片段。

        Args:
            identity: 自我反思会话、运行时世代和片段的联合身份。

        Returns:
            片段已闭合时返回不可变记录，否则返回空。
        """

        with self._lock:
            return self._unattended_episode_ledger.get(identity)

    def commit_request(self, request: object) -> RuntimeMemoryMessage | None:
        """在助手请求真正入队后记录真实用户消息。

        Args:
            request: 已进入 Bridge 或助手子进程的 AssistantRequest。

        Returns:
            首次记录的用户消息；非用户轮或重复请求返回空。
        """

        request_id = _clean_text(getattr(request, "request_id", ""))
        source = _clean_text(getattr(request, "source", ""))
        user_text = _clean_text(getattr(request, "user_text", ""))
        if not request_id or source not in {"text_input", "final_asr"} or not user_text:
            return None
        with self._lock:
            if request_id in self._committed_request_ids:
                return None
            foreground_context = getattr(request, "foreground_context", {})
            material_note = render_material_history_note(
                foreground_context.get("turn_material_summary")
                if isinstance(foreground_context, dict)
                else None
            )
            message = self._append_message_locked(
                role="user",
                content=user_text,
                llm_context_note=material_note,
                metadata={"request_id": request_id},
            )
            self._committed_request_ids.add(request_id)
            return message

    def apply_memory_commit(self, value: object) -> tuple[RuntimeMemoryMessage, ...]:
        """幂等应用通过 generation 门禁的助手历史增量。

        Args:
            value: MemoryCommit 或对应的跨进程字典。

        Returns:
            本次真正新写入的权威消息。

        Raises:
            ValueError: 提交身份或消息协议不完整。
        """

        commit = value if isinstance(value, MemoryCommit) else _memory_commit_from_payload(value)
        with self._lock:
            if commit.commit_id in self._applied_commit_ids:
                return ()
            if not commit.request_id.strip():
                raise ValueError("MemoryCommit 缺少 request_id")

            appended: list[RuntimeMemoryMessage] = []
            referenced_activity_ids = _task_reference_ids(commit.messages)
            for raw_message in commit.messages:
                role = _clean_text(raw_message.get("role"))
                content = raw_message.get("content")
                if role not in {"user", "assistant", "system", "tool"} or not isinstance(content, str):
                    raise ValueError("MemoryCommit 包含非法消息")
                metadata = raw_message.get("metadata")
                normalized_metadata = dict(metadata) if isinstance(metadata, dict) else {}
                if role == "user" and normalized_metadata.get("request_id") == commit.request_id:
                    # 用户消息已经在请求入队时由主进程提交，子进程回流只用于协议排序校验。
                    continue
                origin_key = _clean_text(normalized_metadata.get("origin_key"))
                if origin_key and any(
                    message.metadata.get("origin_key") == origin_key
                    for message in self._messages
                ):
                    continue
                appended.append(
                    self._append_message_locked(
                        role=role,
                        content=content,
                        created_at=_message_created_at(raw_message.get("created_at")),
                        tool_calls=_tool_calls(raw_message.get("tool_calls")),
                        tool_call_id=_optional_text(raw_message.get("tool_call_id")),
                        llm_context_note=_clean_text(raw_message.get("llm_context_note")),
                        metadata=normalized_metadata,
                    )
                )
            if appended:
                last_message_id = appended[-1].message_id
                for activity_id in referenced_activity_ids:
                    record = self._task_records.get(activity_id)
                    if record is not None:
                        record.last_referenced_message_id = last_message_id
            self._applied_commit_ids.add(commit.commit_id)
            return tuple(appended)

    def observe_tool_event(self, payload: dict[str, object]) -> None:
        """记住 Activity 与原始 Foreground 工具调用的关系。

        Args:
            payload: `llm.foreground.tool_call_completed` 的内部载荷。

        Returns:
            无。
        """

        activity_id = _clean_text(payload.get("activity_id"))
        tool_call_id = _clean_text(payload.get("tool_call_id"))
        if not activity_id or not tool_call_id:
            return
        result = payload.get("result")
        normalized_result = dict(result) if isinstance(result, dict) else {}
        with self._lock:
            origin = self._task_origins.setdefault(
                activity_id,
                {
                    "tool_call_ids": [],
                    "based_on_activity_id": None,
                    "based_on_relationship": None,
                    "recovery_of_activity_id": None,
                },
            )
            tool_call_ids = origin["tool_call_ids"]
            if isinstance(tool_call_ids, list) and tool_call_id not in tool_call_ids:
                tool_call_ids.append(tool_call_id)
            based_on_activity_id = _clean_text(
                normalized_result.get("based_on_activity_id")
            )
            if based_on_activity_id:
                origin["based_on_activity_id"] = based_on_activity_id
                parent_record = self._task_records.get(based_on_activity_id)
                if parent_record is not None:
                    # 工具事务稍后才会提交。先用下一条权威编号保护父详情，避免并发压缩
                    # 在新任务已经引用父任务、但历史回流尚未到达时提前释放它。
                    parent_record.last_referenced_message_id = max(
                        parent_record.last_referenced_message_id or 0,
                        self._next_message_id,
                    )
            based_on_relationship = _clean_text(
                normalized_result.get("based_on_relationship")
            )
            if based_on_relationship:
                if based_on_relationship != "activity_correction":
                    raise ValueError("任务父关系类型无效")
                if not based_on_activity_id:
                    raise ValueError("任务父关系缺少父 Activity 编号")
                origin["based_on_relationship"] = based_on_relationship
            recovery_of_activity_id = _clean_text(
                normalized_result.get("recovery_of_activity_id")
            )
            if recovery_of_activity_id:
                existing_recovery_source = _clean_text(
                    origin.get("recovery_of_activity_id")
                )
                if (
                    existing_recovery_source
                    and existing_recovery_source != recovery_of_activity_id
                ):
                    raise ValueError("同一 Activity 不能恢复两项不同任务")
                origin["recovery_of_activity_id"] = recovery_of_activity_id

    def commit_task_recovery(
        self,
        *,
        source_activity_id: str,
        outcome: TaskRecoveryOutcome,
        recovered_by_activity_id: str | None = None,
    ) -> TaskMemoryRecord:
        """原子提交失败任务的恢复结果、双向关系并释放短时压缩 pin。

        Args:
            source_activity_id: 最初失败的 Cognitive Activity 编号。
            outcome: 前台重新判断后经过系统确认的结构化结果。
            recovered_by_activity_id: 已真实开始的新 Activity 编号。

        Returns:
            已经写入恢复关系的来源任务记录副本。

        Raises:
            ValueError: 来源不存在、关系冲突、结果缺少 Activity 或没有持有 pin。
        """

        normalized_source_activity_id = source_activity_id.strip()
        normalized_recovered_by_activity_id = (
            (recovered_by_activity_id or "").strip() or None
        )
        if not normalized_source_activity_id:
            raise ValueError("source_activity_id 不能为空")
        if not isinstance(outcome, TaskRecoveryOutcome):
            raise ValueError("outcome 必须是 TaskRecoveryOutcome")
        if outcome.establishes_activity != (
            normalized_recovered_by_activity_id is not None
        ):
            raise ValueError("恢复结果与 recovered_by_activity_id 不一致")
        if normalized_recovered_by_activity_id == normalized_source_activity_id:
            raise ValueError("任务不能恢复自己")

        with self._lock:
            source_record = self._task_records.get(normalized_source_activity_id)
            if source_record is None:
                raise ValueError("被恢复的 Task Memory 不存在")
            if source_record.source != "cognitive":
                raise ValueError("只有 Cognitive 失败任务可以记录前台恢复")
            if source_record.terminal_state != "failed":
                raise ValueError("只有失败的 Cognitive Task Memory 可以记录恢复")

            existing_source_for_reflection = self._recovery_source_by_reflection.get(
                outcome.self_reflection_id
            )
            if (
                existing_source_for_reflection is not None
                and existing_source_for_reflection != normalized_source_activity_id
            ):
                raise ValueError("同一自我反思编号已经用于其它失败任务")
            if source_record.recovery_outcome is not None:
                if (
                    source_record.recovery_outcome != outcome
                    or source_record.recovered_by_activity_id
                    != normalized_recovered_by_activity_id
                ):
                    raise ValueError("失败任务已经记录其它恢复结果")
                # 第一次提交可能已经成功，只是调用方没有收到返回；重试必须成功，
                # 同时顺手清掉任何重复取得的同名 pin。
                self._recovery_compaction_pins.release(
                    outcome.self_reflection_id
                )
                return deepcopy(source_record)
            if not self._recovery_compaction_pins.contains(
                outcome.self_reflection_id
            ):
                raise ValueError("恢复关系提交前必须持有对应 compaction pin")

            recovered_record = None
            if normalized_recovered_by_activity_id is not None:
                mapped_source = self._recovery_source_by_activity.get(
                    normalized_recovered_by_activity_id
                )
                if (
                    mapped_source is not None
                    and mapped_source != normalized_source_activity_id
                ):
                    raise ValueError("新 Activity 已经恢复其它失败任务")
                recovered_record = self._task_records.get(
                    normalized_recovered_by_activity_id
                )
                if recovered_record is not None:
                    if recovered_record.source != "background_agent":
                        raise ValueError("恢复 Activity 必须来自 Background Work")
                    if (
                        recovered_record.recovery_of_activity_id
                        != normalized_source_activity_id
                    ):
                        raise ValueError("已有 Activity 没有声明当前恢复来源")

            recovery_message = self._append_message_locked(
                role="system",
                content=render_task_recovery(
                    source_activity_id=normalized_source_activity_id,
                    recovered_by_activity_id=normalized_recovered_by_activity_id,
                    outcome=outcome,
                ),
                metadata={
                    "task_recovery": True,
                    "self_reflection_id": outcome.self_reflection_id,
                    "source_activity_id": normalized_source_activity_id,
                    "recovered_by_activity_id": normalized_recovered_by_activity_id,
                },
            )
            source_record.recovery_outcome = outcome
            source_record.recovered_by_activity_id = (
                normalized_recovered_by_activity_id
            )
            self._recovery_source_by_reflection[
                outcome.self_reflection_id
            ] = normalized_source_activity_id
            if normalized_recovered_by_activity_id is not None:
                self._recovery_source_by_activity[
                    normalized_recovered_by_activity_id
                ] = normalized_source_activity_id
                if recovered_record is not None:
                    recovered_record.recovery_of_activity_id = (
                        normalized_source_activity_id
                    )
            self._touch_recovery_relation_locked(
                normalized_source_activity_id,
                recovery_message.message_id,
            )
            self._recovery_compaction_pins.release(outcome.self_reflection_id)
            return deepcopy(source_record)

    def record_task_terminal(
        self,
        event_type: str,
        payload: dict[str, object],
    ) -> TaskMemoryRecord | None:
        """在首个权威 Activity 终态到达时立即写入任务记忆。

        Args:
            event_type: Cognitive 或后台工作的来源事件名。
            payload: 已通过当前运行时世代门禁的终态载荷。

        Returns:
            首次创建的任务记录；非终态或重复终态返回空。
        """

        terminal = _TERMINAL_EVENT_STATES.get(event_type)
        activity_id = _clean_text(payload.get("activity_id"))
        if terminal is None or not activity_id:
            return None
        source, terminal_state = terminal
        if event_type == "cognitive_task.completed":
            # 终态记忆只能接收已经通过 Cognitive 二分支合同的成功结果；否则
            # 非法终态会先污染模型上下文，再被 Activity Registry 拒绝。
            result_text = payload.get("result_text")
            summary = payload.get("summary")
            if (
                not isinstance(result_text, str)
                or not result_text.strip()
                or not isinstance(summary, str)
                or not summary.strip()
            ):
                raise ValueError("Cognitive 成功终态缺少 result 或 summary")
        with self._lock:
            if activity_id in self._task_records:
                return None
            origin = self._task_origins.get(activity_id, {})
            mapped_recovery_source = self._recovery_source_by_activity.get(activity_id)
            declared_recovery_source = (
                _clean_text(payload.get("recovery_of_activity_id"))
                or _clean_text(origin.get("recovery_of_activity_id"))
                or None
            )
            if (
                mapped_recovery_source is not None
                and declared_recovery_source is not None
                and mapped_recovery_source != declared_recovery_source
            ):
                raise ValueError("恢复 Activity 的来源引用不一致")
            recovery_of_activity_id = (
                declared_recovery_source or mapped_recovery_source
            )
            recovery_source_record = None
            if recovery_of_activity_id is not None:
                if source != "background_agent":
                    raise ValueError("只有 Background Work 可以成为恢复 Activity")
                if recovery_of_activity_id == activity_id:
                    raise ValueError("任务不能恢复自己")
                recovery_source_record = self._task_records.get(
                    recovery_of_activity_id
                )
                if recovery_source_record is None:
                    raise ValueError("recovery_of_activity_id 指向不存在的 Task Memory")
                if recovery_source_record.source != "cognitive":
                    raise ValueError("恢复来源必须是 Cognitive Task Memory")
                if recovery_source_record.recovered_by_activity_id not in {
                    None,
                    activity_id,
                }:
                    raise ValueError("恢复来源已经绑定其它 Activity")
            recorded_interactions = self._task_interactions.pop(activity_id, [])
            based_on_activity_id = (
                _clean_text(payload.get("based_on_activity_id"))
                or _clean_text(origin.get("based_on_activity_id"))
                or None
            )
            based_on_relationship = (
                _clean_text(payload.get("based_on_relationship"))
                or _clean_text(origin.get("based_on_relationship"))
                or None
            )
            if based_on_relationship not in {None, "activity_correction"}:
                raise ValueError("任务父关系类型无效")
            if based_on_relationship is not None and based_on_activity_id is None:
                raise ValueError("任务父关系缺少父 Activity 编号")
            record = TaskMemoryRecord(
                activity_id=activity_id,
                origin_tool_call_ids=tuple(
                    item
                    for item in origin.get("tool_call_ids", [])
                    if isinstance(item, str) and item.strip()
                ),
                source=source,  # type: ignore[arg-type]
                goal=_task_goal(payload),
                terminal_state=terminal_state,  # type: ignore[arg-type]
                outcome_brief=_task_outcome_brief(event_type, payload),
                cancellation_reason=(
                    _clean_text(payload.get("cancellation_reason")) or None
                    if terminal_state == "cancelled"
                    else None
                ),
                based_on_activity_id=based_on_activity_id,
                based_on_relationship=based_on_relationship,  # type: ignore[arg-type]
                recovery_of_activity_id=recovery_of_activity_id,
                full_result=_task_full_result(event_type, payload),
                output_paths=tuple(_string_items(payload.get("output_paths"))),
                safe_interactions=tuple(
                    [
                        *recorded_interactions,
                        *_dict_items(payload.get("safe_interactions")),
                    ]
                ),
                input_snapshot=_task_input_snapshot(payload),
            )
            message = self._append_message_locked(
                role="system",
                content=render_task_memory(record),
                metadata={
                    "task_memory": True,
                    "activity_id": activity_id,
                    "recovery_of_activity_id": recovery_of_activity_id,
                },
            )
            record.terminal_message_id = message.message_id
            record.last_referenced_message_id = message.message_id
            self._task_records[activity_id] = record
            if record.based_on_activity_id:
                parent_record = self._task_records.get(record.based_on_activity_id)
                if parent_record is not None:
                    # 子任务的近期短记忆仍明确提到父任务。只有这条关系也进入摘要后，
                    # 父任务才可以真正失效，避免 Prompt 中留下一个无法解释的孤立编号。
                    parent_record.last_referenced_message_id = max(
                        parent_record.last_referenced_message_id or 0,
                        message.message_id,
                    )
            if recovery_of_activity_id is not None:
                self._recovery_source_by_activity[
                    activity_id
                ] = recovery_of_activity_id
                self._touch_recovery_relation_locked(
                    activity_id,
                    message.message_id,
                )
            return record

    def observe_activity_event(
        self,
        event_type: str,
        payload: dict[str, object],
    ) -> None:
        """保存影响后台任务结果的安全权限和资料事实。

        Args:
            event_type: 后台工作来源事件名。
            payload: 已经过主进程世代门禁的事件载荷。

        Returns:
            无。原始命令、绝对路径和 SDK 输入不会进入该账本。
        """

        activity_id = _clean_text(payload.get("activity_id"))
        if not activity_id:
            return
        with self._lock:
            if event_type == "background_agent.interaction_requested":
                request_id = _clean_text(payload.get("request_id"))
                kind = _clean_text(payload.get("kind"))
                if not request_id or kind not in {"permission", "question"}:
                    return
                fact: dict[str, Any] = {
                    "request_id": request_id,
                    "kind": kind,
                    "status": "pending",
                }
                if kind == "permission":
                    fact["action_summary"] = _clean_text(
                        payload.get("action_summary")
                    )
                else:
                    fact["questions"] = _dict_items(payload.get("questions"))
                interactions = self._task_interactions.setdefault(activity_id, [])
                if not any(item.get("request_id") == request_id for item in interactions):
                    interactions.append(fact)
                return
            if event_type != "background_agent.interaction_resolved":
                return
            request_id = _clean_text(payload.get("request_id"))
            if not request_id:
                return
            for fact in self._task_interactions.get(activity_id, []):
                if fact.get("request_id") != request_id:
                    continue
                fact["status"] = "resolved"
                outcome = _clean_text(payload.get("outcome"))
                if outcome:
                    fact["outcome"] = outcome
                break

    def recall_task(
        self,
        activity_id: str,
        *,
        purpose: TaskMemoryLookupPurpose,
        max_characters: int = 500_000,
    ) -> dict[str, object]:
        """按精确 Activity 编号返回当前仍保留的任务详情。

        Args:
            activity_id: 用户追问对应的任务编号。
            purpose: 前台回答只读取安全投影；任务派生才读取原始执行输入。
            max_characters: 当前二轮最多接收的 Unicode 字符数。

        Returns:
            completed 或 not_available 工具结果。
        """

        normalized_activity_id = activity_id.strip()
        if purpose not in {"foreground_recall", "task_derivation"}:
            raise ValueError("purpose 必须是 foreground_recall 或 task_derivation")
        if max_characters <= 0:
            raise ValueError("max_characters 必须大于零")
        with self._lock:
            record = self._task_records.get(normalized_activity_id)
            if record is None:
                return {
                    "state": "not_available",
                    "activity_id": normalized_activity_id,
                    "safe_message": "这项任务的详细记录已经不在本次运行记忆中。",
                }
            original_character_count = len(record.full_result)
            result_text = record.full_result[:max_characters]
            # 召回请求先于工具事务提交到达主进程。预留下一条消息编号可以阻止
            # 已在运行的压缩计划提前淘汰本次正在使用的详情。
            record.last_referenced_message_id = max(
                record.last_referenced_message_id or 0,
                self._next_message_id,
            )
            self._touch_recovery_relation_locked(
                normalized_activity_id,
                self._next_message_id,
            )
            input_snapshot = (
                deepcopy(record.input_snapshot)
                if purpose == "task_derivation"
                else _foreground_input_snapshot(record.input_snapshot)
            )
            return {
                "state": "completed",
                "activity_id": record.activity_id,
                "source": record.source,
                "goal": record.goal,
                "terminal_state": record.terminal_state,
                "cancellation_reason": record.cancellation_reason,
                "cancellation_meaning": (
                    task_cancellation_meaning(record.cancellation_reason)
                    if record.cancellation_reason
                    else None
                ),
                "outcome_brief": record.outcome_brief,
                "based_on_activity_id": record.based_on_activity_id,
                "based_on_relationship": record.based_on_relationship,
                "based_on_meaning": (
                    task_based_on_meaning(record.based_on_relationship)
                    if record.based_on_activity_id
                    else None
                ),
                "recovery_of_activity_id": record.recovery_of_activity_id,
                "recovered_by_activity_id": record.recovered_by_activity_id,
                "recovery_outcome": (
                    record.recovery_outcome.to_payload()
                    if record.recovery_outcome is not None
                    else None
                ),
                "result_text": result_text,
                "output_paths": list(record.output_paths),
                "safe_interactions": [dict(item) for item in record.safe_interactions],
                "input_snapshot": input_snapshot,
                "original_character_count": original_character_count,
                "returned_character_count": len(result_text),
                "truncated": original_character_count > len(result_text),
            }

    def task_record(self, activity_id: str) -> TaskMemoryRecord | None:
        """返回一份任务记录副本，供主进程测试和派生查验。

        Args:
            activity_id: 需要读取的 Activity 编号。

        Returns:
            记录存在时返回深拷贝，否则返回空。
        """

        with self._lock:
            record = self._task_records.get(activity_id.strip())
            return deepcopy(record) if record is not None else None

    def apply_compaction(
        self,
        *,
        memory_epoch: int,
        plan_id: str,
        source_message_id: int,
        owner_runtime_epoch: int,
        summary: str,
    ) -> bool:
        """校验压缩计划后替换摘要并删除已经彻底失效的任务。

        Args:
            memory_epoch: 创建压缩计划时的记忆版本。
            plan_id: 非空压缩计划编号，用于拒绝无身份结果。
            source_message_id: 本次摘要覆盖到的最后消息编号。
            owner_runtime_epoch: 返回结果的助手运行时世代。
            summary: Summary 角色返回的非空摘要。

        Returns:
            结果仍适用于当前前缀并成功写回时返回真。
        """

        normalized_plan_id = plan_id.strip()
        if not normalized_plan_id:
            return False
        with self._lock:
            active_plan = self._active_compaction_plan
            if active_plan is None or active_plan.plan_id != normalized_plan_id:
                return False
            normalized_summary = summary.strip()
            if (
                active_plan.memory_epoch != memory_epoch
                or active_plan.source_message_id != source_message_id
                or active_plan.owner_runtime_epoch != owner_runtime_epoch
                or not normalized_summary
                or self._recovery_compaction_pins.active
            ):
                # 同一计划返回损坏身份或空正文后已经不能继续等待。保留全部旧记忆，
                # 只释放计划槽位，让后续真实事件可以重新规划。
                self._active_compaction_plan = None
                return False
            if not any(message.message_id == source_message_id for message in self._messages):
                self._active_compaction_plan = None
                return False
            retained = [
                message
                for message in self._messages
                if message.message_id > source_message_id
            ]
            self._rolling_summary = normalized_summary
            self._summary_covered_from = active_plan.covered_from
            self._summary_covered_to = active_plan.covered_to
            self._summary_source_message_id = source_message_id
            self._messages = retained
            self._context_token_count = count_text_tokens(normalized_summary) + sum(
                message.token_count for message in retained
            )
            expired_activity_ids = {
                record.activity_id
                for record in self._task_records.values()
                if record.terminal_message_id is not None
                and record.terminal_message_id <= source_message_id
                and (record.last_referenced_message_id or record.terminal_message_id)
                <= source_message_id
                and not self._has_active_child_locked(record.activity_id)
            }
            # 恢复关系是双向事实。只淘汰其中一端会留下无法查询的悬空编号，
            # 因此一端仍需保留时，另一端也必须一起留下。
            for recovered_activity_id, source_activity_id in (
                self._recovery_source_by_activity.items()
            ):
                if (
                    recovered_activity_id not in self._task_records
                    or source_activity_id not in self._task_records
                ):
                    continue
                if (
                    recovered_activity_id in expired_activity_ids
                ) == (source_activity_id in expired_activity_ids):
                    continue
                expired_activity_ids.discard(recovered_activity_id)
                expired_activity_ids.discard(source_activity_id)
            for activity_id in expired_activity_ids:
                record = self._task_records.pop(activity_id, None)
                self._task_origins.pop(activity_id, None)
                self._task_interactions.pop(activity_id, None)
                self._recovery_source_by_activity.pop(activity_id, None)
                if record is None:
                    continue
                if record.recovered_by_activity_id:
                    self._recovery_source_by_activity.pop(
                        record.recovered_by_activity_id,
                        None,
                    )
                if record.recovery_outcome is not None:
                    self._recovery_source_by_reflection.pop(
                        record.recovery_outcome.self_reflection_id,
                        None,
                    )
            self._memory_epoch += 1
            self._active_compaction_plan = None
            return True

    def create_compaction_plan(
        self,
        *,
        owner_runtime_epoch: int,
        max_context_tokens: int = 8000,
        keep_recent_messages: int = 8,
    ) -> RuntimeMemoryCompactionPlan | None:
        """在达到阈值时冻结一笔协议完整的摘要前缀。

        Args:
            owner_runtime_epoch: 将实际执行这笔计划的助手运行时世代。
            max_context_tokens: 摘要和近期消息超过此 Token 数才规划。
            keep_recent_messages: 尾部至少原样保留的消息数量。

        Returns:
            当前唯一可执行计划；无需压缩或已有计划时返回空。
        """

        if owner_runtime_epoch <= 0:
            raise ValueError("owner_runtime_epoch 必须大于零")
        if max_context_tokens <= 0 or keep_recent_messages <= 0:
            raise ValueError("压缩阈值和近期消息数量必须大于零")
        with self._lock:
            if self._active_compaction_plan is not None:
                return None
            if self._recovery_compaction_pins.active:
                return None
            if self._context_token_count <= max_context_tokens:
                return None
            if len(self._messages) <= keep_recent_messages:
                return None

            cut_index = len(self._messages) - keep_recent_messages
            cut_index = _move_cut_before_protocol_transaction(
                self._messages,
                cut_index,
            )
            if cut_index <= 0:
                return None
            messages_to_summarize = tuple(
                deepcopy(message) for message in self._messages[:cut_index]
            )
            source_message_id = messages_to_summarize[-1].message_id
            covered_dates = [message.created_at.date() for message in messages_to_summarize]
            if self._summary_covered_from is not None:
                covered_dates.append(self._summary_covered_from)
            if self._summary_covered_to is not None:
                covered_dates.append(self._summary_covered_to)
            plan = RuntimeMemoryCompactionPlan(
                memory_epoch=self._memory_epoch,
                plan_id=uuid4().hex,
                source_message_id=source_message_id,
                owner_runtime_epoch=owner_runtime_epoch,
                covered_from=min(covered_dates),
                covered_to=max(covered_dates),
                previous_summary=self._rolling_summary,
                messages_to_summarize=messages_to_summarize,
            )
            self._active_compaction_plan = plan
            return plan

    def release_compaction_plan(
        self,
        *,
        plan_id: str,
        owner_runtime_epoch: int | None = None,
    ) -> bool:
        """释放一笔失败或无法提交的压缩计划。

        Args:
            plan_id: 需要严格匹配的计划编号。
            owner_runtime_epoch: 可选的计划所有者门禁。

        Returns:
            当前计划确实被释放时返回真。
        """

        normalized_plan_id = plan_id.strip()
        with self._lock:
            if (
                self._active_compaction_plan is None
                or self._active_compaction_plan.plan_id != normalized_plan_id
                or (
                    owner_runtime_epoch is not None
                    and self._active_compaction_plan.owner_runtime_epoch
                    != owner_runtime_epoch
                )
            ):
                return False
            self._active_compaction_plan = None
            return True

    def release_compaction_plan_for_runtime(self, runtime_epoch: int) -> bool:
        """释放已经随指定助手运行时一起失效的压缩计划。

        Args:
            runtime_epoch: 已经退出的助手运行时世代。

        Returns:
            当前计划确实属于该世代并被释放时返回真。
        """

        with self._lock:
            if (
                self._active_compaction_plan is None
                or self._active_compaction_plan.owner_runtime_epoch != runtime_epoch
            ):
                return False
            self._active_compaction_plan = None
            return True

    def _append_message_locked(
        self,
        *,
        role: str,
        content: str,
        tool_calls: tuple[dict[str, Any], ...] = (),
        tool_call_id: str | None = None,
        llm_context_note: str = "",
        metadata: dict[str, Any] | None = None,
        created_at: datetime | None = None,
    ) -> RuntimeMemoryMessage:
        """在持锁状态下分配编号并追加一条消息。

        Args:
            role: Chat-like 消息角色。
            content: 消息正文。
            tool_calls: 可选工具调用。
            tool_call_id: 可选工具响应编号。
            llm_context_note: 仅给模型读取的安全附注。
            metadata: 运行期归因字段。
            created_at: 消息真实发生时间；主进程自建事实不传时使用当前本地时间。

        Returns:
            已分配权威编号的消息。
        """

        message = self._build_message_locked(
            role=role,
            content=content,
            tool_calls=tool_calls,
            tool_call_id=tool_call_id,
            llm_context_note=llm_context_note,
            metadata=metadata,
            created_at=created_at,
            message_id=self._next_message_id,
        )
        self._messages.append(message)
        self._next_message_id += 1
        self._context_token_count += message.token_count
        self._memory_epoch += 1
        return message

    def _build_message_locked(
        self,
        *,
        role: str,
        content: str,
        tool_calls: tuple[dict[str, Any], ...] = (),
        tool_call_id: str | None = None,
        llm_context_note: str = "",
        metadata: dict[str, Any] | None = None,
        created_at: datetime | None = None,
        message_id: int,
    ) -> RuntimeMemoryMessage:
        """只构造消息对象，不改动编号、列表或记忆版本。

        Args:
            role: Chat-like 消息角色。
            content: 消息正文。
            tool_calls: 可选工具调用。
            tool_call_id: 可选工具响应编号。
            llm_context_note: 仅给模型读取的安全附注。
            metadata: 运行期归因字段。
            created_at: 消息真实发生时间；未传时使用当前本地时间。
            message_id: 调用方预分配的消息编号。

        Returns:
            尚未写入 Store 的不可变消息对象。
        """

        token_count = count_text_tokens(
            protocol_message_token_text(
                role=role,
                content=content,
                tool_calls=tool_calls,
                tool_call_id=tool_call_id,
                llm_context_note=llm_context_note,
            )
        )
        return RuntimeMemoryMessage(
            message_id=message_id,
            role=role,
            content=content,
            token_count=token_count,
            created_at=created_at or local_wall_datetime(),
            tool_calls=tuple(deepcopy(tool_calls)),
            tool_call_id=tool_call_id,
            llm_context_note=llm_context_note,
            metadata=deepcopy(metadata or {}),
        )

    def _touch_recovery_relation_locked(
        self,
        activity_id: str,
        message_id: int,
    ) -> None:
        """推进恢复关系两端的最近引用边界。

        Args:
            activity_id: 本次直接写入或召回的任务编号。
            message_id: 能证明这次引用存在的权威消息边界。

        Returns:
            无。关系另一端尚未形成 Task Memory 时只更新现存一端。
        """

        related_activity_ids = {activity_id}
        record = self._task_records.get(activity_id)
        if record is not None:
            if record.recovery_of_activity_id:
                related_activity_ids.add(record.recovery_of_activity_id)
            if record.recovered_by_activity_id:
                related_activity_ids.add(record.recovered_by_activity_id)
        mapped_source = self._recovery_source_by_activity.get(activity_id)
        if mapped_source:
            related_activity_ids.add(mapped_source)
        for recovered_activity_id, source_activity_id in (
            self._recovery_source_by_activity.items()
        ):
            if source_activity_id == activity_id:
                related_activity_ids.add(recovered_activity_id)
        for related_activity_id in related_activity_ids:
            related_record = self._task_records.get(related_activity_id)
            if related_record is None:
                continue
            related_record.last_referenced_message_id = max(
                related_record.last_referenced_message_id or 0,
                message_id,
            )

    def _has_active_child_locked(self, activity_id: str) -> bool:
        """判断是否仍有未终态子任务依赖当前任务。

        Args:
            activity_id: 当前父任务编号。

        Returns:
            已登记但尚未形成终态记录的子任务存在时返回真。
        """

        for child_id, origin in self._task_origins.items():
            if origin.get("based_on_activity_id") != activity_id:
                if origin.get("recovery_of_activity_id") != activity_id:
                    continue
            if child_id not in self._task_records:
                return True
        if any(
            source_activity_id == activity_id
            and recovered_activity_id not in self._task_records
            for recovered_activity_id, source_activity_id in (
                self._recovery_source_by_activity.items()
            )
        ):
            return True
        return False


def _memory_commit_from_payload(value: object) -> MemoryCommit:
    """从跨进程字典还原 MemoryCommit。

    Args:
        value: 尚未信任的内部事件字段。

    Returns:
        完成基础类型校验的提交对象。
    """

    if not isinstance(value, dict):
        raise ValueError("MemoryCommit 必须是对象")
    commit_id = _clean_text(value.get("commit_id"))
    request_id = _clean_text(value.get("request_id"))
    base_memory_epoch = value.get("base_memory_epoch")
    messages = value.get("messages")
    if not commit_id or not request_id or not isinstance(base_memory_epoch, int):
        raise ValueError("MemoryCommit 身份不完整")
    if not isinstance(messages, list) or not all(isinstance(item, dict) for item in messages):
        raise ValueError("MemoryCommit messages 必须是对象列表")
    return MemoryCommit(
        commit_id=commit_id,
        request_id=request_id,
        base_memory_epoch=base_memory_epoch,
        messages=tuple(dict(item) for item in messages),
    )


def _message_created_at(value: object) -> datetime:
    """解析子进程提交的消息发生时间。

    Args:
        value: ISO 时间文本或已反序列化的 datetime。

    Returns:
        带时区的本地时间；字段损坏时使用当前本地时间。
    """

    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip())
        except ValueError:
            return local_wall_datetime()
        return parsed if parsed.tzinfo is not None else parsed.astimezone()
    return local_wall_datetime()


def _foreground_input_snapshot(value: dict[str, Any]) -> dict[str, Any]:
    """生成可进入 Foreground 工具结果的输入事实。

    图片 data URL 只用于当前请求执行。把它作为普通文本交给 Foreground 既没有
    视觉意义，也会绕开完整结果的字符护栏，因此这里只保留数量事实。

    Args:
        value: 任务详情中保存的原始输入快照。

    Returns:
        不含图片编码、其余字段保持原样的独立字典。
    """

    snapshot = deepcopy(value)
    # 兼容两种历史形态：元素可能是裸 data_url 字符串，也可能是带标签的证据字典；
    # 无论哪种形态，像素都不进入 Foreground，只保留数量事实。
    encoded_images = snapshot.pop("encoded_images", None)
    if isinstance(encoded_images, (list, tuple)):
        snapshot["encoded_image_count"] = len([
            item for item in encoded_images
            if (isinstance(item, str) and item)
            or (isinstance(item, dict) and item.get("data_url"))
        ])
    return snapshot


def _task_reference_ids(messages: tuple[dict[str, Any], ...]) -> set[str]:
    """从完整工具事务中识别被召回或被派生任务引用的父任务。

    Args:
        messages: 子进程准备提交的协议完整消息。

    Returns:
        本次事务明确引用的 Activity 编号集合。
    """

    recall_call_ids: set[str] = set()
    for message in messages:
        tool_calls = message.get("tool_calls")
        if not isinstance(tool_calls, list):
            continue
        for tool_call in tool_calls:
            if not isinstance(tool_call, dict):
                continue
            function_payload = tool_call.get("function")
            if not isinstance(function_payload, dict):
                continue
            if function_payload.get("name") != "recall_task_memory":
                continue
            tool_call_id = _clean_text(tool_call.get("id"))
            if tool_call_id:
                recall_call_ids.add(tool_call_id)

    references: set[str] = set()
    for message in messages:
        if message.get("role") != "tool":
            continue
        tool_call_id = _clean_text(message.get("tool_call_id"))
        content = message.get("content")
        if not isinstance(content, str):
            continue
        try:
            result = json.loads(content)
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(result, dict):
            continue
        based_on_activity_id = _clean_text(result.get("based_on_activity_id"))
        if based_on_activity_id:
            references.add(based_on_activity_id)
        if tool_call_id in recall_call_ids:
            activity_id = _clean_text(result.get("activity_id"))
            if activity_id and result.get("detail_loaded") is True:
                references.add(activity_id)
    return references


def _task_goal(payload: dict[str, object]) -> str:
    """从来源终态提取任务实际目标。"""

    return (
        _clean_text(payload.get("goal"))
        or _clean_text(payload.get("brief"))
        or _clean_text(payload.get("source_user_text"))
        or "未提供任务目标"
    )


def _task_outcome_brief(event_type: str, payload: dict[str, object]) -> str:
    """从来源终态提取可以放进短记忆的结果概况。"""

    for field_name in (
        "summary",
        "delivery_brief",
        "status_context",
        "error_summary",
        "failure_reason",
    ):
        value = _clean_text(payload.get(field_name))
        if value:
            return value
    failure = payload.get("failure")
    if isinstance(failure, dict):
        summary = _clean_text(failure.get("summary"))
        if summary:
            return summary
    if event_type.endswith(".cancelled"):
        return "任务已取消，没有完成。"
    if event_type.endswith(".failed"):
        return "任务执行失败，没有完成。"
    return "任务已经完成。"


def _task_full_result(event_type: str, payload: dict[str, object]) -> str:
    """从来源终态提取可供精确召回的完整事实。"""

    for field_name in ("result_text", "display_content"):
        value = payload.get(field_name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return _task_outcome_brief(event_type, payload)


def _task_input_snapshot(payload: dict[str, object]) -> dict[str, Any]:
    """保存终态输入事实，并在记忆入口移除图片像素。"""

    snapshot = payload.get("task_input_snapshot")
    return _foreground_input_snapshot(snapshot) if isinstance(snapshot, dict) else {}


def _tool_calls(value: object) -> tuple[dict[str, Any], ...]:
    """清洗 MemoryCommit 里的工具调用列表。"""

    if not isinstance(value, list):
        return ()
    return tuple(project_tool_calls_for_runtime_memory(value))


def _string_items(value: object) -> list[str]:
    """返回去空白的字符串列表。"""

    if not isinstance(value, (list, tuple)):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _dict_items(value: object) -> list[dict[str, Any]]:
    """返回字典列表的独立副本。"""

    if not isinstance(value, (list, tuple)):
        return []
    return [deepcopy(item) for item in value if isinstance(item, dict)]


def _optional_text(value: object) -> str | None:
    """把可选文本裁剪为空或非空字符串。"""

    normalized = _clean_text(value)
    return normalized or None


def _clean_text(value: object) -> str:
    """把未知值安全裁剪成字符串。"""

    return value.strip() if isinstance(value, str) else ""


def _move_cut_before_protocol_transaction(
    messages: list[RuntimeMemoryMessage],
    cut_index: int,
) -> int:
    """把落在工具事务内部的压缩切点前移到事务开头。

    Args:
        messages: 当前主进程近期消息。
        cut_index: 固定保留数量得到的原始切点。

    Returns:
        不会拆开同一工具事务的安全切点。
    """

    if cut_index <= 0 or cut_index >= len(messages):
        return cut_index
    transaction_id = messages[cut_index].metadata.get("protocol_transaction_id")
    if not isinstance(transaction_id, str) or not transaction_id:
        return cut_index
    while cut_index > 0:
        previous_transaction_id = messages[cut_index - 1].metadata.get(
            "protocol_transaction_id"
        )
        if previous_transaction_id != transaction_id:
            break
        cut_index -= 1
    return cut_index
