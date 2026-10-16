"""提供运行时公共发布端口的测试构造工具。"""

from __future__ import annotations

from typing import Any

from companion.realtime.assistant_runtime_event_router import AssistantRuntimeEventRouter
from companion.realtime.runtime_event_queue_drainer import RuntimeEventQueueDrainer
from companion.realtime.runtime_public_event_publisher import (
    PublicEventPublish,
    RuntimePublicEventPublishers,
)


def runtime_public_event_publishers(
    publish: PublicEventPublish,
    publish_ephemeral: PublicEventPublish | None = None,
) -> RuntimePublicEventPublishers:
    """把测试记录器显式绑定到五个运行时公共领域端口。

    Args:
        publish: 记录持久公共事件的异步函数。
        publish_ephemeral: 记录瞬时公共事件的异步函数；为空时复用 ``publish``。

    Returns:
        可注入 Router、Drainer 或媒体会话的完整测试端口组。
    """

    ephemeral = publish if publish_ephemeral is None else publish_ephemeral
    return RuntimePublicEventPublishers(
        publish_interaction=publish,
        publish_assistant_output=publish,
        publish_activity=publish,
        publish_activity_ephemeral=ephemeral,
        publish_materials_ephemeral=ephemeral,
    )


def runtime_event_router(
    *,
    publish: PublicEventPublish,
    publish_ephemeral: PublicEventPublish | None = None,
    **dependencies: Any,
) -> AssistantRuntimeEventRouter:
    """使用显式领域端口构造测试 Router。

    Args:
        publish: 记录持久公共事件的异步函数。
        publish_ephemeral: 记录瞬时公共事件的异步函数。
        **dependencies: 当前用例需要注入 Router 的其它领域依赖。

    Returns:
        使用测试发布端口构造的真实 Router。
    """

    return AssistantRuntimeEventRouter(
        public_event_publishers=runtime_public_event_publishers(
            publish,
            publish_ephemeral,
        ),
        **dependencies,
    )


def runtime_event_queue_drainer(
    *,
    publish: PublicEventPublish,
    publish_ephemeral: PublicEventPublish | None = None,
    **dependencies: Any,
) -> RuntimeEventQueueDrainer:
    """使用显式领域端口构造测试 Drainer。

    Args:
        publish: 记录持久公共事件的异步函数。
        publish_ephemeral: 记录瞬时公共事件的异步函数。
        **dependencies: 当前用例需要注入 Drainer 的其它领域依赖。

    Returns:
        使用测试发布端口构造的真实 Drainer。
    """

    return RuntimeEventQueueDrainer(
        public_event_publishers=runtime_public_event_publishers(
            publish,
            publish_ephemeral,
        ),
        **dependencies,
    )
