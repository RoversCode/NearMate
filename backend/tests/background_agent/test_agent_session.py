from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from claude_agent_sdk import ResultMessage

from companion.background_agent.agent_session import AgentSession, AgentSessionManager
from companion.background_agent.contracts import AgentMaterial
from companion.background_agent.interaction_bridge import InteractionBridge
from companion.background_agent.permission_gate import ActivityPermissionGate
from companion.background_agent.prompt_audit import BackgroundPromptAuditDispatcher
from companion.observability.prompt_audit import PromptAuditRecorder


class _Client:
    created = 0

    def __init__(self, _options: object) -> None:
        type(self).created += 1
        self.prompts: list[str] = []
        self.disconnected = False

    async def connect(self) -> None:
        return None

    async def query(self, prompt: str) -> None:
        self.prompts.append(prompt)

    async def receive_response(self):
        yield _result_message()

    async def interrupt(self) -> None:
        return None

    async def disconnect(self) -> None:
        self.disconnected = True

    def rewind_files(self, _user_message_uuid: str) -> None:
        return None


def test_new_turn_rebuilds_transport_and_resumes_sdk_session(tmp_path: Path) -> None:
    """同一 AgentSession 切换 Turn 时重建回调 transport，并用 SDK 编号恢复上下文。"""

    async def run() -> None:
        created_clients: list[_Client] = []
        created_options: list[object] = []
        dispatched_queries: list[object] = []

        def create_client(options: object) -> _Client:
            created_options.append(options)
            client = _Client(options)
            created_clients.append(client)
            return client

        async def dispatch_query(client: _Client, dispatch: object) -> None:
            """保存 Session 在最终提交边界使用的真实请求现场。"""

            dispatched_queries.append(dispatch)
            await client.query(getattr(dispatch, "prompt"))

        session = AgentSession(
            workspace_root=tmp_path,
            client_factory=create_client,
            query_dispatcher=dispatch_query,
        )
        first_turn = _bind_turn(session, "activity-a")
        await session.ensure_connected(
            SimpleNamespace(resume=None),
            policy="new_session",
            turn_token=first_turn.turn_token,
        )
        await session.query("第一次目标", turn_token=first_turn.turn_token)
        async for _ in session.receive_response(turn_token=first_turn.turn_token):
            pass
        session.sdk_session_id = "sdk-session-a"

        second_turn = _bind_turn(session, "activity-b")
        await session.ensure_connected(
            SimpleNamespace(resume=None),
            policy="resume_required",
            turn_token=second_turn.turn_token,
        )
        await session.query("纠正后的第二次目标", turn_token=second_turn.turn_token)
        async for _ in session.receive_response(turn_token=second_turn.turn_token):
            pass

        assert len(created_clients) == 2
        assert created_clients[0].prompts == ["第一次目标"]
        assert created_clients[0].disconnected is True
        assert created_clients[1].prompts == ["纠正后的第二次目标"]
        assert getattr(created_options[1], "resume", None) == "sdk-session-a"
        second_dispatch = dispatched_queries[1]
        assert getattr(second_dispatch, "activity_id") == "activity-b"
        assert getattr(second_dispatch, "prompt") == "纠正后的第二次目标"
        assert getattr(second_dispatch, "options") is created_options[1]
        assert getattr(getattr(second_dispatch, "options"), "resume") == "sdk-session-a"
        assert session.health == "ready"
        await session.disconnect()
        assert created_clients[1].disconnected is True

    asyncio.run(run())


