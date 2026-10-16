"""定义运行时 Router 使用的领域绑定公共事件发布端口。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any


PublicEventPublish = Callable[[dict[str, Any]], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class RuntimePublicEventPublishers:
    """保存运行时链路实际需要的五个领域发布端口。

    每个字段都由应用组合根绑定到一个固定的 ``BroadcastHub`` 领域 Publisher。
    Router 和下游处理器只能拿到自己职责对应的字段，不能根据事件名称临时选择领域。

    Args:
        publish_interaction: 发布实时输入和 Turn 互动事件。
        publish_assistant_output: 发布助手正文、失败和播放交付事件。
        publish_activity: 发布需要写入公共历史的 Activity 结果卡事件。
        publish_activity_ephemeral: 发布不写公共历史的 Activity 交互卡事件。
        publish_materials_ephemeral: 发布材料释放后的瞬时权威快照。
    """

    publish_interaction: PublicEventPublish
    publish_assistant_output: PublicEventPublish
    publish_activity: PublicEventPublish
    publish_activity_ephemeral: PublicEventPublish
    publish_materials_ephemeral: PublicEventPublish
