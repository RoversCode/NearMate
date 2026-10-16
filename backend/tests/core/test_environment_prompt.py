from companion.prompt.capabilities import compile_foreground_capabilities
from companion.prompt.system_state import (
    _render_environment_snapshot_block,
    render_dynamic_environment,
)


def test_environment_prompt_exposes_availability_without_path_or_bytes() -> None:
    """普通 foreground prompt 只能看到环境元数据。"""

    rendered = _render_environment_snapshot_block(
        {
            "environment_snapshot": {
                "captured_at": "2026-07-12T00:00:00+08:00",
                "active_window_title": "Code",
                "screen_available": True,
                "screenshot_bytes": b"secret",
            }
        }
    )

    assert rendered is not None
    assert "screen_available" in rendered
    assert "true" in rendered
    assert "screenshot_path" not in rendered
    assert "secret" not in rendered


def test_dynamic_prompt_omits_legacy_task_state() -> None:
    """动态 Prompt 不再渲染旧派单协议、任务记忆与 pending notice。"""

    rendered = render_dynamic_environment(
        "2026年7月13日",
        {
            "has_material_inputs": True,
            "foreground_text_available": False,
            "task_timeline_memories": [{"goal_summary": "旧任务"}],
            "pending_notices": [{"text": "旧提醒"}],
            "running_task": {"activity_id": "task-old"},
        },
        capability_profile=compile_foreground_capabilities(()),
    )

    assert "start_background_work" not in rendered
    assert "<background_task>" not in rendered
    assert "旧任务" not in rendered
    assert "旧提醒" not in rendered
    assert "task-old" not in rendered