def test_disconnect_turn_transport_preserves_resumable_session(tmp_path: Path) -> None:
    """Activity 清理只断开 transport，产品 Session 仍能用 SDK 编号恢复。"""

    async def run() -> None:
        """执行一次完整 Turn 清理，再用同一产品 Session 恢复连接。"""

        created_clients: list[_Client] = []
        created_options: list[object] = []

        def create_client(options: object) -> _Client:
            """记录每次恢复实际创建的 SDK Client 和 options。"""

            created_options.append(options)
            client = _Client(options)
            created_clients.append(client)
            return client

        session = AgentSession(
            workspace_root=tmp_path,
            client_factory=create_client,
        )
        first_turn = _bind_turn(session, "activity-a")
        await session.ensure_connected(
            SimpleNamespace(resume=None),
            policy="new_session",
            turn_token=first_turn.turn_token,
        )
        session.sdk_session_id = "sdk-session-a"
        session.retire_turn(first_turn.turn_token)

        await session.disconnect_turn_transport()

        assert created_clients[0].disconnected is True
        assert session.client is None
        assert session.health == "ready"
        assert session.resume_identity() is not None

        second_turn = _bind_turn(session, "activity-b")
        await session.ensure_connected(
            SimpleNamespace(resume=None),
            policy="resume_required",
            turn_token=second_turn.turn_token,
        )

        assert len(created_clients) == 2
        assert getattr(created_options[1], "resume", None) == "sdk-session-a"
        await session.disconnect()

    asyncio.run(run())


def test_manager_rejects_workspace_mismatch(tmp_path: Path) -> None:
    manager = AgentSessionManager(client_factory=lambda _options: _Client(None))
    session = manager.get_or_create(workspace_root=tmp_path)
    try:
        manager.get_or_create(
            workspace_root=tmp_path / "other",
            agent_session_id=session.agent_session_id,
            policy="resume_required",
        )
    except RuntimeError as error:
        assert "工作区" in str(error)
    else:
        raise AssertionError("工作区不匹配必须 fail closed")


def test_manager_timeout_does_not_cancel_real_session_disconnect(tmp_path: Path) -> None:
    """Session 断开超时只结束等待，不能取消仍在执行的真实清理。

    Args:
        tmp_path: pytest 提供的隔离工作区路径。

    Returns:
        无；断开前后的 Session 状态由断言验证。
    """

    class BlockingClient(_Client):
        """让断开操作等待测试显式放行。"""

        def __init__(self) -> None:
            super().__init__(None)
            self.disconnect_started = asyncio.Event()
            self.allow_disconnect = asyncio.Event()
            self.disconnect_finished = asyncio.Event()

        async def disconnect(self) -> None:
            self.disconnect_started.set()
            await self.allow_disconnect.wait()
            self.disconnect_finished.set()

    async def run() -> None:
        client = BlockingClient()
        session = AgentSession(
            workspace_root=tmp_path,
            client_factory=lambda _options: client,
        )
        session._client = client
        session.sdk_session_id = "sdk-session-a"
        session.health = "ready"
        manager = AgentSessionManager(client_factory=lambda _options: client)
        with manager._lock:  # noqa: SLF001
            manager._sessions[session.agent_session_id] = session  # noqa: SLF001

        await manager.disconnect_all(timeout=0.01)

        assert client.disconnect_started.is_set() is True
        assert client.disconnect_finished.is_set() is False
        assert session.health == "ready"
        assert session.client is client

        client.allow_disconnect.set()
        await asyncio.wait_for(client.disconnect_finished.wait(), timeout=1.0)
        await asyncio.sleep(0)
        assert session.health == "closed"
        assert session.client is None

    asyncio.run(run())


def test_manager_starts_disconnect_for_every_session_after_budget_expires(
    tmp_path: Path,
) -> None:
    """关闭预算耗尽后，后续 Session 仍必须启动真实断开任务。

    Args:
        tmp_path: pytest 提供的隔离工作区路径。

    Returns:
        无；两个 Session 的断开开始与完成事实由断言验证。
    """

    class BlockingClient(_Client):
        """让多个 Session 的断开都等待统一放行。"""

        def __init__(self) -> None:
            super().__init__(None)
            self.disconnect_started = asyncio.Event()
            self.allow_disconnect = asyncio.Event()
            self.disconnect_finished = asyncio.Event()

        async def disconnect(self) -> None:
            self.disconnect_started.set()
            await self.allow_disconnect.wait()
            self.disconnect_finished.set()

    async def run() -> None:
        clients = [BlockingClient(), BlockingClient()]
        sessions = [
            AgentSession(
                workspace_root=tmp_path / f"workspace-{index}",
                client_factory=lambda _options, client=client: client,
            )
            for index, client in enumerate(clients)
        ]
        for session, client in zip(sessions, clients, strict=True):
            session._client = client
            session.sdk_session_id = f"sdk-session-{len(session.agent_session_id)}"
            session.health = "ready"
        manager = AgentSessionManager(client_factory=lambda _options: clients[0])
        with manager._lock:  # noqa: SLF001
            manager._sessions.update(  # noqa: SLF001
                {session.agent_session_id: session for session in sessions}
            )

        await manager.disconnect_all(timeout=0.0)
        await asyncio.sleep(0)

        assert all(client.disconnect_started.is_set() for client in clients)
        assert all(session.health == "ready" for session in sessions)

        for client in clients:
            client.allow_disconnect.set()
        await asyncio.gather(
            *(client.disconnect_finished.wait() for client in clients)
        )
        await asyncio.sleep(0)
        assert all(session.health == "closed" for session in sessions)
        assert all(session.client is None for session in sessions)

    asyncio.run(run())


