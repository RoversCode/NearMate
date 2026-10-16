from __future__ import annotations

from companion.records.runtime_log import RuntimeLogBuffer


def test_runtime_log_buffer_evicts_oldest_and_keeps_structured_payload() -> None:
    """缓冲超过容量时淘汰最旧记录，payload 始终保持结构化。"""

    buffer = RuntimeLogBuffer(capacity=3)
    source_payload = {"nested": {"value": 1}}
    buffer.append(event_type="event.1", payload=source_payload)
    source_payload["nested"]["value"] = 99  # type: ignore[index]
    buffer.append(event_type="event.2", payload={"value": 2})
    buffer.append(event_type="event.3", payload={"value": 3})
    buffer.append(event_type="event.4", payload={"value": 4})

    rows = buffer.list_rows(500)

    assert [row.sequence for row in rows] == [4, 3, 2]
    assert [row.event_type for row in rows] == ["event.4", "event.3", "event.2"]
    assert all(isinstance(row.payload, dict) for row in rows)


def test_runtime_log_buffers_start_empty_and_do_not_share_process_state() -> None:
    """新缓冲实例代表新进程运行，不继承上一实例的任何日志。"""

    previous_run = RuntimeLogBuffer()
    previous_run.append(event_type="old.event", payload={"secret": "old"})

    current_run = RuntimeLogBuffer()

    assert current_run.list_rows() == []
    current_run.append(event_type="new.event", payload={"ok": True})
    assert current_run.list_rows()[0].to_payload()["payload"] == {"ok": True}
