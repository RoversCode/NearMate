import asyncio
from collections.abc import Awaitable, Callable
from types import SimpleNamespace

from companion.core.application.material_lifecycle import MaterialLifecycleService
from companion.realtime.assistant_events import RecentMaterialObservation


class MaterialLifecycleHubStub:
    """提供材料生命周期测试真正依赖的领域发布端口。

    Args:
        publish_ephemeral: 每次瞬时材料事件发布时执行的测试行为。
    """

    def __init__(
        self,
        publish_ephemeral: Callable[[dict[str, object]], Awaitable[None]],
    ) -> None:
        self.material_revision = 0
        self.turn_materials = SimpleNamespace(
            staged_preflight_snapshot=lambda: (0, [], "closed")
        )
        self._publish_ephemeral = publish_ephemeral

    def publisher(self, domain: str) -> "MaterialLifecycleHubStub":
        """返回测试使用的材料瞬时事件发布端口。

        Args:
            domain: 生命周期服务申请的公共事件领域。

        Returns:
            当前 Hub；它同时实现 publish_ephemeral 方法。
        """

        assert domain == "materials"
        return self

    async def publish_ephemeral(self, event: dict[str, object]) -> None:
        """执行当前用例配置的材料事件发布行为。

        Args:
            event: 准备公开的材料瞬时事件。
        """

        await self._publish_ephemeral(event)

    def current_material_context_payload(self) -> dict[str, object]:
        """返回与当前 revision 对应的最小材料快照。

        Returns:
            不包含 staged 或 active 材料的权威快照。
        """

        return {
            "revision": self.material_revision,
            "turn_material_context": {"staged_materials": [], "active": None},
        }


async def test_mutation_snapshot_does_not_wait_for_slow_websocket() -> None:
    """验证慢连接不会占用材料 mutation 的 HTTP 响应热路径。"""

    release_broadcast = asyncio.Event()
    events: list[dict[str, object]] = []

    async def broadcast_ephemeral(event: dict[str, object]) -> None:
        await release_broadcast.wait()
        events.append(event)

    hub = MaterialLifecycleHubStub(broadcast_ephemeral)
    lifecycle = MaterialLifecycleService(hub=hub)

    snapshot = await asyncio.wait_for(lifecycle.broadcast_changed(), timeout=0.05)

    assert snapshot["revision"] == 1
    assert events == []
    release_broadcast.set()
    await lifecycle._broadcast_tail  # type: ignore[attr-defined]
    assert events[0]["type"] == "materials.contexts_changed"


async def test_explicit_refresh_records_new_revision_for_live_path_status() -> None:
    """显式刷新必须登记新 revision，让 Desktop 能应用重新检查后的路径状态。"""

    events: list[dict[str, object]] = []

    async def broadcast_ephemeral(event: dict[str, object]) -> None:
        """记录刷新事务发布的权威快照。"""

        events.append(event)

    hub = MaterialLifecycleHubStub(broadcast_ephemeral)
    lifecycle = MaterialLifecycleService(hub=hub)

    snapshot = await lifecycle.refresh_material_context()
    await lifecycle._broadcast_tail  # type: ignore[attr-defined]

    assert snapshot["revision"] == 1
    assert events == [
        {
            "type": "materials.contexts_changed",
            "payload": snapshot,
        }
    ]


async def test_preflight_failure_event_exposes_only_stable_user_safe_fields() -> None:
    """验证预检失败事件不携带本地路径、中文日志或异常正文。"""

    events: list[dict[str, object]] = []

    async def broadcast_ephemeral(event: dict[str, object]) -> None:
        events.append(event)

    hub = MaterialLifecycleHubStub(broadcast_ephemeral)
    lifecycle = MaterialLifecycleService(hub=hub)

    await lifecycle._on_preflight_changed([], [{
        "material_id": "material-1",
        "display_name": "损坏.pdf",
        "reason": "invalid_or_encrypted_pdf",
    }])
    await lifecycle._broadcast_tail  # type: ignore[attr-defined]

    assert events[0] == {
        "type": "materials.preflight_failed",
        "payload": {
            "items": [{
                "material_id": "material-1",
                "display_name": "损坏.pdf",
                "reason": "invalid_or_encrypted_pdf",
            }],
        },
    }
    assert "path" not in str(events[0]).lower()


def test_finish_turn_returns_release_then_revisioned_context_events() -> None:
    """验证 turn 终态清理返回资产释放和带 revision 的权威快照。"""

    turn_materials = SimpleNamespace(
        staged_preflight_snapshot=lambda: (0, [], "closed"),
        finish_turn=lambda context_id: ["asset-1"] if context_id == "context-1" else [],
    )
    hub = SimpleNamespace(
        material_revision=4,
        turn_materials=turn_materials,
        current_material_context_payload=lambda: {
            "revision": hub.material_revision,
            "turn_material_context": {"staged_materials": [], "active": None},
        },
    )
    lifecycle = MaterialLifecycleService(hub=hub)

    events = lifecycle.finish_turn("context-1")

    assert hub.material_revision == 5
    assert events == [
        {
            "type": "materials.assets_released",
            "payload": {"managed_asset_ids": ["asset-1"]},
        },
        {
            "type": "materials.contexts_changed",
            "payload": {
                "revision": 5,
                "turn_material_context": {"staged_materials": [], "active": None},
            },
        },
    ]

