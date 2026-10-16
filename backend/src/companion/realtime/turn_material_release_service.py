"""提供助手请求终态统一释放本轮材料的领域用例。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any


class TurnMaterialReleaseService:
    """校验材料上下文身份，并广播释放后产生的瞬时快照事件。"""

    def __init__(
        self,
        *,
        finish_turn_materials: Callable[[str], list[dict[str, object]]] | None,
        broadcast_ephemeral: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        """初始化材料释放用例。

        Args:
            finish_turn_materials: 按上下文编号结束本轮材料的同步端口。
            broadcast_ephemeral: 广播材料瞬时快照事件的异步端口。
        """

        self._finish_turn_materials = finish_turn_materials
        self._broadcast_ephemeral = broadcast_ephemeral

    async def release_from_payload(self, payload: dict[str, Any]) -> None:
        """释放运行时事件明确绑定的本轮材料上下文。

        Args:
            payload: 包含可选 `turn_material_context_id` 的运行时事件载荷。
        """

        context_id = payload.get("turn_material_context_id")
        if (
            self._finish_turn_materials is None
            or not isinstance(context_id, str)
            or not context_id.strip()
        ):
            return
        for event in self._finish_turn_materials(context_id.strip()):
            await self._broadcast_ephemeral(event)
