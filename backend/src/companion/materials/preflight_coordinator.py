from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from threading import Event

from companion.materials.failure_reasons import describe_material_failure
from companion.materials.preflight import inspect_material_batch
from companion.materials.turn_context import TurnMaterialContextService
from companion.observability.logging_config import get_backend_logger


logger = get_backend_logger(__name__)


class MaterialPreflightCoordinator:
    """在有界后台任务中运行 staged 材料预检。"""

    def __init__(
        self,
        *,
        turn_materials: TurnMaterialContextService,
        on_changed: Callable[[list[str], list[dict[str, str]]], Awaitable[None]],
    ) -> None:
        self._turn_materials = turn_materials
        self._on_changed = on_changed
        self._task: asyncio.Task[None] | None = None
        self._cancel_event: Event | None = None
        # generation 批次串行进入同一调度线程，批次内部再共享最多四个深检 worker。
        self._batch_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="material-preflight-batch")

    def schedule(self) -> None:
        """取消旧 generation 并调度当前 staged 快照。"""

        generation, attachments, status = self._turn_materials.staged_preflight_snapshot()
        if self._task is not None:
            self.cancel()
        if not attachments or status != "pending":
            self._task = None
            return
        self._cancel_event = Event()
        self._task = asyncio.create_task(self._run(generation, attachments, self._cancel_event))

    def cancel(self) -> None:
        """取消当前 generation 的等待任务，线程内迟到结果由状态门控丢弃。"""

        if self._task is not None:
            if self._cancel_event is not None:
                self._cancel_event.set()
            self._task.cancel()
            self._task = None
            self._cancel_event = None

    async def _run(self, generation: int, attachments: list, cancel_event: Event) -> None:
        """离开事件循环执行阻塞预检并提交仍有效的结果。"""

        try:
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(
                self._batch_executor,
                inspect_material_batch,
                attachments,
                cancel_event.is_set,
            )
        except asyncio.CancelledError:
            return
        applied = self._turn_materials.apply_preflight_result(generation, result)
        if applied is not None:
            remaining_count = max(0, len(attachments) - len(result.failures))
            for failure in result.failures:
                logger.warning(
                    "材料预检读取失败 material_id=%s display_name=%s reason=%s detail=%s remaining_count=%s",
                    failure.material_id,
                    failure.display_name,
                    failure.reason,
                    describe_material_failure(failure.reason),
                    remaining_count,
                )
            await self._on_changed(*applied)
