from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from companion.core.api.routers import realtime
from companion.core.api.schemas import (
    TextTurnContextRequest,
    TextTurnEditRequest,
    TextTurnPreparationFailureRequest,
    TextTurnReservationRequest,
)
from companion.core.application.turn_admission import (
    TurnSubmissionConflict,
    TurnSubmissionNotFound,
    TurnSubmissionQueueFull,
)
from companion.realtime.assistant_output_health import AssistantOutputHealthLedger


SUBMISSION_ID = UUID("6d77fa4e-177c-49b6-a4e3-42df9b46211d")


class RecordingTurnAdmission:
    """记录 Router 传给 Turn 准入层的全部命令。"""

    def __init__(self) -> None:
        """初始化空调用记录和统一返回快照。"""

        self.calls: list[tuple[str, dict[str, object]]] = []
        self.snapshot = {"revision": 1, "items": []}
        self.reserve_error: BaseException | None = None

    async def reserve_text(
        self,
        *,
        submission_id: str,
        message_text: str,
    ) -> dict[str, object]:
        """记录 reservation 命令。

        Args:
            submission_id: Desktop 生成的提交编号。
            message_text: 已由 schema 裁剪的正文。

        Returns:
            测试使用的权威快照。

        Raises:
            BaseException: 测试预置了领域错误时原样抛出。
        """

        self.calls.append(
            (
                "reserve",
                {"submission_id": submission_id, "message_text": message_text},
            )
        )
        if self.reserve_error is not None:
            raise self.reserve_error
        return self.snapshot

    async def commit_context(
        self,
        *,
        submission_id: str,
        environment_snapshot_id: str | None,
    ) -> dict[str, object]:
        """记录环境提交命令。

        Args:
            submission_id: 目标提交编号。
            environment_snapshot_id: 现场环境快照编号。

        Returns:
            测试使用的权威快照。
        """

        self.calls.append(
            (
                "context",
                {
                    "submission_id": submission_id,
                    "environment_snapshot_id": environment_snapshot_id,
                },
            )
        )
        return self.snapshot

    async def edit_text(
        self,
        *,
        submission_id: str,
        message_text: str,
    ) -> dict[str, object]:
        """记录编辑命令。

        Args:
            submission_id: 目标提交编号。
            message_text: 替换后的正文。

        Returns:
            测试使用的权威快照。
        """

        self.calls.append(
            (
                "edit",
                {"submission_id": submission_id, "message_text": message_text},
            )
        )
        return self.snapshot

    async def delete_text(self, submission_id: str) -> dict[str, object]:
        """记录删除命令。

        Args:
            submission_id: 目标提交编号。

        Returns:
            测试使用的权威快照。
        """

        self.calls.append(("delete", {"submission_id": submission_id}))
        return self.snapshot

    async def resume_text(self, submission_id: str) -> dict[str, object]:
        """记录恢复命令。

        Args:
            submission_id: 目标提交编号。

        Returns:
            测试使用的权威快照。
        """

        self.calls.append(("resume", {"submission_id": submission_id}))
        return self.snapshot

    async def retry_text(self, submission_id: str) -> dict[str, object]:
        """记录重试命令。

        Args:
            submission_id: 目标提交编号。

        Returns:
            测试使用的权威快照。
        """

        self.calls.append(("retry", {"submission_id": submission_id}))
        return self.snapshot

    async def fail_preparation(self, *, submission_id: str) -> dict[str, object]:
        """记录环境准备失败命令。

        Args:
            submission_id: 目标提交编号。

        Returns:
            测试使用的权威快照。
        """

        self.calls.append(("preparation_failed", {"submission_id": submission_id}))
        return self.snapshot


def build_container(
    admission: RecordingTurnAdmission,
    *,
    output_enabled: bool = True,
    output_available: bool = True,
    initialized: bool = True,
) -> SimpleNamespace:
    """构造 Router 所需的最小依赖切面。

    Args:
        admission: 记录命令的 Turn 准入端口。
        output_enabled: 是否允许助手输出。
        output_available: 当前媒体会话 Bridge 是否真实可用。
        initialized: Bootstrap 是否已经完成。

    Returns:
        可直接传给路由函数的依赖替身。
    """

    return SimpleNamespace(
        turn_admission=admission,
        assistant_process_supervisor=SimpleNamespace(
            output_enabled=output_enabled,
            owns_process=True,
        ),
        hub=SimpleNamespace(
            bootstrap=SimpleNamespace(
                status_payload=lambda: {"is_initialized": initialized}
            )
        ),
        livekit_media_session_controller=SimpleNamespace(
            assistant_output_available=lambda: output_available,
        ),
        assistant_output_health_ledger=AssistantOutputHealthLedger(),
    )


