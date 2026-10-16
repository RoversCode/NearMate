"""统一管理 Self-Reflection episode 的跨组件事务边界。"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime
from threading import RLock

from companion.core.local_clock import local_wall_datetime
from companion.foreground_self_reflection.models import (
    DivertFinalInputCommand,
    InputDiversionResult,
    SelfReflectionAdmissionCommand,
    SelfReflectionAdmissionResult,
    SelfReflectionSnapshot,
    SelfReflectionTransitionResult,
)
from companion.foreground_self_reflection.registry import (
    ForegroundAttentionCoordinator,
)
from companion.observability.logging_config import get_backend_logger
from companion.realtime.assistant_events import RuntimeEvent
from companion.runtime_memory.store import RuntimeMemoryStore
from companion.runtime_memory.unattended_episode import (
    UnattendedEpisodeIdentity,
    UnattendedInputEpisode,
    UnattendedInputItem,
    UnattendedMaterialReference as MemoryMaterialReference,
    serialize_unattended_episode_history,
)


logger = get_backend_logger(__name__)


class SelfReflectionEpisodeLifecycle:
    """让 Attention、Runtime Memory 和 Episode 公共事件共享一个线性化点。

    这个对象只管理机械生命周期，不判断 Prompt、工具、材料租约或表达语义。调用方
    可以在 ``transaction`` 中串联其它准入副作用，但任何 Episode open、append、
    close 和 Runtime epoch 替换都必须回到这里执行。

    Args:
        attention: 当前 Backend 进程唯一的 Self-Reflection Attention 状态机。
        runtime_memory: 保存 Episode 与稳定聊天历史的主进程运行期记忆。
        publish_episode_committed: 把完整 Episode 历史块放入主进程事件队列的端口。
        now: 生成输入接纳和 Episode 关闭时间的可替换本地时钟。
    """

    def __init__(
        self,
        *,
        attention: ForegroundAttentionCoordinator,
        runtime_memory: RuntimeMemoryStore,
        publish_episode_committed: Callable[[RuntimeEvent], None] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        """保存唯一状态所有者和公共事件端口。

        Args:
            attention: Self-Reflection Attention 状态机。
            runtime_memory: 主进程运行期记忆。
            publish_episode_committed: Episode 闭合事件发布端口。
            now: 返回当前本地时间的函数。
        """

        self._attention = attention
        self._runtime_memory = runtime_memory
        self._publish_episode_committed = publish_episode_committed
        self._now = now or local_wall_datetime
        self._lock = RLock()
        self._published_episode_keys: set[tuple[int, str]] = set()
        self._queued_publication_keys: set[tuple[int, str]] = set()
        self._publication_outbox: dict[
            tuple[int, str],
            UnattendedInputEpisode,
        ] = {}

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """把调用方的复合准入或清理步骤纳入同一 Episode 事务。

        Yields:
            None: 调用方可以安全执行不会跨线程等待的同步生命周期步骤。
        """

        with self._lock:
            yield

    def admit_and_open(
        self,
        command: SelfReflectionAdmissionCommand,
    ) -> SelfReflectionAdmissionResult:
        """原子取得 Attention，并在它对输入可见前打开对应 Episode。

        Args:
            command: 已通过 Cognitive 恢复资格判断的准入命令。

        Returns:
            准入成功、幂等重复或失败的结构化结果。Episode 打开失败时，本方法会在
            释放事务锁前终止刚创建的 Attention。
        """

        with self._lock:
            admission = self._attention.admit(command)
            if admission.status != "admitted" or admission.snapshot is None:
                return admission
            snapshot = admission.snapshot
            try:
                opened = self._runtime_memory.open_unattended_input_episode(
                    _episode_identity(snapshot),
                    opened_at=self._now(),
                )
            except Exception:
                logger.exception(
                    "Self-Reflection Episode 打开失败 self_reflection_id=%s",
                    snapshot.self_reflection_id,
                )
                self._attention.abort_admission(
                    snapshot.self_reflection_id,
                    runtime_epoch=snapshot.runtime_epoch,
                    reason="recovery_episode_open_failed",
                )
                return SelfReflectionAdmissionResult(
                    status="rejected",
                    reason="recovery_episode_open_failed",
                )
            if opened:
                return admission
            self._attention.abort_admission(
                snapshot.self_reflection_id,
                runtime_epoch=snapshot.runtime_epoch,
                reason="recovery_episode_identity_conflict",
            )
            return SelfReflectionAdmissionResult(
                status="rejected",
                reason="recovery_episode_identity_conflict",
            )

    def resolve_input_ownership(
        self,
        command: DivertFinalInputCommand,
        *,
        reserve_ordinary_request: bool,
    ) -> InputDiversionResult:
        """原子裁决输入归属，并在返回前完成 Runtime Memory 追加。

        Args:
            command: 带稳定输入身份和安全材料引用的最终输入。
            reserve_ordinary_request: 没有活跃反思时是否同时预留普通请求资格。Reservation
                阶段必须传假，正式 dispatch 和 Final ASR 提交阶段传真。

        Returns:
            已经完整持久化的 Episode 归属，或可以继续普通请求的裁决。
        """

        with self._lock:
            if not reserve_ordinary_request and not self._attention.is_attention_active(
                runtime_epoch=command.runtime_epoch
            ):
                return InputDiversionResult(
                    status="not_diverted",
                    reason="self_reflection_not_active",
                    runtime_epoch=command.runtime_epoch,
                )
            result = self._attention.divert_final_input(command)
            result = replace(result, runtime_epoch=command.runtime_epoch)
            if result.status not in {"accepted", "duplicate"}:
                return result
            if result.accepted_input is None:
                return InputDiversionResult(
                    status="not_diverted",
                    reason="accepted_input_missing",
                    snapshot=result.snapshot,
                    episode_id=result.episode_id,
                    history_disposition=result.history_disposition,
                    runtime_epoch=command.runtime_epoch,
                )
            if result.status == "duplicate":
                return result
            snapshot = result.snapshot
            if snapshot is None:
                return InputDiversionResult(
                    status="not_diverted",
                    reason="self_reflection_snapshot_missing",
                    episode_id=result.episode_id,
                    history_disposition=result.history_disposition,
                    runtime_epoch=command.runtime_epoch,
                )
            item = result.accepted_input
            memory_item = UnattendedInputItem(
                input_identity=item.input_identity,
                source=item.source,
                text=item.text,
                episode_sequence=item.episode_sequence,
                accepted_at=self._now(),
                materials=tuple(
                    MemoryMaterialReference(
                        file_name=reference.file_name,
                        media_type=reference.media_type,
                    )
                    for reference in item.materials
                ),
            )
            try:
                append_status = self._runtime_memory.append_unattended_input(
                    _episode_identity(snapshot),
                    memory_item,
                )
            except Exception:
                logger.exception(
                    "Self-Reflection Episode 输入追加失败 input_identity=%s",
                    item.input_identity,
                )
                append_status = "append_failed"
            if append_status in {"appended", "duplicate"}:
                return result
            self._attention.rollback_diverted_input(
                snapshot.self_reflection_id,
                runtime_epoch=snapshot.runtime_epoch,
                input_identity=item.input_identity,
            )
            return InputDiversionResult(
                status="not_diverted",
                reason=f"episode_memory_{append_status}",
                snapshot=self._attention.snapshot(snapshot.self_reflection_id),
                episode_id=snapshot.episode_id,
                history_disposition="unattended_episode",
                runtime_epoch=command.runtime_epoch,
            )

    def close_completed_episode(
        self,
        snapshot: SelfReflectionSnapshot,
    ) -> SelfReflectionTransitionResult:
        """冻结正常 Recovery 的最终输入并释放 Attention。

        Args:
            snapshot: 已经进入 ``FINALIZING`` 的权威 Attention 快照。

        Returns:
            Attention 完成关闭、幂等重复或拒绝的结构化结果。
        """

        with self._lock:
            # 外层事务锁阻止新输入进入，所以先完成可能失败的 Memory close；失败时
            # Attention 仍留在 FINALIZING，下一次重试还能继续接纳期间输入。
            episode = self._runtime_memory.close_unattended_input_episode(
                _episode_identity(snapshot)
            )
            frozen = self._attention.freeze_inputs_for_close(
                snapshot.self_reflection_id,
                runtime_epoch=snapshot.runtime_epoch,
            )
            if frozen.status not in {"applied", "duplicate"}:
                return frozen
            self._enqueue_publication(episode)
            return self._attention.complete_close(
                snapshot.self_reflection_id,
                runtime_epoch=snapshot.runtime_epoch,
            )

    def abort_admission(
        self,
        snapshot: SelfReflectionSnapshot,
        *,
        reason: str,
    ) -> bool:
        """关闭已打开但没有进入正常 Recovery 的 Episode，并终止 Attention。

        Args:
            snapshot: 本次失败 Admission 的完整身份。
            reason: 写入 Attention 墓碑的稳定失败原因。

        Returns:
            Episode 已关闭并且 Attention 已终止时返回真。
        """

        with self._lock:
            episode = self._runtime_memory.close_unattended_input_episode(
                _episode_identity(snapshot)
            )
            self._enqueue_publication(episode)
            transition = self._attention.abort_admission(
                snapshot.self_reflection_id,
                runtime_epoch=snapshot.runtime_epoch,
                reason=reason,
            )
            return transition.status in {"applied", "duplicate"}

    def begin_runtime_termination(
        self,
        *,
        runtime_epoch: int,
        new_runtime_epoch: int,
    ) -> tuple[SelfReflectionTransitionResult, ...]:
        """原子停止旧 Episode 接纳输入并切换 Attention 世代。

        Args:
            runtime_epoch: 即将退休的旧 Assistant Runtime 世代。
            new_runtime_epoch: 已经发布的新世代。

        Returns:
            旧世代被终止的 Self-Reflection 快照。
        """

        with self._lock:
            if self._attention.runtime_epoch != runtime_epoch:
                return ()
            return self._attention.replace_runtime_epoch(new_runtime_epoch)

    def close_terminated_episode(self, snapshot: SelfReflectionSnapshot) -> bool:
        """在旧世代所有辅助 settlement 完成后关闭并发布 Episode。

        Args:
            snapshot: ``begin_runtime_termination`` 冻结的旧世代身份。

        Returns:
            Episode 已进入稳定历史和公共事件 outbox 时返回真。
        """

        with self._lock:
            episode = self._runtime_memory.close_unattended_input_episode(
                _episode_identity(snapshot)
            )
            self._enqueue_publication(episode)
            return True

    def retry_publications(self) -> int:
        """重试已经稳定提交、但尚未进入公共事件队列的 Episode。

        Returns:
            本轮首次成功入队的事件数量。
        """

        with self._lock:
            pending = tuple(self._publication_outbox.values())
        completed = 0
        for episode in pending:
            with self._lock:
                if self._publish_once(episode):
                    completed += 1
        return completed

    def publication_pending(self, *, runtime_epoch: int, episode_id: str) -> bool:
        """判断指定 Episode 是否仍在等待公共事件入队。

        Args:
            runtime_epoch: Episode 所属旧或当前运行时世代。
            episode_id: Episode 稳定编号。

        Returns:
            Outbox 中仍存在该事件时返回真。
        """

        with self._lock:
            return (runtime_epoch, episode_id.strip()) in self._publication_outbox

    def acknowledge_publication(
        self,
        *,
        runtime_epoch: int,
        episode_id: str,
    ) -> bool:
        """确认 Interaction 广播成功，并清除对应 outbox 凭据。

        Args:
            runtime_epoch: Episode 所属 Assistant Runtime 世代。
            episode_id: Episode 稳定编号。

        Returns:
            首次确认或重复确认已有墓碑时返回真。
        """

        key = (runtime_epoch, episode_id.strip())
        with self._lock:
            if key in self._published_episode_keys:
                return True
            if key not in self._publication_outbox:
                return False
            self._queued_publication_keys.discard(key)
            self._publication_outbox.pop(key, None)
            self._published_episode_keys.add(key)
            return True

    def fail_publication(
        self,
        *,
        runtime_epoch: int,
        episode_id: str,
    ) -> bool:
        """标记 Interaction 广播失败，让 reaper 重新发送同一事件。

        Args:
            runtime_epoch: Episode 所属 Assistant Runtime 世代。
            episode_id: Episode 稳定编号。

        Returns:
            存在待发送凭据并成功解除 in-flight 标记时返回真。
        """

        key = (runtime_epoch, episode_id.strip())
        with self._lock:
            if key in self._published_episode_keys:
                return False
            if key not in self._publication_outbox:
                return False
            self._queued_publication_keys.discard(key)
            return True

    def _enqueue_publication(self, episode: UnattendedInputEpisode) -> None:
        """把稳定 Episode 放入 outbox，并立即尝试一次公共事件发布。

        Args:
            episode: Runtime Memory 已经关闭的不可变 Episode。
        """

        key = _publication_key(episode)
        if key in self._published_episode_keys:
            self._publication_outbox.pop(key, None)
            self._queued_publication_keys.discard(key)
            return
        self._publication_outbox[key] = episode
        self._publish_once(episode)

    def _publish_once(self, episode: UnattendedInputEpisode) -> bool:
        """按完整身份幂等发布一个 Episode 历史块。

        Args:
            episode: 已写入 Runtime Memory 的不可变 Episode。

        Returns:
            事件已成功入队或此前已经入队时返回真。
        """

        key = _publication_key(episode)
        if key in self._published_episode_keys:
            self._publication_outbox.pop(key, None)
            self._queued_publication_keys.discard(key)
            return True
        if key in self._queued_publication_keys:
            return False
        publisher = self._publish_episode_committed
        if publisher is None:
            self._published_episode_keys.add(key)
            self._publication_outbox.pop(key, None)
            self._queued_publication_keys.discard(key)
            return True
        event = RuntimeEvent(
            type="self_reflection.episode_committed",
            source="main_process",
            payload={
                "self_reflection_id": episode.identity.self_reflection_id,
                "episode_id": episode.identity.episode_id,
                "runtime_epoch": episode.identity.runtime_epoch,
                "revision": 4,
                "actionable": False,
                "history_block": list(
                    serialize_unattended_episode_history(episode)
                ),
            },
            runtime_epoch=None,
        )
        try:
            publisher(event)
        except Exception:
            logger.exception(
                "Self-Reflection Episode 闭合事件入队失败 episode_id=%s",
                episode.identity.episode_id,
            )
            return False
        # 入队成功只代表 Runtime Event Queue 接住事件；Interaction 广播仍需显式 ack。
        self._queued_publication_keys.add(key)
        return True


def _episode_identity(snapshot: SelfReflectionSnapshot) -> UnattendedEpisodeIdentity:
    """把 Attention 快照转换为 Runtime Memory Episode 身份。

    Args:
        snapshot: 已通过 Attention 状态机校验的完整快照。

    Returns:
        Runtime Memory 使用的联合身份。
    """

    return UnattendedEpisodeIdentity(
        self_reflection_id=snapshot.self_reflection_id,
        runtime_epoch=snapshot.runtime_epoch,
        episode_id=snapshot.episode_id,
    )


def _publication_key(episode: UnattendedInputEpisode) -> tuple[int, str]:
    """生成不会跨 Runtime epoch 冲突的公共事件幂等键。

    Args:
        episode: 已经闭合的 Episode。

    Returns:
        运行时世代和 Episode 编号组成的联合键。
    """

    return episode.identity.runtime_epoch, episode.identity.episode_id