def test_manager_rejects_closed_session_for_required_resume(tmp_path: Path) -> None:
    """已经关闭的产品 Session 不能靠相同编号伪装成恢复成功。"""

    async def run() -> None:
        manager = AgentSessionManager(client_factory=lambda _options: _Client(None))
        original = manager.get_or_create(workspace_root=tmp_path)
        original.sdk_session_id = "sdk-session-a"
        await original.disconnect()

        with pytest.raises(RuntimeError, match="已关闭"):
            manager.get_or_create(
                workspace_root=tmp_path,
                agent_session_id=original.agent_session_id,
                policy="resume_required",
            )

    asyncio.run(run())


def test_required_resume_does_not_fall_back_when_sdk_session_cannot_connect(
    tmp_path: Path,
) -> None:
    """本地 SDK 会话文件失效时必须失败，不能退回无上下文的新会话。"""

    class ConnectClient(_Client):
        """按 options.resume 决定是否模拟连接失败。"""

        def __init__(self, options: object) -> None:
            super().__init__(options)
            self.options = options

        async def connect(self) -> None:
            if getattr(self.options, "resume", None):
                raise RuntimeError("session file missing")

    async def run() -> None:
        created_clients: list[ConnectClient] = []

        def create_client(options: object) -> ConnectClient:
            client = ConnectClient(options)
            created_clients.append(client)
            return client

        old_client = ConnectClient(SimpleNamespace(resume=None))
        session = AgentSession(workspace_root=tmp_path, client_factory=create_client)
        session._client = old_client
        session.sdk_session_id = "sdk-session-a"
        session.health = "ready"

        with pytest.raises(RuntimeError, match="session file missing"):
            await session.ensure_connected(
                SimpleNamespace(resume=None),
                policy="resume_required",
            )

        assert old_client.disconnected is True
        assert len(created_clients) == 1
        assert getattr(created_clients[0].options, "resume", None) == "sdk-session-a"
        assert created_clients[0].disconnected is True
        assert session.health == "broken"

    asyncio.run(run())


def test_required_resume_fails_closed_for_broken_transport(tmp_path: Path) -> None:
    """强制复用策略不能在 transport 损坏时悄悄创建新会话。"""

    async def run() -> None:
        client = _Client(None)
        session = AgentSession(
            workspace_root=tmp_path,
            client_factory=lambda _options: client,
        )
        session._client = client
        session.sdk_session_id = "sdk-session-a"
        session.health = "broken"

        with pytest.raises(RuntimeError, match="transport"):
            await session.ensure_connected(object(), policy="resume_required")

        assert client.disconnected is False

    asyncio.run(run())


def test_required_resume_fails_when_resume_cannot_be_applied(tmp_path: Path) -> None:
    """强制复用不能因 options 不可写而悄悄退化成全新 SDK 会话。"""

    class FrozenOptions:
        """模拟无法设置 resume 的冻结 SDK options。"""

        __slots__ = ("model",)

        def __init__(self) -> None:
            object.__setattr__(self, "model", "claude-sonnet")

        def __setattr__(self, _name: str, _value: object) -> None:
            raise TypeError("options are frozen")

    async def run() -> None:
        created_clients: list[_Client] = []

        def create_client(options: object) -> _Client:
            """记录是否错误创建了不带 resume 的 Client。"""

            client = _Client(options)
            created_clients.append(client)
            return client

        session = AgentSession(workspace_root=tmp_path, client_factory=create_client)
        session.sdk_session_id = "sdk-session-a"

        with pytest.raises(RuntimeError, match="resume"):
            await session.ensure_connected(
                FrozenOptions(),
                policy="resume_required",
            )

        assert created_clients == []
        assert session.health == "new"

    asyncio.run(run())


