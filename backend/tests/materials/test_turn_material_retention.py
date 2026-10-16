from __future__ import annotations

from companion.materials.models import MaterialAttachment
from companion.materials.turn_context import TurnMaterialContextService


def _managed_material() -> MaterialAttachment:
    """构造带 Desktop 受管资产编号的测试材料。"""

    return MaterialAttachment(
        material_id="material-a",
        turn_id="staged",
        kind="dragged_file",
        source="drag_drop",
        display_name="report.txt",
        path="C:/managed/report.txt",
        created_at="2026-07-14T10:00:00+08:00",
        node_kind="file",
        media_class="text",
        managed_asset_id="asset-a",
    )


def test_retained_turn_releases_managed_asset_only_after_task_result_terminal() -> None:
    """task_result 租约应把受管文件寿命延长到任务终态。"""

    service = TurnMaterialContextService()
    service._staged_materials = [_managed_material()]  # type: ignore[attr-defined]
    context = service.activate_turn(turn_id="turn-a", created_at="2026-07-14T10:00:00+08:00")

    assert service.retain_context(context.context_id) is True
    assert service.finish_turn(context.context_id) == []
    assert service.release_context(context.context_id) == ["asset-a"]


def test_task_result_finishing_before_foreground_final_keeps_release_idempotent() -> None:
    """task_result 先完成时只撤销租约，最终仍由 turn 终态释放一次资产。"""

    service = TurnMaterialContextService()
    service._staged_materials = [_managed_material()]  # type: ignore[attr-defined]
    context = service.activate_turn(turn_id="turn-a", created_at="2026-07-14T10:00:00+08:00")

    assert service.retain_context(context.context_id) is True
    assert service.release_context(context.context_id) == []
    assert service.finish_turn(context.context_id) == ["asset-a"]
