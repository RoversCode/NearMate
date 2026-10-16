"""定义自我反思在主进程与助手子进程之间传递的窄合同。"""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Literal, Protocol

from companion.foreground_self_reflection.prompt import RecoveryPromptFacts
from companion.foreground_self_reflection.models import (
    DivertFinalInputCommand,
    InputDiversionResult,
)


class AssistantRequestAdmissionLeaseProtocol(Protocol):
    """定义普通助手请求在 Bridge 入队前必须结算的提交资格。"""

    def commit_enqueued(self) -> bool:
        """请求成功交给 Bridge 后确认提交资格已经消费。"""

    def rollback(self) -> bool:
        """请求未进入 Bridge 时归还尚未消费的提交资格。"""


class ForegroundAttentionAdmissionPort(Protocol):
    """定义普通输入和 System Notice 共用的 Attention 准入端口。"""

    def divert_final_input(
        self,
        command: DivertFinalInputCommand,
    ) -> InputDiversionResult:
        """在线性化边界接纳或拒绝一项已经公开的 Final 用户输入。

        Args:
            command: 带稳定输入身份、世代和材料引用的改道命令。

        Returns:
            本项输入是否进入 episode；允许普通提交时同时返回必须结算的 lease。
        """

    def try_divert_reserved_text(
        self,
        command: DivertFinalInputCommand,
    ) -> InputDiversionResult:
        """只在 Attention 已活跃时接纳文本 Reservation。

        Args:
            command: 尚未创建普通 AssistantRequest 的文本输入。

        Returns:
            已写入 Episode 的归属，或不预留普通请求资格的未改道结果。
        """

    def reserve_assistant_request(
        self,
        *,
        request_id: str,
        runtime_epoch: int,
        request_source: Literal["system_notice", "timed_reminder_notice"],
    ) -> AssistantRequestAdmissionLeaseProtocol | None:
        """为没有输入 diversion 的普通请求预留 Bridge 提交资格。

        Args:
            request_id: 即将提交的稳定助手请求编号。
            runtime_epoch: 请求准备时所属 Assistant Runtime 世代。
            request_source: Activity 或 Reminder Notice 的独立请求来源。

        Returns:
            取得资格时返回 lease；Attention 已占用或世代失效时返回空。
        """


@dataclass(frozen=True, slots=True)
class TakeoverReceipt:
    """记录一次 Self-Reflection 接管是否进入唯一可见输出路径。

    Args:
        status: 接管已接受、被拒绝或提交过程中失败。
        entry_output_accepted: 入场表达是否已经进入正常可见输出链。
        reason: 不暴露给用户的稳定收口原因。
    """

    status: Literal["accepted", "rejected", "failed"]
    entry_output_accepted: bool = False
    reason: str = ""

    @property
    def accepted(self) -> bool:
        """判断入场表达是否已经建立唯一接管路径。

        Returns:
            入场请求已经进入共享 Foreground 输出链时返回真。
        """

        return self.status == "accepted" and self.entry_output_accepted

    @property
    def suppress_source_notice(self) -> bool:
        """说明普通 Cognitive 失败 Notice 是否必须被抑制。

        入场表达一旦进入正常输出链，用户已经进入 Self-Reflection 的唯一表达路径。
        Recovery 随后提交失败也只能沿当前状态收口，不能再补发普通 Cognitive
        Notice，否则用户会同时收到两套互相冲突的说明。

        Returns:
            入场表达已经可靠接纳时返回真。
        """

        return self.entry_output_accepted


@dataclass(frozen=True, slots=True)
class SelfReflectionEntryContext:
    """保存 Self-Reflection 接管时的可见入场请求。

    Args:
        request_id: 入场可见输出使用的稳定请求编号。
        self_reflection_id: 所属自我反思编号。
        episode_id: 所属未处理插话片段编号。
        runtime_epoch: 所属 Assistant Runtime 世代。
        source_activity_id: 被恢复的 Cognitive Activity 编号。
        instruction: 已经渲染成自然语言的内部指令，不再让模型猜机器枚举。
        fallback_text: 模型失败时可以直接公开的安全短句。
    """

    request_id: str
    self_reflection_id: str
    episode_id: str
    runtime_epoch: int
    source_activity_id: str
    instruction: str
    fallback_text: str

    def __post_init__(self) -> None:
        """规范化身份和正文，并禁止把用户原话塞进表达上下文。"""

        for field_name in (
            "request_id",
            "self_reflection_id",
            "episode_id",
            "source_activity_id",
            "instruction",
            "fallback_text",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} 不能为空")
            object.__setattr__(self, field_name, value.strip())
        if type(self.runtime_epoch) is not int or self.runtime_epoch <= 0:
            raise ValueError("runtime_epoch 必须是大于零的整数")


