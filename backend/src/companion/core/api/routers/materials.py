from __future__ import annotations

from fastapi import APIRouter, HTTPException

from companion.core.api.dependencies import MaterialsDependency
from companion.core.api.schemas import TextPasteRequest
from companion.core.local_clock import local_wall_datetime
from companion.materials.text_paste_policy import classify_text_paste
from companion.core.api.schemas import (
    MaterialItemsRequest,
    RecentMaterialForgetRequest,
)


router = APIRouter()


@router.post("/api/materials/text-paste/classify")
async def classify_composer_text_paste(payload: TextPasteRequest) -> dict[str, object]:
    """使用 Backend 唯一 tokenizer 判断粘贴正文是否应转为材料。"""

    return classify_text_paste(payload.text).to_payload()


@router.post("/api/materials/turn/staged", status_code=202)
async def append_staged_turn_materials(
    payload: MaterialItemsRequest,
    container: MaterialsDependency,
) -> dict[str, object]:
    """把显式材料加入下一轮用户 turn，不触发助手回复。"""

    admission = await container.hub.turn_materials.append_staged(
        attachments=[item.model_dump() for item in payload.items],
        created_at=local_wall_datetime().isoformat(),
    )
    material_context = await container.material_lifecycle.broadcast_changed()
    # 深检
    container.material_lifecycle.schedule_staged_preflight()
    return {
        "accepted": True,
        "materials": [item.to_payload() for item in admission.added],
        "admission": admission.to_payload(),
        "material_context": material_context,
    }


@router.delete("/api/materials/turn/staged/{material_id}")
async def delete_staged_turn_material(
    material_id: str,
    container: MaterialsDependency,
) -> dict[str, object]:
    """删除下一轮用户 turn 待用材料。"""

    removal = container.hub.turn_materials.remove_staged(material_id.strip())
    if removal is None:
        raise HTTPException(status_code=404, detail="staged turn material not found")
    material_context = await container.material_lifecycle.broadcast_changed(removal.released_managed_asset_ids)
    container.material_lifecycle.schedule_staged_preflight()
    return {
        "accepted": True,
        "deleted_material_id": material_id.strip(),
        "deleted_material": {
            "material_id": removal.removed.material_id,
            "display_name": removal.removed.display_name,
        },
        "released_managed_asset_ids": removal.released_managed_asset_ids,
        "material_context": material_context,
    }


@router.post("/api/materials/turn/staged/clear", status_code=202)
async def clear_staged_turn_materials(container: MaterialsDependency) -> dict[str, object]:
    """清空下一轮用户 turn 待用材料。"""

    released = container.hub.turn_materials.clear_staged()
    material_context = await container.material_lifecycle.broadcast_changed(released)
    container.material_lifecycle.schedule_staged_preflight()
    return {
        "accepted": True,
        "released_managed_asset_ids": released,
        "material_context": material_context,
    }


@router.get("/api/materials/contexts/current")
async def get_current_material_context(container: MaterialsDependency) -> dict[str, object]:
    """刷新并读取当前 turn 与近期材料的权威上下文。

    Args:
        container: 当前 Backend 组合根提供的材料领域依赖。

    Returns:
        已重新检查近期路径状态且 revision 更高的完整材料快照。
    """

    return await container.material_lifecycle.refresh_material_context()


@router.post("/api/materials/recent/forget")
async def forget_recent_material(
    payload: RecentMaterialForgetRequest,
    container: MaterialsDependency,
) -> dict[str, object]:
    """忘记一个近期路径，并返回完整 revision 材料快照。

    Args:
        payload: Desktop 提交的单一路径忘记请求。
        container: 当前 Backend 组合根提供的材料领域依赖。

    Returns:
        接纳事实和忘记操作后的完整权威材料快照。
    """

    material_context = await container.material_lifecycle.forget_recent_material(
        payload.path
    )
    return {
        "accepted": True,
        "material_context": material_context,
    }


@router.post("/api/materials/contexts/clear", status_code=202)
async def clear_current_material_context(container: MaterialsDependency) -> dict[str, object]:
    """明确清空当前 turn 材料和 NearMate 记住的近期路径。"""

    released = container.material_lifecycle.clear_all_material_contexts()
    return await container.material_lifecycle.broadcast_changed(released)