def test_interrupt_does_not_release_query_slot_before_response_stream_closes(
    tmp_path: Path,
) -> None:
    """旧 SDK 流尚未收口时，新的 Activity 不能并发 query。"""

    class BlockingClient(_Client):
        """允许测试分别控制 interrupt 和 response stream 的结束时机。"""

        def __init__(self) -> None:
            super().__init__(None)
            self.release_response = asyncio.Event()
            self.interrupt_called = False

        async def receive_response(self):
            await self.release_response.wait()
            if False:
                yield None

        async def interrupt(self) -> None:
            self.interrupt_called = True

    async def run() -> None:
        client = BlockingClient()
        session = AgentSession(
            workspace_root=tmp_path,
            client_factory=lambda _options: client,
        )
        await session.ensure_connected(object())
        await session.query("原始目标")
        response_task = asyncio.create_task(_drain(session))
        await asyncio.sleep(0)
        duplicate_reader = asyncio.create_task(_drain(session))
        with pytest.raises(RuntimeError, match="response reader"):
            await duplicate_reader

        assert await session.interrupt() is True
        assert client.interrupt_called is True
        with pytest.raises(RuntimeError, match="未收口"):
            await session.query("纠正目标")

        client.release_response.set()
        await response_task
        assert session.health == "ready"

    asyncio.run(run())


def test_changed_execution_options_recreate_client_and_resume_sdk_session(
    tmp_path: Path,
) -> None:
    """模型或 max_turns 变化后不能误用旧 Client，应以 SDK session_id 恢复上下文。"""

    async def run() -> None:
        created_clients: list[_Client] = []
        created_options: list[object] = []

        def factory(options: object) -> _Client:
            created_options.append(options)
            client = _Client(options)
            created_clients.append(client)
            return client

        session = AgentSession(workspace_root=tmp_path, client_factory=factory)
        first_options = SimpleNamespace(
            model="claude-sonnet",
            permission_mode="acceptEdits",
            max_turns=20,
            resume=None,
            can_use_tool=lambda: None,
        )
        first_client = await session.ensure_connected(first_options)
        session.sdk_session_id = "sdk-session-a"
        second_options = SimpleNamespace(
            model="claude-opus",
            permission_mode="acceptEdits",
            max_turns=30,
            resume=None,
            can_use_tool=lambda: None,
        )

        second_client = await session.ensure_connected(
            second_options,
            policy="resume_required",
        )

        assert second_client is not first_client
        assert first_client.disconnected is True
        assert len(created_clients) == 2
        assert getattr(created_options[1], "resume", None) == "sdk-session-a"
        assert session.sdk_session_id == "sdk-session-a"

    asyncio.run(run())


def test_empty_sdk_response_marks_session_broken(tmp_path: Path) -> None:
    """CLI 没有返回任何 SDK 消息时，Session 不能继续标记为健康。"""

    class EmptyClient(_Client):
        async def receive_response(self):
            if False:
                yield None

    async def run() -> None:
        client = EmptyClient(None)
        session = AgentSession(
            workspace_root=tmp_path,
            client_factory=lambda _options: client,
        )
        await session.ensure_connected(SimpleNamespace(model="claude-sonnet"))
        await session.query("任务目标")
        async for _ in session.receive_response():
            pass
        assert session.health == "broken"

    asyncio.run(run())


def test_nonterminal_sdk_response_marks_session_broken(tmp_path: Path) -> None:
    """只有过程消息却没有 ResultMessage 时，不能把异常 EOF 当成健康收口。"""

    class NonterminalClient(_Client):
        async def receive_response(self):
            yield SimpleNamespace(kind="assistant")

    async def run() -> None:
        client = NonterminalClient(None)
        session = AgentSession(
            workspace_root=tmp_path,
            client_factory=lambda _options: client,
        )
        await session.ensure_connected(SimpleNamespace(model="claude-sonnet"))
        await session.query("任务目标")
        async for _ in session.receive_response():
            pass
        assert session.health == "broken"

    asyncio.run(run())


