"""验证并发布 Self-Reflection 的公共闭合事件。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from companion.realtime.assistant_events import RuntimeEvent


SELF_REFLECTION_PUBLIC_EVENT_TYPES = frozenset(
    {"self_reflection.episode_committed"}
)


def _validate_non_blank_string(value: str) -> str:
    """拒绝公共事件中的空白身份和正文。

    Args:
        value: 已通过严格类型校验的字符串。

    Returns:
        未改写的非空字符串。
    """

    if not value.strip():
        raise ValueError("Self-Reflection 公共字符串字段不能为空")
    return value


NonBlankString = Annotated[str, AfterValidator(_validate_non_blank_string)]
PositiveInteger = Annotated[int, Field(gt=0)]


class _EpisodeHistoryItemPayload(BaseModel):
    """校验 episode 中一条已经闭合的历史消息。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    role: Literal["user", "assistant"]
    content: NonBlankString
    occurred_at: NonBlankString
    episode_role: Literal["input", "interruption_reply", "closure"]
    source_identity: NonBlankString | None
    reply_settlement: Literal["completed", "interrupted", "failed"] | None

    @model_validator(mode="after")
    def validate_role_specific_fields(self) -> "_EpisodeHistoryItemPayload":
        """保证输入、辅助回应和闭合说明各自只携带合法字段。

        Returns:
            角色和结算字段互相匹配的历史项。
        """

        if self.episode_role == "input":
            if self.role != "user" or self.source_identity is None:
                raise ValueError("episode 输入必须是带身份的 user 消息")
            if self.reply_settlement is not None:
                raise ValueError("episode 输入不能携带辅助回应结算")
            return self
        if self.episode_role == "interruption_reply":
            if (
                self.role != "assistant"
                or self.source_identity is None
                or self.reply_settlement is None
            ):
                raise ValueError("辅助回应必须携带 assistant 身份和结算")
            return self
        if (
            self.role != "assistant"
            or self.source_identity is not None
            or self.reply_settlement is not None
        ):
            raise ValueError("episode 闭合说明不能携带来源身份或回应结算")
        return self


class _EpisodeCommittedPayload(BaseModel):
    """校验一次不可执行 episode 的原子历史提交。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    self_reflection_id: NonBlankString
    episode_id: NonBlankString
    runtime_epoch: PositiveInteger
    revision: PositiveInteger
    actionable: Literal[False]
    history_block: list[_EpisodeHistoryItemPayload]


class SelfReflectionPublicEventHandler:
    """把严格验证后的 episode 闭合事实发布到 Interaction 域。

    Args:
        publish_interaction: 已绑定 Interaction 公共领域的异步发布端口。
        acknowledge_publication: Interaction 成功接收后确认生命周期 outbox 的端口。
        fail_publication: Interaction 广播失败后解除 in-flight 状态的端口。
    """

    def __init__(
        self,
        *,
        publish_interaction: Callable[[dict[str, object]], Awaitable[None]],
        acknowledge_publication: Callable[..., bool] | None = None,
        fail_publication: Callable[..., bool] | None = None,
    ) -> None:
        self._publish_interaction = publish_interaction
        self._acknowledge_publication = acknowledge_publication
        self._fail_publication = fail_publication

    async def handle(self, event: RuntimeEvent) -> None:
        """验证并发布一条 Self-Reflection 公共事件。

        Args:
            event: 已由 Router 确认归属的主进程事件。

        Raises:
            ValueError: 事件名称或来源不符合公共契约时抛出。
            pydantic.ValidationError: 载荷字段不符合严格契约时抛出。
        """

        if event.type not in SELF_REFLECTION_PUBLIC_EVENT_TYPES:
            raise ValueError(
                f"Self-Reflection 公共事件未登记 event_type={event.type}"
            )
        if event.source != "main_process" or event.runtime_epoch is not None:
            raise ValueError("episode committed 必须由主进程发布")
        payload = _EpisodeCommittedPayload.model_validate(event.payload)
        try:
            await self._publish_interaction(event.to_control_event())
        except Exception:
            if self._fail_publication is not None:
                self._fail_publication(
                    runtime_epoch=payload.runtime_epoch,
                    episode_id=payload.episode_id,
                )
            raise
        if self._acknowledge_publication is not None:
            self._acknowledge_publication(
                runtime_epoch=payload.runtime_epoch,
                episode_id=payload.episode_id,
            )
