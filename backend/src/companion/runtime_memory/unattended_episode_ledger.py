"""在线性化锁内管理未处理插话片段的身份和幂等状态。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from companion.runtime_memory.unattended_episode import (
    UnattendedEpisodeIdentity,
    UnattendedInputAppendStatus,
    UnattendedInputEpisode,
    UnattendedInputItem,
    UnattendedInterruptionReply,
    UnattendedReplyAppendStatus,
)


@dataclass(slots=True)
class _OpenUnattendedInputEpisode:
    """保存片段关闭前仍可变化的输入。

    Args:
        identity: 自我反思会话、运行时世代和片段的联合身份。
        opened_at: 片段开始接纳输入的本地时间。
        inputs_by_identity: 按稳定输入身份去重的输入集合。
        input_identity_by_sequence: 防止同一顺序号被不同输入占用。
    """

    identity: UnattendedEpisodeIdentity
    opened_at: datetime
    inputs_by_identity: dict[str, UnattendedInputItem] = field(default_factory=dict)
    input_identity_by_sequence: dict[int, str] = field(default_factory=dict)
    replies_by_request_id: dict[str, UnattendedInterruptionReply] = field(
        default_factory=dict
    )
    sequence_owner: dict[int, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class UnattendedEpisodeCloseResult:
    """说明一次 close 是否首次把片段冻结成不可变记录。

    Args:
        episode: 首次关闭或重试读取到的同一份不可变片段。
        newly_closed: 本次调用是否真正完成了状态转换。
    """

    episode: UnattendedInputEpisode
    newly_closed: bool


class UnattendedEpisodeLedger:
    """保存 episode 身份、开放状态和进程内永久闭合墓碑。

    该对象故意不创建第二把锁。拥有它的 ``RuntimeMemoryStore`` 必须在同一把
    线性化锁内调用所有方法，才能让普通 Turn 提交与 episode 归属互斥。
    """

    def __init__(self) -> None:
        """创建没有任何开放或闭合片段的账本。"""

        self._open_episodes: dict[
            UnattendedEpisodeIdentity,
            _OpenUnattendedInputEpisode,
        ] = {}
        self._closed_episodes: dict[
            UnattendedEpisodeIdentity,
            UnattendedInputEpisode,
        ] = {}
        self._identity_by_episode_id: dict[str, UnattendedEpisodeIdentity] = {}
        self._identity_by_input: dict[str, UnattendedEpisodeIdentity] = {}
        self._identity_by_reflection: dict[
            tuple[str, int],
            UnattendedEpisodeIdentity,
        ] = {}

    def open(
        self,
        identity: UnattendedEpisodeIdentity,
        *,
        opened_at: datetime,
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

        self._validate_identity(identity)
        if not isinstance(opened_at, datetime):
            raise ValueError("opened_at 必须是 datetime")
        self._assert_identity_compatible(identity)
        if identity in self._open_episodes or identity in self._closed_episodes:
            return False
        self._identity_by_episode_id[identity.episode_id] = identity
        self._identity_by_reflection[
            (identity.self_reflection_id, identity.runtime_epoch)
        ] = identity
        self._open_episodes[identity] = _OpenUnattendedInputEpisode(
            identity=identity,
            opened_at=opened_at,
        )
        return True

    def append_input(
        self,
        identity: UnattendedEpisodeIdentity,
        item: UnattendedInputItem,
    ) -> UnattendedInputAppendStatus:
        """按权威序号幂等追加一项 Text 或 Final ASR。

        Args:
            identity: 当前未处理插话片段的联合身份。
            item: 已经越过输入提交边界的不可变用户输入。

        Returns:
            appended、duplicate、closed 或 not_open。

        Raises:
            ValueError: 同一稳定身份或顺序号被改写为其它内容。
        """

        self._validate_identity(identity)
        if not isinstance(item, UnattendedInputItem):
            raise ValueError("item 必须是 UnattendedInputItem")
        self._assert_identity_compatible(identity)
        existing_input_episode = self._identity_by_input.get(item.input_identity)
        if existing_input_episode is not None:
            if existing_input_episode != identity:
                raise ValueError("input_identity 已经属于其它未处理插话片段")
            existing_item = self._find_input(identity, item.input_identity)
            if existing_item != item:
                raise ValueError("同一 input_identity 不能改写正文或顺序")
            return "duplicate"
        if identity in self._closed_episodes:
            return "closed"
        open_episode = self._open_episodes.get(identity)
        if open_episode is None:
            return "not_open"
        sequence_owner = open_episode.input_identity_by_sequence.get(
            item.episode_sequence
        )
        if sequence_owner is not None and sequence_owner != item.input_identity:
            raise ValueError("同一 episode_sequence 不能分配给不同输入")
        existing_sequence_owner = open_episode.sequence_owner.get(item.episode_sequence)
        if existing_sequence_owner is not None and existing_sequence_owner != item.input_identity:
            raise ValueError("episode_sequence 已经被其它账本项占用")
        open_episode.inputs_by_identity[item.input_identity] = item
        open_episode.input_identity_by_sequence[
            item.episode_sequence
        ] = item.input_identity
        open_episode.sequence_owner[item.episode_sequence] = item.input_identity
        self._identity_by_input[item.input_identity] = identity
        return "appended"

    def append_reply(
        self,
        identity: UnattendedEpisodeIdentity,
        reply: UnattendedInterruptionReply,
    ) -> UnattendedReplyAppendStatus:
        """幂等追加一条已经完成 committed settlement 的辅助回应。

        Args:
            identity: 当前开放 episode 的联合身份。
            reply: 带统一顺序和可见正文的辅助回应结算。

        Returns:
            appended、duplicate、closed 或 not_open。
        """

        self._validate_identity(identity)
        if not isinstance(reply, UnattendedInterruptionReply):
            raise ValueError("reply 必须是 UnattendedInterruptionReply")
        self._assert_identity_compatible(identity)
        if identity in self._closed_episodes:
            existing = next(
                (
                    item
                    for item in self._closed_episodes[identity].interruption_replies
                    if item.request_id == reply.request_id
                ),
                None,
            )
            if existing is None:
                return "closed"
            if existing != reply:
                raise ValueError("同一回应请求不能改写结算事实")
            return "duplicate"
        open_episode = self._open_episodes.get(identity)
        if open_episode is None:
            return "not_open"
        if reply.trigger_input_identity not in open_episode.inputs_by_identity:
            raise ValueError("辅助回应必须关联当前片段内的输入")
        existing = open_episode.replies_by_request_id.get(reply.request_id)
        if existing is not None:
            if existing != reply:
                raise ValueError("同一回应请求不能改写结算事实")
            return "duplicate"
        sequence_owner = open_episode.sequence_owner.get(reply.episode_sequence)
        if sequence_owner is not None:
            raise ValueError("episode_sequence 已经被其它账本项占用")
        open_episode.replies_by_request_id[reply.request_id] = reply
        open_episode.sequence_owner[reply.episode_sequence] = reply.request_id
        return "appended"

    def close(
        self,
        identity: UnattendedEpisodeIdentity,
        *,
        closed_at: datetime,
    ) -> UnattendedEpisodeCloseResult:
        """幂等关闭片段并冻结按权威顺序排列的不可变记录。

        Args:
            identity: 当前未处理插话片段的联合身份。
            closed_at: 输入 diversion lane 关闭的本地时间。

        Returns:
            闭合片段和本次是否首次关闭。

        Raises:
            ValueError: 片段未开启、身份冲突或关闭时间无效。
        """

        self._validate_identity(identity)
        if not isinstance(closed_at, datetime):
            raise ValueError("closed_at 必须是 datetime")
        self._assert_identity_compatible(identity)
        preview = self.preview_close(identity, closed_at=closed_at)
        if not preview.newly_closed:
            return preview
        self.commit_close(preview.episode)
        return preview

    def preview_close(
        self,
        identity: UnattendedEpisodeIdentity,
        *,
        closed_at: datetime,
    ) -> UnattendedEpisodeCloseResult:
        """只构造闭合快照，不改变开放或闭合索引。

        Args:
            identity: 当前未处理插话片段的联合身份。
            closed_at: 计划停止接纳输入的本地时间。

        Returns:
            已闭合墓碑或尚未提交的不可变片段快照。

        Raises:
            ValueError: 片段未开启、身份冲突或关闭时间无效。
        """

        self._validate_identity(identity)
        if not isinstance(closed_at, datetime):
            raise ValueError("closed_at 必须是 datetime")
        self._assert_identity_compatible(identity)
        existing = self._closed_episodes.get(identity)
        if existing is not None:
            return UnattendedEpisodeCloseResult(
                episode=existing,
                newly_closed=False,
            )
        open_episode = self._open_episodes.get(identity)
        if open_episode is None:
            raise ValueError("未处理插话片段尚未开启")
        episode = UnattendedInputEpisode(
            identity=identity,
            opened_at=open_episode.opened_at,
            closed_at=closed_at,
            inputs=tuple(
                sorted(
                    open_episode.inputs_by_identity.values(),
                    key=lambda item: item.episode_sequence,
                )
            ),
            interruption_replies=tuple(
                sorted(
                    open_episode.replies_by_request_id.values(),
                    key=lambda item: item.episode_sequence,
                )
            ),
        )
        return UnattendedEpisodeCloseResult(episode=episode, newly_closed=True)

    def commit_close(self, episode: UnattendedInputEpisode) -> bool:
        """提交之前构造好的闭合快照，并写入不可变墓碑。

        Args:
            episode: 已由 ``preview_close`` 构造且未被改写的片段。

        Returns:
            首次提交返回真，重复提交返回假。

        Raises:
            ValueError: 快照身份不匹配当前开放片段。
        """

        identity = episode.identity
        self._validate_identity(identity)
        self._assert_identity_compatible(identity)
        existing = self._closed_episodes.get(identity)
        if existing is not None:
            if existing != episode:
                raise ValueError("同一 episode 不能改写闭合事实")
            return False
        open_episode = self._open_episodes.get(identity)
        if open_episode is None:
            raise ValueError("未处理插话片段尚未开启")
        expected = self.preview_close(identity, closed_at=episode.closed_at)
        if expected.episode != episode:
            raise ValueError("闭合快照与开放账本不一致")
        self._closed_episodes[identity] = episode
        self._open_episodes.pop(identity, None)
        return True

    def get(
        self,
        identity: UnattendedEpisodeIdentity,
    ) -> UnattendedInputEpisode | None:
        """读取一段已经闭合的未处理插话片段。

        Args:
            identity: 自我反思会话、运行时世代和片段的联合身份。

        Returns:
            片段已闭合时返回不可变记录，否则返回空。
        """

        self._validate_identity(identity)
        return self._closed_episodes.get(identity)

    def identity_for_input(
        self,
        input_identity: str,
    ) -> UnattendedEpisodeIdentity | None:
        """读取一个稳定输入身份已经归属的 episode。

        Args:
            input_identity: Text submission 或 Final ASR 的稳定身份。

        Returns:
            输入已经进入 episode 时返回联合身份，否则返回空。
        """

        normalized_input_identity = input_identity.strip()
        return self._identity_by_input.get(normalized_input_identity)

    def _assert_identity_compatible(
        self,
        identity: UnattendedEpisodeIdentity,
    ) -> None:
        """拒绝复用 episode 编号或给同一反思会话改绑片段。

        Args:
            identity: 需要和现有索引核对的片段联合身份。

        Raises:
            ValueError: 片段编号或自我反思身份已经绑定其它记录。
        """

        existing_by_id = self._identity_by_episode_id.get(identity.episode_id)
        if existing_by_id is not None and existing_by_id != identity:
            raise ValueError("episode_id 已经绑定其它自我反思身份")
        existing_by_reflection = self._identity_by_reflection.get(
            (identity.self_reflection_id, identity.runtime_epoch)
        )
        if existing_by_reflection is not None and existing_by_reflection != identity:
            raise ValueError("同一自我反思只能拥有一个未处理插话片段")

    def _find_input(
        self,
        identity: UnattendedEpisodeIdentity,
        input_identity: str,
    ) -> UnattendedInputItem | None:
        """从开放或闭合记录中读取一项已经接纳的输入。

        Args:
            identity: 输入所属的片段联合身份。
            input_identity: Text submission 或 Final ASR 的稳定身份。

        Returns:
            已接纳的输入记录；不存在时返回空。
        """

        open_episode = self._open_episodes.get(identity)
        if open_episode is not None:
            return open_episode.inputs_by_identity.get(input_identity)
        closed_episode = self._closed_episodes.get(identity)
        if closed_episode is None:
            return None
        return next(
            (
                item
                for item in closed_episode.inputs
                if item.input_identity == input_identity
            ),
            None,
        )

    @staticmethod
    def _validate_identity(identity: UnattendedEpisodeIdentity) -> None:
        """拒绝调用方传入未完成自身校验的联合身份。

        Args:
            identity: 调用方提供的片段联合身份。

        Raises:
            ValueError: 参数不是合法的联合身份对象。
        """

        if not isinstance(identity, UnattendedEpisodeIdentity):
            raise ValueError("identity 必须是 UnattendedEpisodeIdentity")