def test_finish_turn_still_projects_context_when_no_assets_are_released() -> None:
    """验证无受管资产时仍投影 turn 已结束的最新材料上下文。"""

    hub = SimpleNamespace(
        material_revision=2,
        turn_materials=SimpleNamespace(
            staged_preflight_snapshot=lambda: (0, [], "closed"),
            finish_turn=lambda _context_id: [],
        ),
        current_material_context_payload=lambda: {
            "revision": hub.material_revision,
            "turn_material_context": {"staged_materials": [], "active": None},
        },
    )
    lifecycle = MaterialLifecycleService(hub=hub)

    events = lifecycle.finish_turn("context-without-managed-assets")

    assert events == [
        {
            "type": "materials.contexts_changed",
            "payload": {
                "revision": 3,
                "turn_material_context": {"staged_materials": [], "active": None},
            },
        }
    ]


def test_explicit_material_context_clear_also_forgets_recent_paths() -> None:
    """用户明确清空材料上下文时，应同时撤销未来新任务的近期路径能力。"""

    calls: list[str] = []
    hub = SimpleNamespace(
        turn_materials=SimpleNamespace(
            staged_preflight_snapshot=lambda: (0, [], "closed"),
            clear_all=lambda: calls.append("turn:clear") or ["asset-a"],
        ),
        recent_materials=SimpleNamespace(
            clear=lambda: calls.append("recent:clear") or True,
        ),
    )
    lifecycle = MaterialLifecycleService(hub=hub)

    released = lifecycle.clear_all_material_contexts()

    assert released == ["asset-a"]
    assert calls == ["turn:clear", "recent:clear"]


async def test_accepted_recent_material_observation_touches_and_broadcasts() -> None:
    """成功重挂应刷新现存路径的 LRU，并发布新的 Desktop 快照 revision。"""

    events: list[dict[str, object]] = []
    touched_paths: list[list[str]] = []

    async def broadcast_ephemeral(event: dict[str, object]) -> None:
        """记录材料快照广播。"""

        events.append(event)

    hub = MaterialLifecycleHubStub(broadcast_ephemeral)
    hub.recent_materials = SimpleNamespace(
        touch=lambda paths: touched_paths.append(paths) or True,
    )
    lifecycle = MaterialLifecycleService(hub=hub)

    snapshot = await lifecycle.observe_recent_materials(
        RecentMaterialObservation(
            outcome="accepted",
            paths=(r"C:\project\app.py",),
        )
    )
    await lifecycle._broadcast_tail  # type: ignore[attr-defined]

    assert touched_paths == [[r"C:\project\app.py"]]
    assert snapshot is not None
    assert snapshot["revision"] == 1
    assert events[-1]["type"] == "materials.contexts_changed"


async def test_late_recent_material_touch_does_not_recreate_forgotten_entry() -> None:
    """忘记后到达的 accepted 事实没有现存项可 touch，也不能制造新 revision。"""

    events: list[dict[str, object]] = []

    async def broadcast_ephemeral(event: dict[str, object]) -> None:
        """记录任何不应发生的材料广播。"""

        events.append(event)

    hub = MaterialLifecycleHubStub(broadcast_ephemeral)
    hub.recent_materials = SimpleNamespace(touch=lambda _paths: False)
    lifecycle = MaterialLifecycleService(hub=hub)

    snapshot = await lifecycle.observe_recent_materials(
        RecentMaterialObservation(
            outcome="accepted",
            paths=(r"C:\project\forgotten.py",),
        )
    )

    assert snapshot is None
    assert hub.material_revision == 0
    assert events == []


async def test_failed_recent_material_validation_refreshes_status_snapshot() -> None:
    """机械校验失败不改 LRU，但必须让 Desktop 重新读取当前路径状态。"""

    events: list[dict[str, object]] = []

    async def broadcast_ephemeral(event: dict[str, object]) -> None:
        """记录失败观察触发的状态快照。"""

        events.append(event)

    def fail_if_touched(_paths: list[str]) -> bool:
        """证明失败观察不会误改 LRU 顺序。"""

        raise AssertionError("失败观察不应刷新 LRU")

    hub = MaterialLifecycleHubStub(broadcast_ephemeral)
    hub.recent_materials = SimpleNamespace(touch=fail_if_touched)
    lifecycle = MaterialLifecycleService(hub=hub)

    snapshot = await lifecycle.observe_recent_materials(
        RecentMaterialObservation(outcome="validation_failed")
    )
    await lifecycle._broadcast_tail  # type: ignore[attr-defined]

    assert snapshot is not None
    assert snapshot["revision"] == 1
    assert events[-1]["type"] == "materials.contexts_changed"


async def test_forget_recent_material_returns_revisioned_snapshot_once() -> None:
    """手动忘记只在首次删除时递增 revision，重复命令返回当前权威快照。"""

    events: list[dict[str, object]] = []
    forgotten_paths: list[str] = []

    async def broadcast_ephemeral(event: dict[str, object]) -> None:
        """记录首次忘记触发的材料快照广播。"""

        events.append(event)

    hub = MaterialLifecycleHubStub(broadcast_ephemeral)

    def forget(path: str) -> bool:
        """只让同一路径第一次忘记成功。"""

        if path in forgotten_paths:
            return False
        forgotten_paths.append(path)
        return True

    hub.recent_materials = SimpleNamespace(forget=forget)
    lifecycle = MaterialLifecycleService(hub=hub)

    first = await lifecycle.forget_recent_material(r"C:\project\app.py")
    await lifecycle._broadcast_tail  # type: ignore[attr-defined]
    second = await lifecycle.forget_recent_material(r"C:\project\app.py")

    assert first["revision"] == 1
    assert second["revision"] == 1
    assert forgotten_paths == [r"C:\project\app.py"]
    assert [event["type"] for event in events] == ["materials.contexts_changed"]
