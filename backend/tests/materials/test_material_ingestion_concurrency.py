import asyncio
import logging
import time
from pathlib import Path
from threading import Lock
from threading import Event as ThreadEvent
from types import SimpleNamespace

from companion.core.application.assistant_request_context_preparer import AssistantRequestContextPreparer
from companion.materials.models import MaterialAttachment, MaterialWorkloadProfile
from companion.materials.preflight import (
    MaterialPreflightFailure,
    MaterialPreflightResult,
    _ItemFacts,
    inspect_material_batch,
)
from companion.materials.preflight_coordinator import MaterialPreflightCoordinator
from companion.materials.turn_context import TurnMaterialContextService


def test_preflight_uses_at_most_four_workers_and_preserves_result_order(monkeypatch) -> None:
    """验证深检并发上限为四，失败结果仍按提交顺序归并。"""

    active = 0
    maximum = 0
    lock = Lock()

    def inspect(attachment: MaterialAttachment, *, deadline: float, cancelled=None) -> _ItemFacts:
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        try:
            time.sleep(0.02)
            return _ItemFacts(text_bytes=1, total_bytes=1)
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr("companion.materials.preflight._inspect_item", inspect)
    result = inspect_material_batch([_attachment(index) for index in range(5)])

    assert result.status == "ready"
    assert result.profile.local_total_bytes == 5
    assert maximum == 4


async def test_coordinator_keeps_event_loop_responsive(tmp_path: Path, monkeypatch) -> None:
    """验证阻塞预检完整离开事件循环线程。"""

    material_path = tmp_path / "notes.txt"
    material_path.write_text("content", encoding="utf-8")
    service = TurnMaterialContextService()
    await service.append_staged(
        attachments=[{
            "kind": "dragged_file",
            "source": "test",
            "display_name": material_path.name,
            "path": str(material_path),
        }],
        created_at="2026-07-12T00:00:00+08:00",
    )
    original = inspect_material_batch

    def delayed(attachments: list[MaterialAttachment], cancelled=None):
        time.sleep(0.03)
        return original(attachments, cancelled)

    monkeypatch.setattr("companion.materials.preflight_coordinator.inspect_material_batch", delayed)
    changed = asyncio.Event()
    coordinator = MaterialPreflightCoordinator(
        turn_materials=service,
        on_changed=lambda _released, _failures: _set_event(changed),
    )

    coordinator.schedule()
    await asyncio.wait_for(asyncio.sleep(0.005), timeout=0.02)
    await asyncio.wait_for(changed.wait(), timeout=1)

    assert service.staged_preflight_snapshot()[2] == "ready"


async def test_coordinator_logs_applied_read_failure_without_file_path(tmp_path: Path, monkeypatch, caplog) -> None:
    """验证有效预检失败会记录材料名称、稳定原因和剩余数量。"""

    material_path = tmp_path / "broken.png"
    material_path.write_bytes(b"not-an-image")
    service = TurnMaterialContextService()
    admission = await service.append_staged(
        attachments=[{
            "kind": "dragged_file",
            "source": "test",
            "display_name": material_path.name,
            "path": str(material_path),
        }],
        created_at="2026-07-12T00:00:00+08:00",
    )
    failure = MaterialPreflightFailure(admission.added[0].material_id, material_path.name, "invalid_image")
    result = MaterialPreflightResult("ready", _empty_profile(), (failure,))
    monkeypatch.setattr("companion.materials.preflight_coordinator.inspect_material_batch", lambda *_args: result)
    changed = asyncio.Event()
    coordinator = MaterialPreflightCoordinator(
        turn_materials=service,
        on_changed=lambda _released, _failures: _set_event(changed),
    )

    with caplog.at_level(logging.WARNING, logger="companion.materials.preflight_coordinator"):
        coordinator.schedule()
        await asyncio.wait_for(changed.wait(), timeout=1)

    assert (
        f"材料预检读取失败 material_id={failure.material_id} display_name=broken.png "
        "reason=invalid_image detail=图片内容无效 remaining_count=0"
    ) in caplog.text
    assert str(tmp_path) not in caplog.text


