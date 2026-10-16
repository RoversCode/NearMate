"""定义未处理插话片段及其唯一闭合历史投影。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal


UnattendedInputSource = Literal["text", "final_asr"]
UnattendedInputAppendStatus = Literal[
    "appended",
    "duplicate",
    "closed",
    "not_open",
    "ordinary_turn",
]
UnattendedReplyAppendStatus = Literal[
    "appended",
    "duplicate",
    "closed",
    "not_open",
]
UnattendedReplySettlement = Literal["completed", "interrupted", "failed"]
UNATTENDED_EPISODE_HISTORY_POLICY = "unattended_episode"
UNATTENDED_EPISODE_CLOSED_FACT = (
    "刚才我在集中重新梳理前面的事情，这些内容当时没有处理，不是待办，也没有"
    "被我当成已经接受的任务；之后也不会因为这段历史自动执行。"
)


@dataclass(frozen=True, slots=True)
class UnattendedEpisodeIdentity:
    """唯一标识一轮前台自我反思收集的未处理片段。

    Args:
        self_reflection_id: 本轮前台自我反思会话编号。
        runtime_epoch: 接纳这轮输入的 Assistant Runtime 世代。
        episode_id: 本轮未处理插话片段编号。
    """

    self_reflection_id: str
    runtime_epoch: int
    episode_id: str

    def __post_init__(self) -> None:
        """裁剪稳定身份，并拒绝无法归属到运行时的记录。"""

        normalized_reflection_id = self.self_reflection_id.strip()
        normalized_episode_id = self.episode_id.strip()
        if not normalized_reflection_id or not normalized_episode_id:
            raise ValueError("self_reflection_id 和 episode_id 不能为空")
        if (
            not isinstance(self.runtime_epoch, int)
            or isinstance(self.runtime_epoch, bool)
            or self.runtime_epoch <= 0
        ):
            raise ValueError("runtime_epoch 必须是大于零的整数")
        object.__setattr__(self, "self_reflection_id", normalized_reflection_id)
        object.__setattr__(self, "episode_id", normalized_episode_id)


@dataclass(frozen=True, slots=True)
class UnattendedMaterialReference:
    """保存未处理输入中允许进入运行期记忆的安全材料信息。

    Args:
        file_name: 不带本机目录的材料文件名。
        media_type: 材料的媒体类型。
    """

    file_name: str
    media_type: str

    def __post_init__(self) -> None:
        """拒绝空值和路径，避免运行期记忆泄漏本机目录。"""

        normalized_file_name = self.file_name.strip()
        normalized_media_type = self.media_type.strip()
        if not normalized_file_name or not normalized_media_type:
            raise ValueError("file_name 和 media_type 不能为空")
        if "/" in normalized_file_name or "\\" in normalized_file_name:
            raise ValueError("file_name 只能保存文件名，不能包含路径")
        if normalized_file_name in {".", ".."}:
            raise ValueError("file_name 不是有效文件名")
        object.__setattr__(self, "file_name", normalized_file_name)
        object.__setattr__(self, "media_type", normalized_media_type)


@dataclass(frozen=True, slots=True)
class UnattendedInputItem:
    """保存一项已经越过输入提交边界、但当时没有处理的原话。

    Args:
        input_identity: Text submission 或 Final ASR 的稳定幂等身份。
        source: 输入来自文本提交还是 Final ASR。
        text: Desktop 已经展示的真实用户正文。
        episode_sequence: 主进程在 Attention 锁内分配的顺序号。
        accepted_at: 输入真正归入片段的本地时间。
        materials: 只含安全文件名和媒体类型的材料引用。
    """

    input_identity: str
    source: UnattendedInputSource
    text: str
    episode_sequence: int
    accepted_at: datetime
    materials: tuple[UnattendedMaterialReference, ...] = ()

    def __post_init__(self) -> None:
        """保留用户原文，同时拒绝空输入、坏顺序和未知来源。"""

        normalized_identity = self.input_identity.strip()
        if not normalized_identity:
            raise ValueError("input_identity 不能为空")
        if self.source not in {"text", "final_asr"}:
            raise ValueError("source 只允许 text 或 final_asr")
        if not isinstance(self.text, str) or not self.text.strip():
            raise ValueError("text 不能为空")
        if (
            not isinstance(self.episode_sequence, int)
            or isinstance(self.episode_sequence, bool)
            or self.episode_sequence <= 0
        ):
            raise ValueError("episode_sequence 必须是大于零的整数")
        if not isinstance(self.accepted_at, datetime):
            raise ValueError("accepted_at 必须是 datetime")
        normalized_materials = tuple(self.materials)
        if not all(
            isinstance(item, UnattendedMaterialReference)
            for item in normalized_materials
        ):
            raise ValueError("materials 必须是安全材料引用")
        object.__setattr__(self, "input_identity", normalized_identity)
        object.__setattr__(self, "materials", normalized_materials)


@dataclass(frozen=True, slots=True)
class UnattendedInterruptionReply:
    """保存一条辅助打扰回应真实公开的正文与最终结算。

    Args:
        request_id: 辅助回应的稳定请求编号。
        generation_id: 本次辅助 generation 编号；请求未进入输出域时为空。
        trigger_input_identity: 触发回应的 episode 输入身份。
        content: 已经公开的完整或部分正文；失败且未出字时可为空。
        settlement: 回应正常完成、被下一输入打断或生成失败。
        episode_sequence: 与用户输入共用的权威发生顺序。
        settled_at: 可见输出真正提交完成的本地时间。
    """

    request_id: str
    generation_id: str | None
    trigger_input_identity: str
    content: str
    settlement: UnattendedReplySettlement
    episode_sequence: int
    settled_at: datetime

    def __post_init__(self) -> None:
        """规范化稳定身份，并拒绝会破坏 episode 顺序的结算记录。"""

        for field_name in ("request_id", "trigger_input_identity"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} 不能为空")
            object.__setattr__(self, field_name, value.strip())
        if self.generation_id is not None:
            if not isinstance(self.generation_id, str) or not self.generation_id.strip():
                raise ValueError("generation_id 必须是非空字符串或空")
            object.__setattr__(self, "generation_id", self.generation_id.strip())
        if not isinstance(self.content, str):
            raise ValueError("content 必须是字符串")
        if self.settlement not in {"completed", "interrupted", "failed"}:
            raise ValueError("settlement 不是允许的回应结算")
        if (
            not isinstance(self.episode_sequence, int)
            or isinstance(self.episode_sequence, bool)
            or self.episode_sequence <= 0
        ):
            raise ValueError("episode_sequence 必须是大于零的整数")
        if not isinstance(self.settled_at, datetime):
            raise ValueError("settled_at 必须是 datetime")


@dataclass(frozen=True, slots=True)
class UnattendedInputEpisode:
    """保存一段已经闭合、只可回忆且不可自动执行的未处理历史。

    Args:
        identity: 自我反思会话、运行时世代和片段的联合身份。
        opened_at: 片段开始接纳输入的本地时间。
        closed_at: 片段停止接纳输入并形成历史的本地时间。
        inputs: 按主进程接纳顺序保存的 Text 与 Final ASR。
        interruption_replies: 按统一 episode 序号保存的辅助回应结算。
        actionable: 固定为假，明确这些历史不是待办或动作授权。
    """

    identity: UnattendedEpisodeIdentity
    opened_at: datetime
    closed_at: datetime
    inputs: tuple[UnattendedInputItem, ...] = ()
    interruption_replies: tuple[UnattendedInterruptionReply, ...] = ()
    actionable: Literal[False] = False

    def __post_init__(self) -> None:
        """验证闭合时间、顺序和幂等身份没有互相冲突。"""

        if not isinstance(self.identity, UnattendedEpisodeIdentity):
            raise ValueError("identity 必须是 UnattendedEpisodeIdentity")
        if not isinstance(self.opened_at, datetime) or not isinstance(
            self.closed_at,
            datetime,
        ):
            raise ValueError("opened_at 和 closed_at 必须是 datetime")
        try:
            closes_before_open = self.closed_at < self.opened_at
        except TypeError as exception:
            raise ValueError("opened_at 和 closed_at 必须使用一致的时区") from exception
        if closes_before_open:
            raise ValueError("closed_at 不能早于 opened_at")
        normalized_inputs = tuple(self.inputs)
        normalized_replies = tuple(self.interruption_replies)
        if not all(isinstance(item, UnattendedInputItem) for item in normalized_inputs):
            raise ValueError("inputs 必须是 UnattendedInputItem")
        input_identities = [item.input_identity for item in normalized_inputs]
        input_sequences = [item.episode_sequence for item in normalized_inputs]
        if len(input_identities) != len(set(input_identities)):
            raise ValueError("同一 input_identity 不能在片段内重复")
        if len(input_sequences) != len(set(input_sequences)):
            raise ValueError("同一 episode_sequence 不能分配给两项输入")
        if input_sequences != sorted(input_sequences):
            raise ValueError("inputs 必须按 episode_sequence 排序")
        if not all(
            isinstance(item, UnattendedInterruptionReply)
            for item in normalized_replies
        ):
            raise ValueError("interruption_replies 类型不正确")
        reply_request_ids = [item.request_id for item in normalized_replies]
        if len(reply_request_ids) != len(set(reply_request_ids)):
            raise ValueError("同一回应请求不能在片段内重复")
        known_input_identities = set(input_identities)
        if any(
            item.trigger_input_identity not in known_input_identities
            for item in normalized_replies
        ):
            raise ValueError("辅助回应必须关联当前片段内的输入")
        ordered_sequences = [item.episode_sequence for item in self.ordered_items]
        if len(ordered_sequences) != len(set(ordered_sequences)):
            raise ValueError("输入和回应不能占用同一 episode_sequence")
        if self.actionable is not False:
            raise ValueError("未处理插话片段的 actionable 必须固定为假")
        object.__setattr__(self, "inputs", normalized_inputs)
        object.__setattr__(self, "interruption_replies", normalized_replies)

    @property
    def ordered_items(
        self,
    ) -> tuple[UnattendedInputItem | UnattendedInterruptionReply, ...]:
        """返回按统一序号排列的输入和辅助回应。

        Returns:
            不改变原对象的有序 episode 账本快照。
        """

        return tuple(
            sorted(
                (*self.inputs, *self.interruption_replies),
                key=lambda item: item.episode_sequence,
            )
        )


@dataclass(frozen=True, slots=True)
class UnattendedEpisodeHistoryItem:
    """描述闭合片段要原子追加的一条普通历史消息。

    Args:
        role: 该条历史使用 user 还是 assistant 角色。
        content: 已经带完整未处理语义的历史正文。
        occurred_at: 这条历史在现实中发生的本地时间。
        episode_role: 该条消息保存单项输入、辅助回应还是闭合说明。
        source_identity: 输入或回应的稳定身份；闭合说明为空。
        reply_settlement: 辅助回应的结算结果；其它消息为空。
    """

    role: Literal["user", "assistant"]
    content: str
    occurred_at: datetime
    episode_role: Literal["input", "interruption_reply", "closure"]
    source_identity: str | None = None
    reply_settlement: UnattendedReplySettlement | None = None


def project_unattended_input_episode(
    episode: UnattendedInputEpisode,
) -> tuple[UnattendedEpisodeHistoryItem, ...]:
    """把片段投影成任何历史消费者都能理解的闭合对话。

    Args:
        episode: 已经停止接纳输入的不可变片段。

    Returns:
        没有输入时返回空；否则返回一条合并用户原话和一条闭合助手说明。
    """

    if not episode.inputs:
        return ()
    history: list[UnattendedEpisodeHistoryItem] = []
    for item in episode.ordered_items:
        if isinstance(item, UnattendedInputItem):
            content = item.text
            if item.materials:
                material_names = "、".join(
                    f"{material.file_name}（{material.media_type}）"
                    for material in item.materials
                )
                content = (
                    f"{content}\n"
                    f"[当时随这段话提交但没有被处理的材料：{material_names}]"
                )
            history.append(
                UnattendedEpisodeHistoryItem(
                    role="user",
                    content=content,
                    occurred_at=item.accepted_at,
                    episode_role="input",
                    source_identity=item.input_identity,
                )
            )
            continue
        if item.content.strip():
            history.append(
                UnattendedEpisodeHistoryItem(
                    role="assistant",
                    content=item.content,
                    occurred_at=item.settled_at,
                    episode_role="interruption_reply",
                    source_identity=item.request_id,
                    reply_settlement=item.settlement,
                )
            )
    history.append(
        UnattendedEpisodeHistoryItem(
            role="assistant",
            content=UNATTENDED_EPISODE_CLOSED_FACT,
            occurred_at=episode.closed_at,
            episode_role="closure",
        )
    )
    return tuple(history)


def serialize_unattended_episode_history(
    episode: UnattendedInputEpisode,
) -> tuple[dict[str, object], ...]:
    """把统一历史投影转换为可通过公共事件发送的 JSON 数据。

    Args:
        episode: 已经停止接纳输入的不可变片段。

    Returns:
        与稳定 Runtime Memory 使用相同顺序和正文的公共历史块。
    """

    return tuple(
        {
            "role": item.role,
            "content": item.content,
            "occurred_at": item.occurred_at.isoformat(),
            "episode_role": item.episode_role,
            "source_identity": item.source_identity,
            "reply_settlement": item.reply_settlement,
        }
        for item in project_unattended_input_episode(episode)
    )
