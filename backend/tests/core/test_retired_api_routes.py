from __future__ import annotations

import shutil
from pathlib import Path

from fastapi.testclient import TestClient

from companion.core.app import build_app
from companion.core.application.container import AppBuildOptions
from companion.core.settings import Settings


class StubLiveKitMediaSessionController:
    """提供应用组合根要求的最小媒体控制端口。"""

    def enqueue_timed_reminder_notice(self) -> bool:
        """拒绝测试中不会实际交付的独立 Reminder Notice。

        Returns:
            固定返回假，表示测试没有可用媒体会话。
        """

        return False

    def cancel_timed_reminder_requests(self, _request_ids: tuple[str, ...]) -> None:
        """吸收测试中不会产生的提醒请求取消。

        Args:
            _request_ids: 需要停止表达的独立提醒请求编号。
        """

    def interrupt_runtime_activities(
        self,
        _runtime_epoch: int,
    ) -> list[dict[str, object]]:
        """测试不启动实时 agent，因此没有提醒或材料租约需要清理。"""

        return []

    async def restart_current_session(self) -> bool:
        """测试未启动媒体会话，因此没有输入消费者需要重建。"""

        return False


class StubRealtimeInputRuntime:
    """提供组合根身份绑定所需的最小输入 Runtime。"""

    def __init__(self) -> None:
        self.input_runtime_id = ""
        self.input_runtime_epoch = 0

    def bind_runtime_identity(
        self,
        *,
        input_runtime_id: str,
        input_runtime_epoch: int,
    ) -> None:
        """记录 Owner 分配的进程级身份。

        Args:
            input_runtime_id: UUID 输入 Runtime 身份。
            input_runtime_epoch: 进程内单调 epoch。
        """

        self.input_runtime_id = input_runtime_id
        self.input_runtime_epoch = input_runtime_epoch

def test_retired_task_and_permission_routes_return_standard_404(tmp_path: Path) -> None:
    """验证旧任务、权限、worker 与后台材料地址不再注册兼容路由。"""

    character_cards_dir = tmp_path / "character-cards"
    shutil.copytree(Path(__file__).resolve().parents[2] / "data" / "character-cards", character_cards_dir)
    settings = Settings(
        bootstrap_db_path=str(tmp_path / "records.sqlite3"),
        background_agent_usage_db_path=str(tmp_path / "background-agent-usage.sqlite3"),
        user_settings_path=str(tmp_path / "user-settings.json"),
        tool_waiting_policies_path=str(tmp_path / "tool-waiting-policies.json"),
        character_cards_dir=str(character_cards_dir),
        console_dist_dir=str(tmp_path / "missing-console-dist"),
        livekit_api_key="",
        livekit_api_secret="",
    )
    app = build_app(
        AppBuildOptions(
            settings=settings,
            realtime_input_runtime=StubRealtimeInputRuntime(),
            assistant_process_client=object(),
            livekit_media_session_controller=StubLiveKitMediaSessionController(),
        )
    )
    client = TestClient(app)

    health_response = client.get("/healthz")
    assert health_response.status_code == 200
    assert health_response.json() == {"status": "ok", "instance_id": ""}

    retired_requests = (
        ("post", "/api/tasks/task-1/cancel"),
        ("post", "/api/permission-decision"),
        ("post", "/internal/worker-events"),
        ("get", "/api/console/tasks"),
        ("get", "/api/console/permissions"),
        ("get", "/api/console/conversations"),
        ("get", "/api/pending-candidate/current"),
        ("post", "/api/pending-candidate/current/cancel"),
        ("delete", "/api/materials/background/context-1/material-1"),
    )

    for method, path in retired_requests:
        response = client.request(method, path)
        assert response.status_code == 404
        assert response.json() == {"detail": "Not Found"}
