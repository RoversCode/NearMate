from __future__ import annotations

from queue import Queue
from types import SimpleNamespace
from typing import Any, cast

import pytest

from companion.realtime import assistant_process as assistant_process_module
from companion.realtime.assistant_process import AssistantProcessConfig


def test_child_top_level_crash_logs_traceback_and_sends_sanitized_internal_fact(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """子进程入口异常必须写完整日志，同时只回传异常类型和阶段。

    Args:
        monkeypatch: 替换真实运行时装配，避免测试启动模型和音频 provider。
        caplog: 收集日志，确认完整异常仍只存在本地诊断侧。

    Returns:
        None: 通过异常、队列载荷和日志内容验证崩溃边界。
    """

    event_queue: Queue[Any] = Queue()
    bridge_work_event = SimpleNamespace(set=lambda: None)
    stop_event = SimpleNamespace(is_set=lambda: False)
    crash_message = "内部配置不应进入公共事件"

    monkeypatch.setattr(
        assistant_process_module,
        "_bootstrap_assistant_process_runtime",
        lambda: None,
    )
    monkeypatch.setattr(
        assistant_process_module,
        "_build_assistant_child_runtime",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError(crash_message)
        ),
    )
    monkeypatch.setattr(
        assistant_process_module,
        "shutdown_backend_logging",
        lambda: None,
    )

    with caplog.at_level("ERROR", logger="companion.realtime.assistant_process"):
        with pytest.raises(RuntimeError, match=crash_message):
            assistant_process_module.run_assistant_process(
                stop_event,
                bridge_work_event,
                cast(Any, Queue()),
                event_queue,
                cast(Any, Queue()),
                cast(Any, SimpleNamespace(set=lambda: None)),
                cast(AssistantProcessConfig, None),
            )

    crash_event = event_queue.get_nowait()
    assert crash_event.type == "runtime.internal_crash"
    assert crash_event.payload == {
        "exception_type": "RuntimeError",
        "stage": "build_child_runtime",
    }
    assert crash_message in caplog.text
    assert "Traceback" in caplog.text
    assert crash_message not in str(crash_event.payload)
