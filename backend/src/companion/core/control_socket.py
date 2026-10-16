from __future__ import annotations

from collections.abc import Callable

from companion.core.broadcast import BroadcastHub
from companion.activities.runtime.models import ActivitySnapshot
from companion.realtime.interaction_state import InteractionState
from companion.timed_reminders.models import TimedReminderSnapshot


def build_control_socket_initial_events(
    hub: BroadcastHub,
    *,
    interaction_state: InteractionState,
    activity_snapshot: ActivitySnapshot,
    timed_reminder_snapshot: TimedReminderSnapshot,
    assistant_output_status_snapshot: Callable[[], dict[str, object]],
    input_runtime_snapshot: Callable[[], dict[str, object] | None] | None = None,
    interaction_state_snapshot: Callable[[], dict[str, object] | None] | None = None,
    pending_interactions_snapshot: Callable[[], dict[str, object]] | None = None,
    result_card_events: Callable[[], list[dict[str, object]]] | None = None,
    turn_submissions_snapshot: Callable[[], dict[str, object]] | None = None,
    application_status_events: Callable[[], list[dict[str, object]]] | None = None,
    scope: str = "runtime",
) -> list[dict]:
    """构建控制 WebSocket 新连接建立时需要补发的初始快照事件。

    Args:
        hub: 提供设置公开快照的广播中枢。
        interaction_state: 当前追问热区状态。
        activity_snapshot: 当前 Activity 权威快照。
        timed_reminder_snapshot: 当前定时提醒权威快照。
        input_runtime_snapshot: 当前输入 Runtime 身份快照读取函数。
        interaction_state_snapshot: 当前完整交互状态读取函数，不携带历史按键反馈来源。
        pending_interactions_snapshot: 可选的待答卡完整快照读取函数。
        result_card_events: 可选的结果卡快照读取函数。
        turn_submissions_snapshot: 可选的待发文本完整快照读取函数。

    Returns:
        按首帧顺序排列的控制事件列表。
    """

    if scope == "status":
        return application_status_events() if application_status_events is not None else []

    # 追问热区状态是控制 socket 首帧契约，新连接必须知道当前是否需要重新唤醒。
    followup_state_event = {
        "type": "followup.state_changed",
        "payload": {
            "followup_state": interaction_state.followup_state,
        },
    }
    # settings.changed 让 panel/avatar 在 backend 重连后重新拿到最新持久化设置，而不是继续停留在旧内存快照或默认值。
    settings_event = {
        "type": "settings.changed",
        "payload": hub.current_settings_payload(),
    }
    activity_event = {
        "type": "activities.snapshot_changed",
        "payload": activity_snapshot.to_payload(),
    }
    timed_reminder_event = {
        "type": "reminders.snapshot_changed",
        "payload": timed_reminder_snapshot.to_payload(),
    }
    initial_events = [
        *(application_status_events() if application_status_events is not None else []),
        followup_state_event,
        *(
            [input_runtime_event]
            if input_runtime_snapshot is not None
            and (input_runtime_event := input_runtime_snapshot()) is not None
            else []
        ),
        *(
            [interaction_state_event]
            if interaction_state_snapshot is not None
            and (interaction_state_event := interaction_state_snapshot()) is not None
            else []
        ),
        settings_event,
        assistant_output_status_snapshot(),
        activity_event,
        timed_reminder_event,
    ]
    if turn_submissions_snapshot is not None:
        initial_events.append(turn_submissions_snapshot())
    if pending_interactions_snapshot is not None:
        initial_events.append(pending_interactions_snapshot())
    if result_card_events is not None:
        initial_events.extend(result_card_events())
    return initial_events
