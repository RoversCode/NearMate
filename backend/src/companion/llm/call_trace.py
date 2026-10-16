from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from uuid import uuid4


ForegroundLlmRound = Literal[
    "initial",
    "tool_followup",
    "self_reflection",
    "self_reflection_entry",
]


@dataclass(frozen=True)
class LlmCallTrace:
    """标识一次可独立计数的 Foreground 模型调用。

    Args:
        request_id: 当前助手请求编号。
        generation_id: 当前输出 generation 编号。
        llm_call_id: 本次模型调用的唯一编号。
        round: 首轮决策或工具结果续写。
        request_source: 当前助手请求的真实来源。
    """

    request_id: str
    generation_id: str
    llm_call_id: str
    round: ForegroundLlmRound
    request_source: str

    def log_fields(self, *, protocol: str) -> dict[str, str]:
        """返回结构化日志使用的稳定身份字段。

        Args:
            protocol: 当前客户端使用的 API 协议。

        Returns:
            可直接展开到结构化日志事件的字段字典。
        """

        return {
            "request_id": self.request_id,
            "generation_id": self.generation_id,
            "llm_call_id": self.llm_call_id,
            "round": self.round,
            "request_source": self.request_source,
            "protocol": protocol,
        }


@dataclass(frozen=True)
class ForegroundRequestTrace:
    """保存同一 generation 内多轮 Foreground 调用的共同身份。

    Args:
        request_id: 当前助手请求编号。
        generation_id: 当前输出 generation 编号。
        request_source: 当前助手请求的真实来源。
    """

    request_id: str
    generation_id: str
    request_source: str

    def llm_call(self, round: ForegroundLlmRound) -> LlmCallTrace:
        """为指定 Foreground 轮次生成确定性调用编号。

        Args:
            round: 首轮决策或工具结果续写。

        Returns:
            可贯穿模型客户端和业务日志的只读调用身份。
        """

        return LlmCallTrace(
            request_id=self.request_id,
            generation_id=self.generation_id,
            llm_call_id=f"llm-{uuid4().hex}",
            round=round,
            request_source=self.request_source,
        )


@dataclass(frozen=True)
class ActivityLlmCallTrace:
    """标识一次 Activity 后台任务的普通文本流模型调用。

    Args:
        activity_id: 当前 Activity 编号，用于日志关联。
        activity_kind: 当前 Activity 种类。
        llm_call_id: 本次模型调用的唯一编号。
    """

    activity_id: str
    activity_kind: str
    llm_call_id: str

    @classmethod
    def new(cls, *, activity_id: str, activity_kind: str) -> ActivityLlmCallTrace:
        """为一次 Activity 模型调用生成唯一身份。

        Args:
            activity_id: 当前 Activity 编号。
            activity_kind: 当前 Activity 种类。

        Returns:
            可贯穿 Knowledge 日志的只读调用身份。
        """

        return cls(
            activity_id=activity_id,
            activity_kind=activity_kind,
            llm_call_id=f"llm-{uuid4().hex}",
        )

    def log_fields(self, *, protocol: str) -> dict[str, str]:
        """返回结构化日志使用的稳定身份字段。

        Args:
            protocol: 当前客户端使用的 API 协议。

        Returns:
            可直接展开到结构化日志事件的字段字典。
        """

        return {
            "activity_id": self.activity_id,
            "activity_kind": self.activity_kind,
            "llm_call_id": self.llm_call_id,
            "protocol": protocol,
        }