@dataclass(frozen=True, slots=True)
class SelfReflectionRecoveryContext:
    """保存可见 Recovery Turn 的身份、截止时间和失败事实。

    Args:
        request_id: Recovery 可见输出使用的稳定请求编号。
        self_reflection_id: 本次自我反思编号。
        episode_id: 收集反思期间新输入的片段编号。
        runtime_epoch: 所属 Assistant Runtime 世代。
        source_activity_id: 被恢复的 Cognitive Activity 编号。
        source_request_id: 创建来源 Activity 的 Foreground 请求编号。
        source_protocol_transaction_id: 来源 Cognitive 工具事务编号。
        deadline: 从 Attention Admission 起计算的绝对单调截止时间。
        prompt_facts: 原目标、旧方法能力和安全失败说明。
        fallback_text: Recovery 模型失败时可以公开的确定性正文。
    """

    request_id: str
    self_reflection_id: str
    episode_id: str
    runtime_epoch: int
    source_activity_id: str
    source_request_id: str
    source_protocol_transaction_id: str
    deadline: float
    prompt_facts: RecoveryPromptFacts
    fallback_text: str

    def __post_init__(self) -> None:
        """规范化稳定身份，并拒绝无法安全执行的半完整上下文。"""

        for field_name in (
            "request_id",
            "self_reflection_id",
            "episode_id",
            "source_activity_id",
            "source_request_id",
            "source_protocol_transaction_id",
            "fallback_text",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} 不能为空")
            object.__setattr__(self, field_name, value.strip())
        if type(self.runtime_epoch) is not int or self.runtime_epoch <= 0:
            raise ValueError("runtime_epoch 必须是大于零的整数")
        if (
            isinstance(self.deadline, bool)
            or not isinstance(self.deadline, (int, float))
            or not isfinite(float(self.deadline))
            or self.deadline <= 0
        ):
            raise ValueError("deadline 必须是大于零的有限单调时间")
        if not isinstance(self.prompt_facts, RecoveryPromptFacts):
            raise ValueError("prompt_facts 必须是 RecoveryPromptFacts")


@dataclass(frozen=True, slots=True)
class SelfReflectionInterruptionReplyContext:
    """保存一条辅助打扰回应的身份和固定表达约束。

    Args:
        request_id: 辅助回应使用的稳定 AssistantRequest 编号。
        self_reflection_id: 当前不可打断的 Self-Reflection 编号。
        episode_id: 本轮所有输入与辅助回应共同归属的片段编号。
        runtime_epoch: 所属 Assistant Runtime 世代。
        trigger_input_identity: 触发本次回应的用户输入稳定身份。
        instruction: 只允许确认听到、不得执行输入内容的内部指令。
        fallback_text: 模型异常时仍可安全公开的确定性短句。
    """

    request_id: str
    self_reflection_id: str
    episode_id: str
    runtime_epoch: int
    trigger_input_identity: str
    instruction: str
    fallback_text: str

    def __post_init__(self) -> None:
        """规范化身份和正文，拒绝无法归入 episode 的半完整请求。"""

        for field_name in (
            "request_id",
            "self_reflection_id",
            "episode_id",
            "trigger_input_identity",
            "instruction",
            "fallback_text",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} 不能为空")
            object.__setattr__(self, field_name, value.strip())
        if type(self.runtime_epoch) is not int or self.runtime_epoch <= 0:
            raise ValueError("runtime_epoch 必须是大于零的整数")


@dataclass(frozen=True, slots=True)
class SelfReflectionToolCommitDecision:
    """返回主进程是否已经允许子进程越过真实工具提交边界。

    Args:
        approved: 主进程已完成 Attention 授权和必要的 canonical 登记时为真。
        reason: 批准、deadline、热替换或等待失效对应的稳定原因。
    """

    approved: bool
    reason: str

    def __post_init__(self) -> None:
        """拒绝没有明确结论或稳定原因的回执。"""

        if type(self.approved) is not bool:
            raise ValueError("approved 必须是布尔值")
        normalized_reason = self.reason.strip() if isinstance(self.reason, str) else ""
        if not normalized_reason:
            raise ValueError("reason 不能为空")
        object.__setattr__(self, "reason", normalized_reason)


@dataclass(frozen=True, slots=True)
class SelfReflectionToolCommitDecisionCommand:
    """把主进程的工具提交批准或拒绝送回原 Assistant Runtime。

    Args:
        commit_request_id: 子进程正在同步等待的提交请求编号。
        self_reflection_id: 本次自我反思编号。
        source_activity_id: 被恢复的 Cognitive Activity 编号。
        runtime_epoch: 回执针对的 Assistant Runtime 世代。
        tool_call_id: 模型产生的唯一工具调用编号。
        tool_name: 本次实际选择的工具名。
        approved: 主进程是否已经完成所有提交前置条件。
        reason: 批准或拒绝的稳定原因。
        issued_at: 主进程创建回执时的单调时间。
    """

    commit_request_id: str
    self_reflection_id: str
    source_activity_id: str
    runtime_epoch: int
    tool_call_id: str
    tool_name: str
    approved: bool
    reason: str
    issued_at: float
    kind: Literal["self_reflection_tool_commit_decision"] = (
        "self_reflection_tool_commit_decision"
    )

    def __post_init__(self) -> None:
        """规范化完整回执身份，并拒绝含糊的批准事实。"""

        for field_name in (
            "commit_request_id",
            "self_reflection_id",
            "source_activity_id",
            "tool_call_id",
            "tool_name",
            "reason",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} 不能为空")
            object.__setattr__(self, field_name, value.strip())
        if type(self.runtime_epoch) is not int or self.runtime_epoch <= 0:
            raise ValueError("runtime_epoch 必须是大于零的整数")
        if type(self.approved) is not bool:
            raise ValueError("approved 必须是布尔值")
        if self.approved and self.reason != "tool_commit_approved":
            raise ValueError("批准回执必须使用 tool_commit_approved 原因")
        if not self.approved and self.reason == "tool_commit_approved":
            raise ValueError("拒绝回执不能使用批准原因")
        if (
            not isinstance(self.issued_at, (int, float))
            or isinstance(self.issued_at, bool)
            or not isfinite(float(self.issued_at))
            or self.issued_at < 0
        ):
            raise ValueError("issued_at 必须是非负有限单调时间")