def test_closing_session_response_closes_inner_sdk_reader_synchronously(
    tmp_path: Path,
) -> None:
    """外层 reader 提前关闭时，SDK 内层 reader 的 finally 必须先完成。"""

    class ReaderLifecycleClient(_Client):
        """记录 SDK response reader 是否已经执行 finally。"""

        def __init__(self) -> None:
            super().__init__(None)
            self.reader_closed = asyncio.Event()

        async def receive_response(self):
            """先返回终态，再等待调用方显式关闭内层生成器。"""

            try:
                yield _result_message()
                await asyncio.Future()
            finally:
                self.reader_closed.set()

    async def run() -> None:
        client = ReaderLifecycleClient()
        session = AgentSession(
            workspace_root=tmp_path,
            client_factory=lambda _options: client,
        )
        turn = _bind_turn(session, "activity-a")
        await session.ensure_connected(object(), turn_token=turn.turn_token)
        await session.query("任务目标", turn_token=turn.turn_token)
        response_stream = session.receive_response(turn_token=turn.turn_token)

        assert isinstance(await anext(response_stream), ResultMessage)
        await response_stream.aclose()

        assert client.reader_closed.is_set() is True
        assert session.callback_turn(turn.turn_token) is None
        assert session.health == "ready"

    asyncio.run(run())


def test_interrupt_waits_until_query_submission_finishes(tmp_path: Path) -> None:
    """query 仍在向 transport 写入时，interrupt 不能并发进入同一 Client。"""

    class BlockingQueryClient(_Client):
        def __init__(self) -> None:
            super().__init__(None)
            self.query_entered = asyncio.Event()
            self.release_query = asyncio.Event()
            self.interrupt_entered = asyncio.Event()

        async def query(self, prompt: str) -> None:
            self.query_entered.set()
            await self.release_query.wait()
            await super().query(prompt)

        async def interrupt(self) -> None:
            self.interrupt_entered.set()

    async def run() -> None:
        client = BlockingQueryClient()
        recorder = PromptAuditRecorder(
            enabled=True,
            output_dir=tmp_path / "prompt-audits",
            max_runs=5,
            process_role="assistant-runtime",
        )
        session = AgentSession(
            workspace_root=tmp_path,
            client_factory=lambda _options: client,
            query_dispatcher=BackgroundPromptAuditDispatcher(recorder),
        )
        await session.ensure_connected(SimpleNamespace(model="claude-sonnet"))
        query_task = asyncio.create_task(session.query("原始目标"))
        await client.query_entered.wait()
        interrupt_task = asyncio.create_task(session.interrupt())
        await asyncio.sleep(0)

        # 审计已在 transport 写入前成为提交事实，interrupt 仍等待同一控制锁。
        assert len(list((tmp_path / "prompt-audits").rglob("*.md"))) == 1
        assert client.interrupt_entered.is_set() is False
        client.release_query.set()
        await query_task
        assert await interrupt_task is True
        assert client.interrupt_entered.is_set() is True
        async for _ in session.receive_response():
            pass

    asyncio.run(run())


def test_disconnect_waits_until_interrupt_finishes(tmp_path: Path) -> None:
    """disconnect 必须等待正在提交的 interrupt，避免两个控制调用并发写 transport。"""

    class BlockingInterruptClient(_Client):
        def __init__(self) -> None:
            super().__init__(None)
            self.interrupt_entered = asyncio.Event()
            self.release_interrupt = asyncio.Event()
            self.disconnect_entered = asyncio.Event()

        async def interrupt(self) -> None:
            self.interrupt_entered.set()
            await self.release_interrupt.wait()

        async def disconnect(self) -> None:
            self.disconnect_entered.set()
            await super().disconnect()

    async def run() -> None:
        client = BlockingInterruptClient()
        session = AgentSession(
            workspace_root=tmp_path,
            client_factory=lambda _options: client,
        )
        await session.ensure_connected(object())
        await session.query("原始目标")
        interrupt_task = asyncio.create_task(session.interrupt())
        await client.interrupt_entered.wait()
        disconnect_task = asyncio.create_task(session.disconnect())
        await asyncio.sleep(0)

        assert client.disconnect_entered.is_set() is False
        client.release_interrupt.set()
        assert await interrupt_task is True
        await disconnect_task
        assert client.disconnect_entered.is_set() is True
        assert session.health == "closed"

    asyncio.run(run())


