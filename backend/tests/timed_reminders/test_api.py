from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from companion.core.api.dependencies import get_timed_reminders_dependencies
from companion.core.api.routers import timed_reminders
from companion.timed_reminders.repository import TimedReminderRepository
from companion.timed_reminders.service import TimedReminderService


def _client() -> tuple[TestClient, TimedReminderService]:
    """构造只挂载提醒路由且时钟固定的测试客户端。

    Returns:
        可直接发起同步 HTTP 请求的客户端。
    """

    async def ignore_broadcast(_event: dict[str, object]) -> None:
        return None

    service = TimedReminderService(
        repository=TimedReminderRepository(
            runtime_id="00000000-0000-4000-8000-0000000000ee",
        ),
        broadcast_snapshot=ignore_broadcast,
        now=lambda: datetime(2026, 9, 3, 6, 0, tzinfo=UTC),
    )
    app = FastAPI()
    app.include_router(timed_reminders.router)
    app.dependency_overrides[get_timed_reminders_dependencies] = lambda: SimpleNamespace(
        timed_reminder_service=service
    )
    return TestClient(app), service


def test_api_creates_lists_updates_and_cancels_reminder() -> None:
    """提醒公共 API 应返回 canonical 事实并完成完整手动管理流程。"""

    client, _service = _client()
    create_response = client.post(
        "/api/timed-reminders",
        json={"content": "准备面试", "delay_seconds": 1800},
    )

    assert create_response.status_code == 201
    created = create_response.json()
    reminder_id = created["reminder"]["reminder_id"]
    assert created["runtime_only"] is True
    assert created["reminder"]["trigger_at"] == "2026-09-03T06:30:00+00:00"
    assert client.get("/api/timed-reminders").json()["active"][0]["content"] == "准备面试"

    update_response = client.patch(
        f"/api/timed-reminders/{reminder_id}",
        json={"content": "进入面试", "delay_seconds": 3600},
    )
    assert update_response.status_code == 200
    assert update_response.json()["reminder"]["content"] == "进入面试"
    assert update_response.json()["reminder"]["trigger_at"] == "2026-09-03T07:00:00+00:00"

    cancel_response = client.delete(f"/api/timed-reminders/{reminder_id}")
    assert cancel_response.status_code == 204
    snapshot = client.get("/api/timed-reminders").json()
    assert snapshot["active"] == []
    assert snapshot["recent"][0]["state"] == "cancelled"


def test_api_rejects_invalid_duplicate_missing_and_terminal_updates() -> None:
    """HTTP 层应稳定映射输入错误、精确重复、不存在和终态冲突。"""

    client, _service = _client()
    invalid = client.post(
        "/api/timed-reminders",
        json={"content": "空时间"},
    )
    assert invalid.status_code == 422

    payload = {"content": "喝水", "delay_seconds": 60}
    first = client.post("/api/timed-reminders", json=payload)
    duplicate = client.post("/api/timed-reminders", json=payload)
    assert duplicate.status_code == 409

    reminder_id = first.json()["reminder"]["reminder_id"]
    assert client.delete(f"/api/timed-reminders/{reminder_id}").status_code == 204
    assert client.patch(
        f"/api/timed-reminders/{reminder_id}",
        json={"content": "改名"},
    ).status_code == 409
    assert client.delete("/api/timed-reminders/missing").status_code == 404


def test_api_dismisses_visible_ringing_batch_and_rejects_stale_runtime() -> None:
    """批次关闭接口必须幂等，并拒绝旧运行期误关当前任务。"""

    client, service = _client()
    created = client.post(
        "/api/timed-reminders",
        json={"content": "准备出门", "delay_seconds": 1},
    ).json()["reminder"]
    service._repository.activate_due(  # noqa: SLF001 - 路由测试显式推进权威时钟状态
        now=datetime(2026, 9, 3, 6, 0, 1, tzinfo=UTC)
    )

    stale = client.post(
        "/api/timed-reminders/dismiss",
        json={"runtime_id": "old-runtime", "reminder_ids": [created["reminder_id"]]},
    )
    assert stale.status_code == 409

    payload = {
        "runtime_id": "00000000-0000-4000-8000-0000000000ee",
        "reminder_ids": [created["reminder_id"]],
    }
    dismissed = client.post("/api/timed-reminders/dismiss", json=payload)
    assert dismissed.status_code == 200
    assert dismissed.json()["dismissed_ids"] == [created["reminder_id"]]
    assert client.post("/api/timed-reminders/dismiss", json=payload).json()["dismissed_ids"] == []
    assert client.get("/api/timed-reminders").json()["recent"][0]["state"] == "dismissed"
