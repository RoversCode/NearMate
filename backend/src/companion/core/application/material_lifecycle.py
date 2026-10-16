from __future__ import annotations

import asyncio
from collections.abc import Iterable

from companion.core.broadcast import BroadcastHub
from companion.materials.models import MaterialAttachment
from companion.materials.preflight_coordinator import MaterialPreflightCoordinator
from companion.observability.logging_config import get_backend_logger
from companion.realtime.assistant_events import RecentMaterialObservation


logger = get_backend_logger(__name__)


class MaterialLifecycleService:
    """协调材料上下文变更、资产释放与控制事件顺序。

    Args:
        hub: 持有本轮材料上下文及瞬时广播能力的控制中心。
    """

    def __init__(self, *, hub: BroadcastHub) -> None:
        self.hub = hub
        self.preflight = MaterialPreflightCoordinator(
            turn_materials=hub.turn_materials,
            on_changed=self._on_preflight_changed,
        )
        self._broadcast_tail: asyncio.Task[None] | None = None

    def schedule_staged_preflight(self) -> None:
        """为当前 staged generation 调度异步产物预检。"""

        self.preflight.schedule()

    def remember_recent_materials(
        self,
        attachments: Iterable[MaterialAttachment],
    ) -> bool:
        """登记已经随正式用户输入成立的近期材料。

        登记失败不能推翻已经公开或写入 Episode 的用户输入，因此这里统一吞掉内部
        异常并留下诊断。调用方只需要在返回真时登记新的材料快照 revision。

        Args:
            attachments: 与已成立用户输入绑定的完整材料附件。

        Returns:
            至少一项用户自持路径被新增或刷新时返回真。
        """

        try:
            return self.hub.recent_materials.remember(attachments)
        except Exception:
            logger.exception("近期材料登记失败，已成立的用户输入保持有效")
            return False

    async def observe_recent_materials(
        self,
        observation: RecentMaterialObservation,
    ) -> dict[str, object] | None:
        """应用 Background 重挂后的 LRU 和 Desktop 状态刷新事实。

        Args:
            observation: Router 已解析并剥离私有路径的近期材料观察。

        Returns:
            发生公共快照刷新时返回新 revision；没有现存项可刷新时返回空。
        """

        if observation.outcome == "accepted":
            try:
                changed = self.hub.recent_materials.touch(
                    list(observation.paths)
                )
            except Exception:
                # LRU 只是未来重挂体验，不能反向推翻已经启动的 Background Activity。
                logger.exception("近期材料使用顺序刷新失败，已启动任务保持有效")
                return None
            if not changed:
                # 忘记操作可能早于迟到的 accepted 事实；这里绝不重新创建已忘记条目。
                return None
        return await self.broadcast_changed()

    async def _on_preflight_changed(
        self,
        released_managed_asset_ids: list[str],
        failures: list[dict[str, str]],
    ) -> None:
        """按资产释放、失败反馈和权威快照顺序发布预检结果。

        Args:
            released_managed_asset_ids: 本次预检后不再被引用的受管资产编号。
            failures: 需要反馈给桌面端的材料预检失败项。
        """

        if failures:
            await self.hub.publisher("materials").publish_ephemeral(
                {"type": "materials.preflight_failed", "payload": {"items": failures}}
            )
        await self.broadcast_changed(released_managed_asset_ids)

    async def broadcast_changed(
        self,
        released_managed_asset_ids: list[str] | None = None,
    ) -> dict[str, object]:
        """广播当前材料上下文快照。

        Args:
            released_managed_asset_ids: 状态变化后可由 Desktop 删除的项目副本编号。

        Returns:
            已登记 revision 且已进入串行广播队列的最新材料上下文快照。
        """

        payload, events = self._record_changed_events(released_managed_asset_ids)
        previous = self._broadcast_tail

        async def publish() -> None:
            """串行发布资产释放和对应 revision，避免慢连接阻塞 mutation 响应。"""

            if previous is not None:
                await previous
            try:
                for event in events:
                    await self.hub.publisher("materials").publish_ephemeral(event)
            except Exception as exception:
                # 单个广播异常不得打断更高 revision 的串行投影。
                logger.warning("材料快照广播失败 revision=%s error=%s", payload["revision"], exception)

        self._broadcast_tail = asyncio.create_task(publish())
        return payload

    async def refresh_material_context(self) -> dict[str, object]:
        """重新检查近期路径状态，并发布一个可被 Desktop 接纳的新版本。

        Desktop 只接受更高 revision。显式刷新必须先登记新版本，否则文件在磁盘上
        发生变化但注册表没有 mutation 时，同 revision 的新状态会被当成重复响应丢弃。

        Returns:
            已重新投影路径状态并登记更高 revision 的完整材料快照。
        """

        return await self.broadcast_changed()

    def finish_turn(self, context_id: str) -> list[dict[str, object]]:
        """结束指定 turn 并生成有序的材料瞬时事件。

        Args:
            context_id: 助手终态携带的本轮材料上下文编号。

        Returns:
            按资产释放、权威上下文快照顺序排列的瞬时事件。
        """

        released_managed_asset_ids = self.hub.turn_materials.finish_turn(context_id)
        _, events = self._record_changed_events(released_managed_asset_ids)
        return events

    def clear_all_material_contexts(self) -> list[str]:
        """清空本轮材料生命周期和未来可重挂的近期路径。

        Returns:
            所有不再被 staged 或活动上下文引用的受管资产编号。
        """

        released_managed_asset_ids = self.hub.turn_materials.clear_all()
        self.hub.recent_materials.clear()
        return released_managed_asset_ids

    async def forget_recent_material(self, path: str) -> dict[str, object]:
        """撤销一项未来 Background 新任务可用的近期路径能力。

        Args:
            path: Desktop 当前快照中选择忘记的完整本地路径。

        Returns:
            条目存在时返回更高 revision 快照；已被其它操作忘记时返回当前快照。
        """

        if not self.hub.recent_materials.forget(path):
            return self.hub.current_material_context_payload()
        return await self.broadcast_changed()

    def retain_turn(self, context_id: str) -> bool:
        """为异步任务取得指定 turn 的材料租约。

        Args:
            context_id: 异步任务准备读取的材料上下文编号。

        Returns:
            成功取得材料租约时返回真。
        """

        return self.hub.turn_materials.retain_context(context_id)

    def release_turn(self, context_id: str) -> list[dict[str, object]]:
        """释放异步任务持有的 turn 材料租约。

        Args:
            context_id: 已经完成读取的材料上下文编号。

        Returns:
            最后一个材料所有者退出时产生的资产释放与权威快照事件。
        """

        released_managed_asset_ids = self.hub.turn_materials.release_context(context_id)
        if not released_managed_asset_ids:
            return []
        _, events = self._record_changed_events(released_managed_asset_ids)
        return events

    def _record_changed_events(
        self,
        released_managed_asset_ids: list[str] | None = None,
    ) -> tuple[dict[str, object], list[dict[str, object]]]:
        """登记新 revision 并构造同版本的材料事件。

        Args:
            released_managed_asset_ids: 新快照不再引用的受管资产编号。

        Returns:
            权威上下文快照及其对应的有序瞬时事件。
        """

        self.hub.material_revision += 1
        payload = self.hub.current_material_context_payload()
        events: list[dict[str, object]] = []
        if released_managed_asset_ids:
            events.append(
                {
                    "type": "materials.assets_released",
                    "payload": {"managed_asset_ids": released_managed_asset_ids},
                }
            )
        events.append(
            {
                "type": "materials.contexts_changed",
                "payload": payload,
            }
        )
        return payload, events