def test_response_reader_cannot_overwrite_disconnected_health(tmp_path: Path) -> None:
    """disconnect 导致 reader 退出时，迟到异常不能把 closed 会话改回 broken。"""

    class DisconnectingReaderClient(_Client):
        def __init__(self) -> None:
            super().__init__(None)
            self.reader_entered = asyncio.Event()
            self.release_reader = asyncio.Event()

        async def receive_response(self):
            self.reader_entered.set()
            await self.release_reader.wait()
            raise RuntimeError("transport closed")
            yield  # pragma: no cover

        async def disconnect(self) -> None:
            self.release_reader.set()
            await super().disconnect()

    async def run() -> None:
        client = DisconnectingReaderClient()
        session = AgentSession(
            workspace_root=tmp_path,
            client_factory=lambda _options: client,
        )
        await session.ensure_connected(object())
        await session.query("原始目标")
        reader_task = asyncio.create_task(_drain(session))
        await client.reader_entered.wait()

        await session.disconnect()
        with pytest.raises(RuntimeError, match="transport closed"):
            await reader_task
        assert session.health == "closed"

    asyncio.run(run())


def test_interrupt_cannot_revive_broken_session(tmp_path: Path) -> None:
    """query 已经写入失败后，后续 interrupt 不能把死 transport 重新标为健康。"""

    class QueryFailureClient(_Client):
        async def query(self, _prompt: str) -> None:
            raise RuntimeError("query failed")

    async def run() -> None:
        client = QueryFailureClient(None)
        session = AgentSession(
            workspace_root=tmp_path,
            client_factory=lambda _options: client,
        )
        await session.ensure_connected(object())
        with pytest.raises(RuntimeError, match="query failed"):
            await session.query("原始目标")
        assert session.health == "broken"

        assert await session.interrupt() is True
        assert session.health == "broken"

    asyncio.run(run())


def test_cancelled_query_releases_response_slot_and_marks_session_broken(
    tmp_path: Path,
) -> None:
    """SDK query 自身取消后不能让 Session 永久残留 busy 响应槽。"""

    class CancelledQueryClient(_Client):
        """模拟 transport 在 query 提交阶段返回协程取消。"""

        async def query(self, _prompt: str) -> None:
            """抛出 SDK 原始取消，不伪装成普通运行时错误。"""

            raise asyncio.CancelledError("sdk query cancelled")

    async def run() -> None:
        client = CancelledQueryClient(None)
        recorder = PromptAuditRecorder(
            enabled=True,
            output_dir=tmp_path / "prompt-audits",
            max_runs=5,
            process_role="assistant-runtime",
        )
        session = AgentSession(
            workspace_root=tmp_path,
            client_factory=lambda _options: client,
            query_dispatcher=BackgroundPromptAuditDispatcher(recorder),
        )
        await session.ensure_connected(SimpleNamespace(model="claude-sonnet"))

        with pytest.raises(asyncio.CancelledError, match="sdk query cancelled"):
            await session.query("任务目标")

        assert session.health == "broken"
        assert session._response_open is False
        assert len(list((tmp_path / "prompt-audits").rglob("*.md"))) == 1
        with pytest.raises(RuntimeError, match="尚未连接"):
            await session.query("不应复用损坏 transport")
        await session.disconnect()

    asyncio.run(run())


