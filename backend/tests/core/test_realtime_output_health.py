from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from companion.core.api.routers.realtime import _require_assistant_output
from companion.realtime.assistant_output_health import AssistantOutputHealthLedger


def _container(
    *,
    available: bool,
    initialized: bool = True,
    owns_process: bool = False,
    ready: bool = True,
) -> SimpleNamespace:
    """构造文本准入健康门禁需要的最小应用依赖。"""

    health = AssistantOutputHealthLedger()
    return SimpleNamespace(
        assistant_process_supervisor=SimpleNamespace(
            output_enabled=True,
            owns_process=owns_process,
            is_ready=ready,
        ),
        hub=SimpleNamespace(
            bootstrap=SimpleNamespace(
                status_payload=lambda: {"is_initialized": initialized},
            )
        ),
        livekit_media_session_controller=SimpleNamespace(
            assistant_output_available=lambda: available,
        ),
        assistant_output_health_ledger=health,
    )


def test_text_reservation_rejects_unavailable_bridge_and_updates_snapshot() -> None:
    """Backend 必须在 reservation 前拒绝真实 Bridge 已不可用的请求。"""

    container = _container(available=False)

    with pytest.raises(HTTPException) as raised:
        _require_assistant_output(container)

    assert raised.value.status_code == 409
    assert container.assistant_output_health_ledger.snapshot_event()["payload"] == {
        "available": False,
        "code": "output_unavailable",
        "message": "助手输出暂时不可用，请重新连接后再试。",
    }


def test_text_reservation_refreshes_available_health_snapshot() -> None:
    """真实 Bridge 恢复后 Backend 准入应同步恢复健康快照。"""

    container = _container(available=True)

    _require_assistant_output(container)

    assert container.assistant_output_health_ledger.snapshot_event()["payload"] == {
        "available": True,
        "code": None,
        "message": None,
    }


def test_bootstrap_gate_runs_before_live_bridge_health_check() -> None:
    """Bootstrap 未完成时不得用尚未创建的媒体会话掩盖真实启动原因。"""

    container = _container(available=False, initialized=False, owns_process=True)

    with pytest.raises(HTTPException) as raised:
        _require_assistant_output(container)

    assert raised.value.detail == "bootstrap is not initialized"


def test_health_changed_at_only_moves_when_state_really_changes() -> None:
    """重复刷新同一健康值不能伪造新的状态变更时间。"""

    timestamps = iter((10.0, 20.0, 30.0))
    ledger = AssistantOutputHealthLedger(clock=lambda: next(timestamps))
    initial = ledger.snapshot()

    repeated_unavailable = ledger.mark_unavailable("output_unavailable")
    available = ledger.mark_available()
    repeated_available = ledger.mark_available()

    assert repeated_unavailable.changed_at == initial.changed_at == 10.0
    assert available.changed_at == 20.0
    assert repeated_available.changed_at == available.changed_at


def test_retired_bridge_cannot_overwrite_new_bridge_health_snapshot() -> None:
    """旧 Bridge 迟到停止时不能把新 Bridge 的恢复状态重新改成不可用。"""

    ledger = AssistantOutputHealthLedger()
    ledger.register_owner("bridge-old")
    ledger.mark_available(owner_id="bridge-old")
    ledger.register_owner("bridge-new")
    ledger.mark_available(owner_id="bridge-new")

    stale_update = ledger.mark_unavailable(
        "output_unavailable",
        owner_id="bridge-old",
    )

    assert stale_update is None
    assert ledger.snapshot().available is True
    assert ledger.snapshot().code is None

    ledger.mark_unavailable("output_unavailable", owner_id="bridge-new")
    assert ledger.snapshot().available is False

    repeated_stale_update = ledger.mark_unavailable(
        "output_unavailable",
        owner_id="bridge-old",
    )
    assert repeated_stale_update is None
    assert ledger.snapshot().available is False


def test_backend_health_gate_preserves_identity_conflict_code() -> None:
    """文本准入刷新失败快照时不能把身份冲突降级成普通不可用。"""

    container = _container(available=False)
    container.assistant_output_health_ledger.register_owner("bridge-current")
    container.assistant_output_health_ledger.mark_unavailable(
        "request_identity_conflict",
        owner_id="bridge-current",
    )

    with pytest.raises(HTTPException):
        _require_assistant_output(container)

    assert (
        container.assistant_output_health_ledger.snapshot().code
        == "request_identity_conflict"
    )


def test_not_ready_runtime_cannot_reuse_stale_identity_conflict_code() -> None:
    """运行时未就绪时必须报告普通不可用，不能沿用旧 Bridge 冲突诊断。"""

    container = _container(available=False, ready=False)
    container.assistant_output_health_ledger.register_owner("bridge-current")
    container.assistant_output_health_ledger.mark_unavailable(
        "request_identity_conflict",
        owner_id="bridge-current",
    )

    with pytest.raises(HTTPException):
        _require_assistant_output(container)

    assert container.assistant_output_health_ledger.snapshot().code == "output_unavailable"