async def test_freezing_old_turn_does_not_cancel_next_staged_preflight(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """A 请求冻结上下文时不得取消已经为下一轮 B 启动的预检。"""

    first_path = tmp_path / "first.txt"
    first_path.write_text("first", encoding="utf-8")
    second_path = tmp_path / "second.txt"
    second_path.write_text("second", encoding="utf-8")
    turn_materials = TurnMaterialContextService()
    await turn_materials.append_staged(
        attachments=[{
            "kind": "dragged_file",
            "source": "test",
            "display_name": first_path.name,
            "path": str(first_path),
        }],
        created_at="2026-07-21T00:00:00+08:00",
    )
    context_a = turn_materials.activate_turn(
        turn_id="turn-a",
        created_at="2026-07-21T00:00:01+08:00",
    )
    await turn_materials.append_staged(
        attachments=[{
            "kind": "dragged_file",
            "source": "test",
            "display_name": second_path.name,
            "path": str(second_path),
        }],
        created_at="2026-07-21T00:00:02+08:00",
    )

    started = ThreadEvent()
    release = ThreadEvent()
    changed = asyncio.Event()

    def delayed_preflight(
        _attachments: list[MaterialAttachment],
        _cancelled=None,
    ) -> MaterialPreflightResult:
        """阻塞 B 预检，直到 A 请求已经完成冻结。"""

        started.set()
        release.wait(timeout=1.0)
        return MaterialPreflightResult(
            "ready",
            MaterialWorkloadProfile(True, 1, 0, 6, 6, False, 0, ()),
            (),
        )

    monkeypatch.setattr(
        "companion.materials.preflight_coordinator.inspect_material_batch",
        delayed_preflight,
    )
    coordinator = MaterialPreflightCoordinator(
        turn_materials=turn_materials,
        on_changed=lambda _released, _failures: _set_event(changed),
    )
    coordinator.schedule()
    assert await asyncio.to_thread(started.wait, 1.0) is True

    request_context = AssistantRequestContextPreparer(
            hub=SimpleNamespace(
                turn_materials=turn_materials,
                recent_materials=SimpleNamespace(snapshot=lambda: ()),
                environment=SimpleNamespace(
                claim_snapshot_by_id=lambda _snapshot_id: None,
                current_payload=lambda: None,
            ),
            current_prompt_profile_payload=lambda: {
                "character_card": None,
                "user_call_name": "用户",
                "user_self_intro": "",
                "user_extra_notes": "",
            },
        ),
        realtime_input_runtime_owner=SimpleNamespace(
            current_interaction_state=lambda: SimpleNamespace(
                interaction_session_id="interaction-a"
            )
        ),
    )
    request_context.prepare_notice_context = lambda **_kwargs: {
        "notice_batch": [],
    }
    prepared = request_context.prepare(
        request_id="request-a",
        request_source="text_input",
        turn_material_context_id=context_a.context_id,
    )
    prepared.rollback()
    release.set()
    await asyncio.wait_for(changed.wait(), timeout=1.0)

    _, _, status = turn_materials.staged_preflight_snapshot()
    assert status == "ready"


def _attachment(index: int) -> MaterialAttachment:
    """生成不依赖文件系统的预检引用。"""

    return MaterialAttachment(
        material_id=f"material-{index}",
        turn_id="staged",
        kind="selected_text",
        source="test",
        display_name=f"item-{index}",
        path=f"item-{index}.txt",
        created_at="2026-07-12T00:00:00+08:00",
        node_kind="file",
        media_class="text",
    )


async def _set_event(event: asyncio.Event) -> None:
    """记录协调器已经应用结果。"""

    event.set()


def _empty_profile() -> MaterialWorkloadProfile:
    """生成无可用材料的预检画像。"""

    return MaterialWorkloadProfile(False, 0, 0, 0, 0, False, 0, ())
