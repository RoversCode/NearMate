"""把成功广播的助手正文归档到正确的主进程账本。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from threading import RLock

from companion.activities.progress_ledger import ActivityProgressLedger
from companion.observability.logging_config import get_backend_logger
from companion.prompt.xml_markup import render_xml_cdata_leaf
from companion.realtime.assistant_events import (
    AcknowledgeActivityProgressCommand,
    RuntimeEvent,
)
from companion.realtime.visible_output_journal import (
    VisibleOutputAdmission,
    VisibleOutputJournal,
    VisibleOutputSettlement,
)
from companion.runtime_memory.models import MemoryCommit
from companion.runtime_memory.store import RuntimeMemoryStore


logger = get_backend_logger(__name__)


@dataclass(slots=True)
class _ProgressArchiveState:
    """记录一条 Progress settlement 已经完成的归档阶段。

    Args:
        runtime_memory_committed: 主动播报是否已经写入运行期记忆。
        progress_ledger_recorded: 纯文本是否已经写入 Progress 临时账本。
    """

    runtime_memory_committed: bool = False
    progress_ledger_recorded: bool = False


class AssistantOutputSettlementService:
    """统一处理稳定对话与 Activity Progress 的可见输出结算。

    Args:
        runtime_memory_store: Backend 进程级稳定运行期记忆。
        activity_progress_ledger: Backend 进程级 Progress 临时账本。
        visible_output_journal: 记录成功广播正文的短期账本。
        progress_acknowledgement_handler: 把已登记 Progress 回执交给助手子进程的端口。
        output_settled_handler: 所有归档完成后释放下一轮准入屏障的端口。
    """

    def __init__(
        self,
        *,
        runtime_memory_store: RuntimeMemoryStore | None = None,
        activity_progress_ledger: ActivityProgressLedger | None = None,
        visible_output_journal: VisibleOutputJournal | None = None,
        progress_acknowledgement_handler: Callable[
            [AcknowledgeActivityProgressCommand], object
        ]
        | None = None,
        output_settled_handler: Callable[[VisibleOutputSettlement], object] | None = None,
    ) -> None:
        """初始化输出结算所需的唯一状态所有者和外部端口。

        Args:
            runtime_memory_store: Backend 进程级稳定运行期记忆。
            activity_progress_ledger: Backend 进程级 Progress 临时账本。
            visible_output_journal: 记录成功广播正文的短期账本。
            progress_acknowledgement_handler: 把 Progress 回执交给子进程的端口。
            output_settled_handler: 完成归档后释放下一轮准入屏障的端口。
        """

        self._runtime_memory_store = runtime_memory_store
        self._activity_progress_ledger = (
            activity_progress_ledger or ActivityProgressLedger()
        )
        self._visible_output_journal = (
            visible_output_journal or VisibleOutputJournal()
        )
        self._progress_acknowledgement_handler = (
            progress_acknowledgement_handler
        )
        self._output_settled_handler = output_settled_handler
        # 这里只保留 journal 事务尚未提交期间的归档阶段。Turn 放行失败后同一 settlement
        # 会重试，但已经写入的 Progress 不能因此重复追加。
        self._archived_request_ids: set[str] = set()
        # 来源回执可能在记忆和临时账本都写完后暂时入队失败。这里保存两个已完成
        # 阶段，让同一 settlement 重试时只补投回执，不重复追加用户已经看到的正文。
        self._progress_archive_states: dict[str, _ProgressArchiveState] = {}
        self._settlement_lock = RLock()

    def prepare_broadcast_text(
        self,
        event: RuntimeEvent,
    ) -> VisibleOutputAdmission | None:
        """在公共广播前执行 Journal 身份和墓碑门禁。"""

        return self._visible_output_journal.prepare_public_text_event(
            event.type,
            event.payload,
        )

    def commit_broadcast_text(
        self,
        event: RuntimeEvent,
        admission: VisibleOutputAdmission,
    ) -> bool:
        """广播成功后提交正文，并应用可选稳定记忆事务。"""

        recorded = self._visible_output_journal.commit_public_text_event(admission)
        if not recorded:
            return False

        memory_commit = event.payload.get("memory_commit")
        if memory_commit is None:
            return True
        history_policy = event.payload.get("history_policy")
        if history_policy == "activity_progress":
            raise ValueError("Activity Progress 不能携带稳定 MemoryCommit")
        if self._runtime_memory_store is None:
            return True

        request_id = _clean_text(event.payload.get("request_id"))
        commit_request_id = (
            _clean_text(memory_commit.get("request_id"))
            if isinstance(memory_commit, dict)
            else ""
        )
        if not request_id or commit_request_id != request_id:
            raise ValueError("助手文本与 MemoryCommit 的 request_id 不一致")

        try:
            self._runtime_memory_store.apply_memory_commit(memory_commit)
        except Exception:
            # 公开正文已经进入 Journal，主进程记忆暂时不可写时不能阻断 final
            # 事件后面的材料释放和结果确认；后续 assistant.output_settled 会重试完整提交。
            logger.exception(
                "稳定 MemoryCommit 暂时写入失败，等待 output settlement 重试 request_id=%s",
                request_id,
            )
            return True
        self._visible_output_journal.mark_stable_history_committed(request_id)
        return True

    def validate_settlement(self, event: RuntimeEvent) -> None:
        """在排入提交 lane 前只读校验结算身份。"""

        self._visible_output_journal.validate_settlement(event.payload)

    def abort_broadcast_text(self, admission: VisibleOutputAdmission) -> None:
        """公共广播失败时撤销 Journal admission。"""

        self._visible_output_journal.abort_public_text_event(admission)

    def settle(self, event: RuntimeEvent) -> VisibleOutputSettlement | None:
        """结算一次已停止产生正文的助手请求。

        Args:
            event: `assistant.output_settled` 运行时事件。

        Returns:
            首次结算产生的不可变结果；重复结算返回空。
        """

        settlement = self._visible_output_journal.settle(
            event.payload,
            settled_at=event.produced_at,
            commit_settlement=self._archive_and_release,
        )
        if settlement is not None:
            with self._settlement_lock:
                self._archived_request_ids.discard(settlement.request_id)
                self._progress_archive_states.pop(settlement.request_id, None)
        return settlement

    def _archive_and_release(
        self,
        settlement: VisibleOutputSettlement,
    ) -> None:
        """归档可见正文，并在同一 journal 事务末尾释放 Turn。

        Args:
            settlement: journal 根据已公开正文构造的待提交结算。
        """

        with self._settlement_lock:
            already_archived = settlement.request_id in self._archived_request_ids

        if not already_archived:
            if settlement.history_policy == "stable_dialogue":
                self._commit_uncommitted_stable_text(settlement)
            elif settlement.history_policy == "activity_progress":
                self._record_progress_text(settlement)
            elif settlement.history_policy != "unattended_episode":
                raise ValueError("未知助手历史策略")
            with self._settlement_lock:
                self._archived_request_ids.add(settlement.request_id)

        # Turn 屏障只能观察已经完成的稳定记忆或 Progress 账本事实。回调异常也必须
        # 让整项 settlement 保持可重试；重试会复用上面的归档阶段，不重复追加正文。
        if self._output_settled_handler is not None:
            self._output_settled_handler(settlement)

    def _commit_uncommitted_stable_text(
        self,
        settlement: VisibleOutputSettlement,
    ) -> None:
        """把 final 前已经公开但尚未提交的正文补入稳定运行期记忆。

        Args:
            settlement: 可见输出账本生成的首次结算结果。
        """

        if self._runtime_memory_store is None:
            return
        if settlement.memory_commit is not None:
            # 文本事件在广播后提交失败时，Journal 保留完整工具协议。这里先应用该
            # 幂等提交，再处理它之后才公开的正文尾部，避免 settlement 只剩一条文本。
            self._runtime_memory_store.apply_memory_commit(settlement.memory_commit)
        uncommitted_text = (
            settlement.uncommitted_visible_text
            if settlement.uncommitted_visible_text is not None
            else settlement.visible_text
        )
        if not uncommitted_text:
            return
        memory_epoch = self._runtime_memory_store.snapshot().memory_epoch
        self._runtime_memory_store.apply_memory_commit(
            MemoryCommit(
                commit_id=f"{settlement.settlement_id}:visible-tail",
                request_id=settlement.request_id,
                base_memory_epoch=memory_epoch,
                messages=(
                    {
                        "role": "assistant",
                        "content": uncommitted_text,
                        "metadata": {
                            "request_id": settlement.request_id,
                            "output_outcome": settlement.outcome,
                            "derived_from_activity_ids": list(
                                settlement.derived_from_activity_ids
                            ),
                        },
                    },
                ),
            )
        )

    def _record_progress_text(
        self,
        settlement: VisibleOutputSettlement,
    ) -> None:
        """把 Progress 正文写入记忆和临时账本，再确认来源冻结序号。

        Args:
            settlement: 可见输出账本生成的首次结算结果。

        Raises:
            RuntimeError: 来源回执没有进入助手控制队列时抛出。
        """

        if not settlement.visible_text or not settlement.progress_references:
            return
        with self._settlement_lock:
            archive_state = self._progress_archive_states.setdefault(
                settlement.request_id,
                _ProgressArchiveState(),
            )

        if not archive_state.runtime_memory_committed:
            self._commit_progress_broadcast_to_memory(settlement)
            with self._settlement_lock:
                archive_state.runtime_memory_committed = True

        if not archive_state.progress_ledger_recorded:
            self._activity_progress_ledger.record_visible_progress(
                visible_text=settlement.visible_text,
                references=settlement.progress_references,
            )
            with self._settlement_lock:
                archive_state.progress_ledger_recorded = True

        if self._progress_acknowledgement_handler is None:
            return
        acknowledgement_accepted = self._progress_acknowledgement_handler(
            AcknowledgeActivityProgressCommand(
                request_id=settlement.request_id,
                references=settlement.progress_references,
                issued_at=settlement.settled_at,
            )
        )
        if acknowledgement_accepted is False:
            raise RuntimeError("activity_progress_acknowledgement_rejected")

    def _commit_progress_broadcast_to_memory(
        self,
        settlement: VisibleOutputSettlement,
    ) -> None:
        """把实际公开的主动播报写入 Foreground 运行期记忆。

        播报正文自本变更起进入运行期记忆，作为模型的自我感知输入。原始 Progress
        临时账本仍保存纯文本；运行期历史使用内部 XML 标签，明确这是角色已经说出口的播报。

        Args:
            settlement: 已经完成公共广播的 Progress 结算事实。
        """

        if self._runtime_memory_store is None:
            return
        memory_epoch = self._runtime_memory_store.snapshot().memory_epoch
        self._runtime_memory_store.apply_memory_commit(
            MemoryCommit(
                commit_id=f"{settlement.settlement_id}:proactive-broadcast",
                request_id=settlement.request_id,
                base_memory_epoch=memory_epoch,
                messages=(
                    {
                        "role": "assistant",
                        "content": render_xml_cdata_leaf(
                            "proactive_broadcast",
                            settlement.visible_text,
                        ),
                        "metadata": {
                            "request_id": settlement.request_id,
                            "proactive_broadcast": True,
                            "output_outcome": settlement.outcome,
                            "derived_from_activity_ids": list(
                                settlement.derived_from_activity_ids
                            ),
                        },
                    },
                ),
            )
        )


def _clean_text(value: object) -> str:
    """把跨进程身份字段清洗为无首尾空白的字符串。

    Args:
        value: RuntimeEvent 中的任意字段值。

    Returns:
        合法字符串的清洗结果；其它类型返回空串。
    """

    return value.strip() if isinstance(value, str) else ""
