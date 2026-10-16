"""助手语音播放交付的 HTTP 契约。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator


class PlayoutDeliveryAckRequest(BaseModel):
    """描述 Desktop 对一次语音交付的真实播放结果。

    Args:
        delivery_id: Backend 首帧发布事件携带的交付编号。
        request_id: 关联的助手请求编号。
        generation_id: 关联的 generation 编号。
        client_instance_id: 发送回执的 Desktop 实例编号。
        outcome: 已开始播放或播放失败。
        failure_code: 播放失败时的稳定错误码。
        failure_message: 播放失败时的用户可理解说明。
    """

    model_config = ConfigDict(extra="forbid")

    delivery_id: str
    request_id: str
    generation_id: str
    client_instance_id: str
    outcome: Literal["playing", "failed"]
    failure_code: str | None = None
    failure_message: str | None = None

    @field_validator(
        "delivery_id",
        "request_id",
        "generation_id",
        "client_instance_id",
        "failure_code",
        "failure_message",
    )
    @classmethod
    def trim_non_empty_strings(cls, value: str | None) -> str | None:
        """裁剪字符串，并拒绝提交空的身份或失败说明。"""

        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("字段不能是空字符串")
        return normalized

