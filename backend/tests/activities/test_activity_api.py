from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from companion.activities.runtime.failure_reporting import failure_from_reason
from companion.activities.runtime.models import (
    ACTIVITY_STAGE_PLAN,
    ActivityCompact,
    ActivityDetail,
    ActivitySnapshot,
    ActivityStagePeriod,
)
from companion.activities.runtime.registry import ActivityNotFound, ActivityStateConflict
from companion.activities.runtime.service import ActivityRuntimeUnavailable
from companion.core.api.dependencies import get_activities_dependencies
from companion.core.api.routers import activities


RUNTIME_ID = "00000000-0000-4000-8000-0000000000cc"


class FakeActivityService:
    """提供 Activity API 状态码测试所需的最小服务替身。"""

    def __init__(
        self,
        *,
        cancel_error: Exception | None = None,
        activity_id: str | None = None,
        with_failure: bool = False,
    ) -> None:
        self.cancel_error = cancel_error
        self.cancelled_ids: list[str] = []
        self.activity_id = activity_id or str(uuid4())
        self.with_failure = with_failure

    def console_snapshot(self) -> ActivitySnapshot:
        """返回一条固定活动列表。"""

        compact = self._compact()
        return ActivitySnapshot(
            revision=1,
            runtime_id=RUNTIME_ID,
            active=(compact,),
            recent=(),
        )

    def detail(self, activity_id: str) -> ActivityDetail:
        """按固定编号返回详情。"""

        if activity_id != self.activity_id:
            raise ActivityNotFound()
        if self.with_failure:
            return self.failed_detail(activity_id)
        return ActivityDetail(
            compact=self._compact(),
            goal="查询天气",
            stage_plan=ACTIVITY_STAGE_PLAN,
            stage_timeline=(
                ActivityStagePeriod(
                    stage_code="preparing",
                    stage_label="准备任务",
                    started_at="2026-07-18T00:00:00+00:00",
                    finished_at=None,
                ),
            ),
            result_text="",
            failure=None,
            failure_reason=None,
        )

    def failed_detail(self, activity_id: str) -> ActivityDetail:
        """按固定编号返回携带故障报告的失败详情。"""

        compact = self._compact()
        return ActivityDetail(
            compact=ActivityCompact(
                activity_id=compact.activity_id,
                kind=compact.kind,
                type_label=compact.type_label,
                status="failed",
                goal_summary=compact.goal_summary,
                failure_summary="模型服务返回了无法正常处理的结果。",
                stage_code="processing",
                stage_label="分析与处理",
                started_at=compact.started_at,
                updated_at=compact.updated_at,
                finished_at="2026-07-18T00:01:00+00:00",
                can_cancel=False,
            ),
            goal="查询天气",
            stage_plan=ACTIVITY_STAGE_PLAN,
            stage_timeline=(),
            result_text="",
            failure=failure_from_reason("empty_model_output", stage_code="processing"),
            failure_reason=None,
        )

    async def cancel(self, activity_id: str) -> None:
        """记录取消或抛出指定领域错误。"""

        if self.cancel_error is not None:
            raise self.cancel_error
        self.cancelled_ids.append(activity_id)

    def _compact(self) -> ActivityCompact:
        """构造固定 Activity 摘要。"""

        return ActivityCompact(
            activity_id=self.activity_id,
            kind="cognitive",
            type_label="事实查询",
            status="running",
            goal_summary="查询天气",
            failure_summary=None,
            stage_code="preparing",
            stage_label="准备任务",
            started_at="2026-07-18T00:00:00+00:00",
            updated_at="2026-07-18T00:00:00+00:00",
            finished_at=None,
            can_cancel=True,
        )


def _client(service: FakeActivityService) -> TestClient:
    """构造只挂载 Activity 路由的测试客户端。"""

    app = FastAPI()
    app.include_router(activities.router)
    app.dependency_overrides[get_activities_dependencies] = lambda: SimpleNamespace(
        activity_service=service
    )
    return TestClient(app)


def test_activity_api_lists_details_and_accepts_cancel() -> None:
    """列表、详情和取消成功路径遵守公共协议。"""

    service = FakeActivityService()
    client = _client(service)

    snapshot = client.get("/api/console/activities").json()
    assert snapshot["revision"] == 1
    assert snapshot["runtime_id"] == RUNTIME_ID
    assert snapshot["active"][0]["goal_summary"] == "查询天气"
    assert snapshot["active"][0]["stage_code"] == "preparing"
    assert snapshot["active"][0]["stage_label"] == "准备任务"

    detail_response = client.get(f"/api/console/activities/{service.activity_id}")
    cancel_response = client.post(f"/api/activities/{service.activity_id}/cancel")

    assert detail_response.status_code == 200
    detail = detail_response.json()
    assert detail["goal"] == "查询天气"
    assert [entry["stage_code"] for entry in detail["stage_plan"]] == [
        "preparing",
        "loading_materials",
        "processing",
        "generating_result",
        "delivering_result",
    ]
    assert [period["stage_code"] for period in detail["stage_timeline"]] == ["preparing"]
    assert detail["failure"] is None
    # 公共协议不再返回 error_text 或旧失败字段。
    assert "error_text" not in detail
    assert "error_text" not in snapshot["active"][0]
    assert cancel_response.status_code == 202
    assert cancel_response.content == b""
    assert service.cancelled_ids == [service.activity_id]


def test_activity_api_failed_detail_carries_structured_failure() -> None:
    """失败详情必须返回结构化故障报告而非纯文本错误。"""

    service = FakeActivityService(with_failure=True)
    client = _client(service)

    response = client.get(f"/api/console/activities/{service.activity_id}")

    assert response.status_code == 200
    detail = response.json()
    failure = detail["failure"]
    assert failure["code"] == "invalid_provider_response"
    assert failure["stage_code"] == "processing"
    assert failure["summary"].strip()
    assert failure["suggested_action"].strip()
    assert detail["status"] == "failed"
    assert "error_text" not in detail


def test_activity_api_maps_not_found_conflict_and_unavailable() -> None:
    """取消领域错误分别映射为 404、409 和 503。"""

    activity_id = str(uuid4())
    cases = (
        (ActivityNotFound(), 404),
        (ActivityStateConflict(), 409),
        (ActivityRuntimeUnavailable(), 503),
    )

    for error, expected_status in cases:
        response = _client(FakeActivityService(cancel_error=error)).post(
            f"/api/activities/{activity_id}/cancel"
        )
        assert response.status_code == expected_status


def test_activity_api_uses_strict_shared_uuid_contract() -> None:
    """HTTP 详情与取消接受 UUID v7，并拒绝非 RFC 变体或无版本 UUID。"""

    uuid_v7 = "01890f3e-7cc2-7d00-8abc-1234567890ab"
    service = FakeActivityService(activity_id=uuid_v7)
    client = _client(service)

    assert client.get(f"/api/console/activities/{uuid_v7}").status_code == 200
    assert client.post(f"/api/activities/{uuid_v7}/cancel").status_code == 202
    for invalid_id in (
        "01890f3e-7cc2-7d00-0abc-1234567890ab",
        "00000000-0000-0000-0000-000000000000",
    ):
        assert client.get(f"/api/console/activities/{invalid_id}").status_code == 422
        assert client.post(f"/api/activities/{invalid_id}/cancel").status_code == 422
