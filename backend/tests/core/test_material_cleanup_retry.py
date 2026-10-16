"""任务材料部分释放后失败不能丢失租约，也不能重复释放成功的部分。"""

from unittest.mock import Mock

import pytest

from companion.realtime.task_result_delivery import TaskResultDeliveryCoordinator
from backend.tests.activities.runtime_event_router_test_support import _event


def test_partial_material_release_preserves_remaining_leases_and_events():
    released = {"type": "materials.assets_released", "payload": {"managed_asset_ids": ["asset-a"]}}
    release = Mock(side_effect=[[released], RuntimeError("temporary"), []])
    retain = Mock(return_value=True)
    coordinator = TaskResultDeliveryCoordinator(retain_material=retain, release_material=release)
    acceptance = _event("llm.foreground.tool_call_completed", {
        "name": "start_background_work", "state": "accepted", "deliverable": True,
        "activity_id": "work-a", "request_id": "turn-a", "turn_material_context_id": "context-a",
    })
    assert coordinator.track_tool_acceptance(acceptance).material_retained
    supplemental = _event("llm.foreground.tool_call_completed", {
        "name": "respond_to_work_interaction", "state": "accepted",
        "activity_id": "work-a", "turn_material_context_id": "context-b",
    })
    assert coordinator.track_supplemental_material(supplemental)
    with pytest.raises(RuntimeError, match="temporary"):
        coordinator.interrupt_runtime_epoch(1)
    assert not coordinator.track_tool_acceptance(acceptance).accepted
    assert not coordinator.track_supplemental_material(supplemental)
    assert coordinator.interrupt_runtime_epoch(1) == [released]
    assert [call.args[0] for call in release.call_args_list] == ["context-a", "context-b", "context-b"]
    assert coordinator.interrupt_runtime_epoch(1) == []
    assert retain.call_count == 2


def test_repeated_cleanup_failures_keep_release_events_with_their_epoch():
    """旧世代多次失败时，新世代释放不能取走旧批次尚未返回的事件。"""
    attempts = {}

    def release(context_id):
        """让旧世代的第二项连续失败两次，其余上下文各释放一次。"""
        attempts[context_id] = attempts.get(context_id, 0) + 1
        if context_id == "old-b" and attempts[context_id] <= 2:
            raise RuntimeError("temporary")
        return [{"type": "materials.assets_released", "context_id": context_id}]

    coordinator = TaskResultDeliveryCoordinator(retain_material=lambda _: True, release_material=release)
    for epoch, context in ((1, "old-a"), (1, "old-b"), (2, "new-a")):
        coordinator.track_tool_acceptance(_event("llm.foreground.tool_call_completed", {
            "name": "start_background_work", "state": "accepted", "deliverable": True,
            "activity_id": context, "request_id": "turn-" + context,
            "turn_material_context_id": context,
        }, runtime_epoch=epoch))
    with pytest.raises(RuntimeError, match="temporary"):
        coordinator.interrupt_runtime_epoch(1)
    assert [event["context_id"] for event in coordinator.interrupt_runtime_epoch(2)] == ["new-a"]
    with pytest.raises(RuntimeError, match="temporary"):
        coordinator.interrupt_runtime_epoch(1)
    assert [event["context_id"] for event in coordinator.interrupt_runtime_epoch(1)] == ["old-a", "old-b"]
    assert coordinator.interrupt_runtime_epoch(1) == []
    assert coordinator.interrupt_runtime_epoch(2) == []
    assert attempts == {"old-a": 1, "old-b": 3, "new-a": 1}


@pytest.mark.parametrize("failed_context", ["context-a", "context-b"])
def test_epoch_cleanup_closes_all_claims_and_keeps_events_across_activities(failed_context):
    failures = [failed_context]
    def release(context_id):
        if context_id in failures:
            failures.remove(context_id)
            raise RuntimeError("temporary")
        return [{"type": "materials.assets_released", "context_id": context_id}]
    coordinator = TaskResultDeliveryCoordinator(retain_material=lambda _: True, release_material=release)
    claims = []
    for suffix in ("a", "b"):
        payload = {"activity_id": "work-" + suffix, "request_id": "turn-" + suffix}
        coordinator.track_tool_acceptance(_event("llm.foreground.tool_call_completed", payload | {
            "name": "start_background_work", "state": "accepted", "deliverable": True,
            "turn_material_context_id": "context-" + suffix,
        }))
        coordinator.register_started(_event("background_agent.started", payload))
        coordinator.acknowledge_request(1, payload["request_id"])
        claims.append(coordinator.track_terminal(_event("background_agent.completed", payload)))
    assert all(claims)
    with pytest.raises(RuntimeError, match="temporary"):
        coordinator.interrupt_runtime_epoch(1)
    assert all(not coordinator.is_delivery_current(claim) for claim in claims)
    events = coordinator.interrupt_runtime_epoch(1)
    assert sorted(event["context_id"] for event in events) == ["context-a", "context-b"]
    assert coordinator.interrupt_runtime_epoch(1) == []