def test_prequery_interrupt_prevents_current_turn_query(tmp_path: Path) -> None:
    """当前 Turn 在 query 前已取消时，之后到达的 query 不能继续提交给 Client。"""

    async def run() -> None:
        client = _Client(None)
        recorder = PromptAuditRecorder(
            enabled=True,
            output_dir=tmp_path / "prompt-audits",
            max_runs=5,
            process_role="assistant-runtime",
        )

        session = AgentSession(
            workspace_root=tmp_path,
            client_factory=lambda _options: client,
            query_dispatcher=BackgroundPromptAuditDispatcher(recorder),
        )
        bridge = InteractionBridge(
            activity_id="activity-a",
            request_notifier=lambda _request: None,
        )
        turn_capability = session.bind_turn(
            activity_id="activity-a",
            bridge=bridge,
            permission_gate=ActivityPermissionGate(
                interaction_bridge=bridge,
                resolution_notifier=lambda _request, _outcome, _suppress_notice: None,
            ),
            material_catalog=(),
        )
        await session.ensure_connected(
            object(),
            turn_token=turn_capability.turn_token,
        )

        assert await session.interrupt(activity_id="activity-a") is True
        with pytest.raises(RuntimeError, match="query 前取消"):
            await session.query(
                "不应执行的目标",
                turn_token=turn_capability.turn_token,
            )
        assert client.prompts == []
        assert not (tmp_path / "prompt-audits").exists()

    asyncio.run(run())


def test_late_sdk_callbacks_from_retired_turn_are_cancelled(tmp_path: Path) -> None:
    """旧 Turn 首次迟到的新工具编号也不能获得新 Activity 的权限。"""

    async def run() -> None:
        session = AgentSession(workspace_root=tmp_path, client_factory=lambda _options: _Client(None))
        old_turn = _bind_turn(session, "activity-old")
        new_turn = _bind_turn(session, "activity-new")
        permission_answer = await session.request_permission(
            tool_name="Bash",
            input_data={},
            action_summary="执行命令",
            turn_token=old_turn.turn_token,
        )
        question_answer = await session.request_interaction(
            tool_name="AskUserQuestion",
            input_data={"questions": []},
            questions=[],
            turn_token=old_turn.turn_token,
        )

        assert permission_answer.outcome == "cancel"
        assert question_answer.outcome == "cancel"
        assert session.callback_turn(old_turn.turn_token) is None
        assert session.callback_turn(new_turn.turn_token) is new_turn

        missing_token_answer = await session.request_permission(
            tool_name="Bash",
            input_data={},
            action_summary="执行命令",
            turn_token=None,
        )
        assert missing_token_answer.outcome == "cancel"

    asyncio.run(run())


def test_session_material_capability_accumulates_and_replaces_paths(
    tmp_path: Path,
) -> None:
    """同一 AgentSession 继承旧材料，并让重新授权的新路径替换旧路径。"""

    old_path = tmp_path / "old.txt"
    new_path = tmp_path / "new.txt"
    old_path.write_text("旧", encoding="utf-8")
    new_path.write_text("新", encoding="utf-8")
    session = AgentSession(workspace_root=tmp_path)
    turn = _bind_turn(session, "activity-material")
    session.extend_materials(
        (
            AgentMaterial("material-a", "报告.txt", "dragged_file", "text", str(old_path), "file"),
            AgentMaterial("material-missing", "失效资料.txt", "dragged_file", "text", str(tmp_path / "missing.txt"), "file"),
        )
    )

    assert str(old_path.resolve()) in {
        material.local_path for material in turn.material_access.capabilities()
    }
    session.extend_materials(
        (AgentMaterial("material-a", "报告.txt", "dragged_file", "text", str(new_path), "file"),)
    )

    assert session.material_catalog()[0].local_path == str(new_path)
    readable_paths = {
        material.local_path for material in turn.material_access.capabilities()
    }
    assert str(old_path.resolve()) not in readable_paths
    assert str(new_path.resolve()) in readable_paths
    assert "失效资料.txt" in session.material_limitations()


async def _drain(session: AgentSession) -> None:
    """排空测试 Session 当前的消息流。"""

    async for _ in session.receive_response():
        pass


def _bind_turn(session: AgentSession, activity_id: str):
    """为测试 Session 创建一项带独立 Capability 的 Activity Turn。"""

    bridge = InteractionBridge(
        activity_id=activity_id,
        request_notifier=lambda _request: None,
    )
    return session.bind_turn(
        activity_id=activity_id,
        bridge=bridge,
        permission_gate=ActivityPermissionGate(
            interaction_bridge=bridge,
            resolution_notifier=lambda _request, _outcome, _suppress_notice: None,
        ),
        material_catalog=(),
    )


def _result_message() -> ResultMessage:
    """构造能够证明一轮 SDK 响应完整结束的最小终态消息。"""

    return ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id="sdk-session-a",
        result="完成",
        structured_output=None,
    )
