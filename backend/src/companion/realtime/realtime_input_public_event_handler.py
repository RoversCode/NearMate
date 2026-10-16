"""验证并发布实时输入领域的公共控制事件。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from companion.realtime.assistant_events import RuntimeEvent


REALTIME_INPUT_PUBLIC_EVENT_TYPES = frozenset(
    {
        "interaction.state_changed",
        "followup.state_changed",
        "wake.detected",
        "environment.snapshot_requested",
        "turn.state_changed",
        "input.smart_turn_waiting_user_started",
        "input.smart_turn_waiting_user_ended",
        "user.partial_transcript",
        "user.final_transcript",
        "barge_in.detected",
    }
)


def _validate_non_blank_string(value: str) -> str:
    """拒绝只有空白字符的公共字符串字段。

    Args:
        value: 已通过严格字符串类型校验的字段值。

    Returns:
        未改写的原始非空字符串。
    """

    if not value.strip():
        raise ValueError("公共字符串字段不能为空")
    return value


NonBlankString = Annotated[str, AfterValidator(_validate_non_blank_string)]
PositiveInteger = Annotated[int, Field(gt=0)]
FiniteFloat = Annotated[float, Field(allow_inf_nan=False)]


class _InputRuntimePayload(BaseModel):
    """校验所有语音事件都能归属到唯一输入 Runtime。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    input_runtime_id: str
    input_runtime_epoch: PositiveInteger

    @field_validator("input_runtime_id")
    @classmethod
    def validate_input_runtime_id(cls, value: str) -> str:
        """确认 Runtime 身份使用固定前缀和标准 UUID。

        Args:
            value: 待校验的输入 Runtime 身份。

        Returns:
            原始合法身份。
        """

        prefix = "input-runtime-"
        if not value.startswith(prefix):
            raise ValueError("input_runtime_id 前缀非法")
        UUID(value[len(prefix) :])
        return value

class _InputTurnPayload(_InputRuntimePayload):
    """校验需要排序的语音事件拥有数值 Turn 身份。"""

    turn_id: NonBlankString
    turn_sequence: PositiveInteger


class _FollowupStatePayload(_InputRuntimePayload):
    followup_state: Literal["followup_cool", "followup_hot"]
    source: Literal["manual_toggle"] | None = None


class _InteractionStatePayload(_InputRuntimePayload):
    """校验可恢复的交互状态快照，不接收额外字段或未知状态。

    Args:
        revision: 当前输入 Runtime 内的非负安全整数版本。
        followup_state: 是否仍可不经唤醒继续语音输入。
        turn_state: 输入和助手输出共同派生的当前轮次状态。
        session_id: 当前交互会话编号。
        source: 仅本次手动切换事件可携带的反馈来源。

    Returns:
        无返回值；实例只用于公共事件发布前的严格校验。
    """

    input_runtime_epoch: Annotated[int, Field(gt=0, le=2**53 - 1)]
    revision: Annotated[int, Field(ge=0, le=2**53 - 1)]
    followup_state: Literal["followup_cool", "followup_hot"]
    turn_state: Literal["idle", "listening", "processing", "speaking"]
    session_id: NonBlankString
    source: Literal["manual_toggle"] | None = None

    @model_validator(mode="after")
    def reject_explicit_null_source(self) -> "_InteractionStatePayload":
        """要求缺省来源直接省略，避免与非法空值混淆。

        Args:
            无。

        Returns:
            来源省略或为手动切换标记的合法快照。
        """

        if "source" in self.model_fields_set and self.source is None:
            raise ValueError("交互状态来源不能显式为空")
        return self


