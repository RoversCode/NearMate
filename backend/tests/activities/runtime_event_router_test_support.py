"""提供运行时事件 Router 测试共用的窄构造函数。"""

from __future__ import annotations

from companion.realtime.assistant_events import RuntimeEvent
from companion.runtime_memory.models import MemoryCommit
from companion.runtime_memory.store import RuntimeMemoryStore


RUNTIME_EPOCH = 1
RUNTIME_ID = "00000000-0000-4000-8000-0000000000bb"


async def _noop_broadcast(_event: dict[str, object]) -> None:
    """提供不产生副作用的公共广播端口。

    Args:
        _event: Router 尝试公开广播的事件。
    """


def _event(
    event_type: str,
    payload: dict[str, object],
    runtime_epoch: int = RUNTIME_EPOCH,
) -> RuntimeEvent:
    """构造材料分发测试使用的运行时事件。

    Args:
        event_type: 助手运行时事件类型。
        payload: 事件携带的业务数据。
        runtime_epoch: 产生事件的助手运行代际。

    Returns:
        带稳定测试身份的运行时事件。
    """

    normalized_payload = dict(payload)
    if event_type in {
        "assistant.text_chunk",
        "assistant.text_segment_final",
        "assistant.text_final",
        "assistant.audio_finished",
    }:
        normalized_payload.setdefault("session_id", "session-test")
        normalized_payload.setdefault("generation_id", "generation-test")

    return RuntimeEvent(
        type=event_type,
        source="assistant_runtime",
        payload=normalized_payload,
        produced_at=10.0,
        runtime_epoch=runtime_epoch,
    )


def _append_runtime_memory_messages(store: RuntimeMemoryStore, *, count: int) -> None:
    """追加足以触发测试压缩计划的权威消息。

    Args:
        store: 接收测试消息的主进程权威运行期记忆。
        count: 需要追加的消息数量。
    """

    for index in range(count):
        store.apply_memory_commit(MemoryCommit(
            commit_id=f"router-memory-{index}",
            request_id=f"router-request-{index}",
            base_memory_epoch=store.memory_epoch,
            messages=({
                "role": "assistant",
                "content": f"用于压缩的消息 {index} " + ("较长内容" * 1000),
                "metadata": {"origin_key": f"router-origin-{index}"},
            },),
        ))

async def _append_async(target: list[str], value: str) -> None:
    """向时序列表追加异步端口标记。

    Args:
        target: 记录调用顺序的列表。
        value: 本次调用对应的时序标记。
    """

    target.append(value)