def test_reservation_schema_rejects_legacy_fields_and_blank_text() -> None:
    """新 reservation 不接纳旧 turn 编号、材料载荷或空正文。"""

    with pytest.raises(ValidationError):
        TextTurnReservationRequest(
            submission_id=SUBMISSION_ID,
            message_text="整理材料",
            turn_id="turn-from-client",  # type: ignore[call-arg]
        )
    with pytest.raises(ValidationError):
        TextTurnReservationRequest(
            submission_id=SUBMISSION_ID,
            message_text="整理材料",
            staged_materials=[],  # type: ignore[call-arg]
        )
    with pytest.raises(ValidationError):
        TextTurnReservationRequest(
            submission_id=SUBMISSION_ID,
            message_text="   ",
        )


def test_legacy_turn_input_route_is_removed() -> None:
    """破坏性迁移后路由表只能暴露新的两阶段提交入口。"""

    route_paths = {route.path for route in realtime.router.routes}

    assert "/api/turn-input" not in route_paths
    assert "/api/turn-submissions" in route_paths
    assert "/api/turn-submissions/{submission_id}/context" in route_paths


@pytest.mark.asyncio
async def test_reservation_endpoint_returns_authoritative_snapshot() -> None:
    """reservation 成功后立即返回 Backend 权威快照，不等待环境捕获。"""

    admission = RecordingTurnAdmission()
    response = await realtime.reserve_text_turn(
        TextTurnReservationRequest(
            submission_id=SUBMISSION_ID,
            message_text="  整理材料  ",
        ),
        build_container(admission),  # type: ignore[arg-type]
    )

    assert response == {
        "accepted": True,
        "turn_submissions": admission.snapshot,
    }
    assert admission.calls == [
        (
            "reserve",
            {
                "submission_id": str(SUBMISSION_ID),
                "message_text": "整理材料",
            },
        )
    ]


@pytest.mark.asyncio
async def test_context_and_queue_commands_keep_submission_identity() -> None:
    """环境、编辑、删除、恢复和重试命令始终使用同一个 submission_id。"""

    admission = RecordingTurnAdmission()
    container = build_container(admission)

    await realtime.commit_text_turn_context(
        SUBMISSION_ID,
        TextTurnContextRequest(environment_snapshot_id="  environment-a  "),
        container,  # type: ignore[arg-type]
    )
    await realtime.edit_text_turn(
        SUBMISSION_ID,
        TextTurnEditRequest(message_text="  新正文  "),
        container,  # type: ignore[arg-type]
    )
    await realtime.delete_text_turn(SUBMISSION_ID, container)  # type: ignore[arg-type]
    await realtime.resume_text_turn(SUBMISSION_ID, container)  # type: ignore[arg-type]
    await realtime.retry_text_turn(SUBMISSION_ID, container)  # type: ignore[arg-type]
    await realtime.fail_text_turn_preparation(
        SUBMISSION_ID,
        TextTurnPreparationFailureRequest(reason="environment_capture_failed"),
        container,  # type: ignore[arg-type]
    )

    submission_id = str(SUBMISSION_ID)
    assert admission.calls == [
        (
            "context",
            {
                "submission_id": submission_id,
                "environment_snapshot_id": "environment-a",
            },
        ),
        ("edit", {"submission_id": submission_id, "message_text": "新正文"}),
        ("delete", {"submission_id": submission_id}),
        ("resume", {"submission_id": submission_id}),
        ("retry", {"submission_id": submission_id}),
        ("preparation_failed", {"submission_id": submission_id}),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("domain_error", "expected_status"),
    [
        (TurnSubmissionNotFound("missing"), 404),
        (TurnSubmissionConflict("conflict"), 409),
        (TurnSubmissionQueueFull("full"), 429),
    ],
)
async def test_reservation_maps_domain_errors_to_http(
    domain_error: BaseException,
    expected_status: int,
) -> None:
    """Router 只把明确领域错误映射成稳定 HTTP 状态。

    Args:
        domain_error: Sequencer 返回的领域错误。
        expected_status: 对外 HTTP 状态码。
    """

    admission = RecordingTurnAdmission()
    admission.reserve_error = domain_error

    with pytest.raises(HTTPException) as raised:
        await realtime.reserve_text_turn(
            TextTurnReservationRequest(
                submission_id=SUBMISSION_ID,
                message_text="整理材料",
            ),
            build_container(admission),  # type: ignore[arg-type]
        )

    assert raised.value.status_code == expected_status


@pytest.mark.asyncio
async def test_reservation_requires_output_and_bootstrap() -> None:
    """输出关闭或 Bootstrap 未完成时不能创建会永远无法派发的 reservation。"""

    admission = RecordingTurnAdmission()
    payload = TextTurnReservationRequest(
        submission_id=SUBMISSION_ID,
        message_text="整理材料",
    )

    with pytest.raises(HTTPException) as output_disabled:
        await realtime.reserve_text_turn(
            payload,
            build_container(admission, output_enabled=False),  # type: ignore[arg-type]
        )
    with pytest.raises(HTTPException) as bootstrap_missing:
        await realtime.reserve_text_turn(
            payload,
            build_container(admission, initialized=False),  # type: ignore[arg-type]
        )

    assert output_disabled.value.status_code == 409
    assert bootstrap_missing.value.status_code == 409
    assert admission.calls == []