class _WakeDetectedPayload(_InputRuntimePayload):
    session_id: NonBlankString
    text: str | None = None
    detected_at: float | None = Field(default=None, ge=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def reject_explicit_null_text(self) -> "_WakeDetectedPayload":
        """区分未提供唤醒文案与显式空值。

        Returns:
            未携带 text 或携带字符串的合法载荷。
        """

        if "text" in self.model_fields_set and self.text is None:
            raise ValueError("wake text 不能显式为空")
        return self


class _EnvironmentSnapshotPayload(_InputTurnPayload):
    request_id: NonBlankString
    reason: Literal["voice_turn_started"]
    session_id: NonBlankString
    created_at: NonBlankString


class _TurnStatePayload(_InputRuntimePayload):
    session_id: NonBlankString
    turn_state: Literal["idle", "listening", "processing", "speaking"]
    turn_id: NonBlankString | None = None
    turn_sequence: PositiveInteger | None = None
    reason: NonBlankString | None = None
    generation_id: NonBlankString | None = None

    @model_validator(mode="after")
    def validate_turn_identity_pair(self) -> "_TurnStatePayload":
        """要求 Turn 的可读身份与数值顺序同时出现。

        Returns:
            Turn 身份字段成对出现或同时省略的合法载荷。
        """

        if (self.turn_id is None) != (self.turn_sequence is None):
            raise ValueError("turn_id 与 turn_sequence 必须同时出现")
        return self


class _SmartTurnStartedPayload(_InputTurnPayload):
    session_id: NonBlankString
    probability: FiniteFloat
    hint_text: NonBlankString


class _SmartTurnEndedPayload(_InputTurnPayload):
    session_id: NonBlankString
    probability: FiniteFloat
    reason: NonBlankString


class _PartialTranscriptPayload(_InputTurnPayload):
    session_id: NonBlankString
    text: NonBlankString


class _FinalTranscriptPayload(_PartialTranscriptPayload):
    request_id: NonBlankString | None = None
    history_disposition: Literal["ordinary_turn", "unattended_episode"]
    episode_id: NonBlankString | None = None

    @model_validator(mode="after")
    def validate_history_disposition(self) -> "_FinalTranscriptPayload":
        """保证临时 episode 转写始终带有可原子提交的片段身份。"""

        if self.history_disposition == "unattended_episode":
            if self.episode_id is None:
                raise ValueError("unattended_episode 必须携带 episode_id")
        elif self.episode_id is not None:
            raise ValueError("ordinary_turn 不能携带 episode_id")
        return self


class _BargeInPayload(_InputRuntimePayload):
    session_id: NonBlankString
    generation_id: NonBlankString | None = None


_PAYLOAD_MODELS: dict[str, type[BaseModel]] = {
    "interaction.state_changed": _InteractionStatePayload,
    "followup.state_changed": _FollowupStatePayload,
    "wake.detected": _WakeDetectedPayload,
    "environment.snapshot_requested": _EnvironmentSnapshotPayload,
    "turn.state_changed": _TurnStatePayload,
    "input.smart_turn_waiting_user_started": _SmartTurnStartedPayload,
    "input.smart_turn_waiting_user_ended": _SmartTurnEndedPayload,
    "user.partial_transcript": _PartialTranscriptPayload,
    "user.final_transcript": _FinalTranscriptPayload,
    "barge_in.detected": _BargeInPayload,
}


class RealtimeInputPublicEventHandler:
    """严格校验输入侧载荷后原样发布，不经过助手字段投影。

    Args:
        publish_interaction: 已绑定 Interaction 公共领域的异步发布端口。
    """

    def __init__(
        self,
        *,
        publish_interaction: Callable[[dict[str, object]], Awaitable[None]],
    ) -> None:
        self._publish_interaction = publish_interaction

    async def handle(self, event: RuntimeEvent) -> None:
        """验证一条明确属于实时输入领域的公共事件。

        Args:
            event: 已经由 Router 确认名称归属的运行时事件。

        Raises:
            ValueError: 事件名称不属于输入领域时抛出。
            pydantic.ValidationError: 公共载荷不符合严格契约时抛出。
        """

        payload_model = _PAYLOAD_MODELS.get(event.type)
        if payload_model is None:
            raise ValueError(f"实时输入公共事件未登记 event_type={event.type}")
        payload_model.model_validate(event.payload)
        await self._publish_interaction(event.to_control_event())
