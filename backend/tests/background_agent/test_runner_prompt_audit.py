from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from threading import Event

from claude_agent_sdk import AssistantMessage, ResultMessage, SystemMessage
from claude_agent_sdk.types import TextBlock

from companion.background_agent.contracts import AgentEvent, AgentTask
from companion.background_agent.model_config import AgentExecutionConfig, AgentModelConfig
from companion.background_agent.persona import DEFAULT_AGENT_PERSONA
from companion.background_agent.runner import AgentTaskRunner
from companion.background_agent.workspace import WorkspaceManager
from companion.observability.prompt_audit import PromptAuditRecorder


_TEST_ACTIVITY_ID = "00000000-0000-4000-8000-000000000001"


class SuccessClient:
    """立即返回成功终态的最小 SDK Client 替身。"""

    async def connect(self) -> None:
        return None

    async def query(self, _prompt: str) -> None:
        return None

    async def receive_response(self):
        yield SystemMessage(subtype="init", data={"session_id": "session-sdk"})
        yield AssistantMessage(
            content=[TextBlock(text="任务完成")],
            model="claude-sonnet-4-5",
        )
        yield ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id="session-sdk",
            structured_output={
                "display_content": "任务完成",
                "delivery_brief": "任务已经完成并生成了可查看的结果。",
            },
        )

    async def interrupt(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None


def test_runner_records_background_prompt_audit_at_query_commit(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """后台真实 query、Persona 和有效 options 在同一提交边界进入审计。"""

    monkeypatch.setattr(
        "companion.background_agent.runner.ClaudeSDKClient",
        lambda _options: SuccessClient(),
    )
    events: list[AgentEvent] = []
    terminal = Event()
    recorder = PromptAuditRecorder(
        enabled=True,
        output_dir=tmp_path / "prompt-audits",
        max_runs=5,
        process_role="assistant-runtime",
    )
    runner = _runner(tmp_path, events, terminal, prompt_audit_recorder=recorder)
    task = _task(tmp_path)
    # 环境变量覆盖凭据、复合请求头、签名 URL、安全模型别名和未知高级配置。
    execution_config = _execution_config(
        tmp_path,
        environment={
            "ANTHROPIC_API_KEY": "sk-test-secret",
            "ANTHROPIC_AUTH_TOKEN": "auth-token-secret",
            "ANTHROPIC_CUSTOM_HEADERS": "Authorization: Bearer header-secret",
            "ANTHROPIC_BASE_URL": (
                "https://user:password@api.example.test:8443/v1/messages"
                "?signature=query-secret#fragment-secret"
            ),
            "ANTHROPIC_DEFAULT_SONNET_MODEL": "safe-sonnet-alias",
            "CLAUDE_CODE_EXPERIMENTAL_FEATURE": "unknown-secret-value",
        },
    )

    assert runner.start(task, execution_config) is True
    assert terminal.wait(2.0) is True
    runner.shutdown()

    markdown_files = list(
        (tmp_path / "prompt-audits" / "assistant-runtime" / "runs").glob("*/*.md")
    )
    assert len(markdown_files) == 1
    audit_path = markdown_files[0]
    assert audit_path.name.startswith(
        "000001__background_agent__background_work__execute__"
    )
    audit_text = audit_path.read_text(encoding="utf-8")
    # 调用信息表固定记录职责、来源、协议、真实模型和 Activity 身份。
    assert "| `role` | background_agent |" in audit_text
    assert "| `source` | background_work |" in audit_text
    assert "| `protocol` | claude_agent_sdk |" in audit_text
    assert "| `model` | claude-sonnet-4-5 |" in audit_text
    assert "| `operation` | execute |" in audit_text
    assert f"| `activity_id` | {_TEST_ACTIVITY_ID} |" in audit_text
    # Prompt 消息包含 Persona 追加文本（标注 preset 身份）和完整任务文本。
    assert DEFAULT_AGENT_PERSONA.strip().splitlines()[0] in audit_text
    assert "claude_code" in audit_text
    assert "## 执行任务" in audit_text
    assert "完成测试任务" in audit_text
    # 请求参数还原能影响行为的关键 options 与启用的 Skill 名单。
    assert '"permission_mode": "acceptEdits"' in audit_text
    assert '"max_turns": 30' in audit_text
    assert '"skills": []' in audit_text
    assert '"allowed_tools"' in audit_text
    assert '"continue_conversation": false' in audit_text
    assert '"fork_session": false' in audit_text
    assert '"session_id": null' in audit_text
    assert '"extra_args"' in audit_text
    assert '"replay-user-messages": null' in audit_text
    assert '"can_use_tool": true' in audit_text
    assert '"stderr": true' in audit_text
    assert '"hook_events": [\n    "PreToolUse"\n  ]' in audit_text
    assert "CLAUDE_CONFIG_DIR" in audit_text
    # env 先按 Background 白名单投影，通用脱敏器再做第二道防线。
    for secret in (
        "sk-test-secret",
        "auth-token-secret",
        "header-secret",
        "user:password",
        "query-secret",
        "fragment-secret",
        "unknown-secret-value",
    ):
        assert secret not in audit_text
    assert "[已屏蔽凭据]" in audit_text
    assert "[已屏蔽请求头]" in audit_text
    assert "[值已省略]" in audit_text
    assert '"scheme": "https"' in audit_text
    assert '"host": "api.example.test"' in audit_text
    assert '"port": 8443' in audit_text
    assert '"path": "/v1/messages"' in audit_text
    assert "safe-sonnet-alias" in audit_text
    assert "CLAUDE_CODE_EXPERIMENTAL_FEATURE" in audit_text
    escaped_config_dir = str(
        execution_config.model_config.config_dir.absolute()
    ).replace("\\", "\\\\")
    assert escaped_config_dir in audit_text


def test_runner_correction_audit_uses_effective_resume_options(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """纠正审计必须读取真正交给新 Client 的 resume options。"""

    created_options: list[object] = []

    def create_client(options: object) -> SuccessClient:
        """保存每个 Turn 真实构造 Client 时收到的 options。"""

        created_options.append(options)
        return SuccessClient()

    monkeypatch.setattr(
        "companion.background_agent.runner.ClaudeSDKClient",
        create_client,
    )
    events: list[AgentEvent] = []
    terminal = Event()
    recorder = PromptAuditRecorder(
        enabled=True,
        output_dir=tmp_path / "prompt-audits",
        max_runs=5,
        process_role="assistant-runtime",
    )
    runner = _runner(tmp_path, events, terminal, prompt_audit_recorder=recorder)
    first_task = _task(tmp_path)

    assert runner.start(first_task, _execution_config(tmp_path)) is True
    assert terminal.wait(2.0) is True
    assert runner.wait_until_available(2.0) is True
    first_completed = next(event for event in events if event.event_type == "completed")

    terminal.clear()
    second_activity_id = "00000000-0000-4000-8000-000000000002"
    second_task_root = Path(first_task.workspace_root) / "tasks" / second_activity_id
    (second_task_root / "inputs").mkdir(parents=True, exist_ok=True)
    (second_task_root / "outputs").mkdir(exist_ok=True)
    correction_task = replace(
        first_task,
        activity_id=second_activity_id,
        goal="执行纠正后的目标",
        task_output_dir=str((second_task_root / "outputs").resolve()),
        agent_session_id=str(first_completed.payload["agent_session_id"]),
        expected_sdk_session_id="session-sdk",
        based_on_activity_id=first_task.activity_id,
        task_mode="activity_correction",
        correction_instruction="新的目标具有最高优先级。",
    )

    assert runner.start(correction_task, _execution_config(tmp_path)) is True
    assert terminal.wait(2.0) is True
    assert runner.wait_until_available(2.0) is True
    runner.shutdown()

    markdown_files = sorted(
        (tmp_path / "prompt-audits" / "assistant-runtime" / "runs").glob("*/*.md")
    )
    assert len(markdown_files) == 2
    correction_audit = markdown_files[1].read_text(encoding="utf-8")
    assert "执行纠正后的目标" in correction_audit
    assert '"resume": "session-sdk"' in correction_audit
    assert getattr(created_options[1], "resume", None) == "session-sdk"


def test_runner_prompt_audit_disabled_writes_nothing(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """审计关闭时不产生任何文件，任务照常完成。"""

    monkeypatch.setattr(
        "companion.background_agent.runner.ClaudeSDKClient",
        lambda _options: SuccessClient(),
    )
    events: list[AgentEvent] = []
    terminal = Event()
    recorder = PromptAuditRecorder(
        enabled=False,
        output_dir=tmp_path / "prompt-audits",
        max_runs=5,
        process_role="assistant-runtime",
    )
    runner = _runner(tmp_path, events, terminal, prompt_audit_recorder=recorder)

    assert runner.start(_task(tmp_path), _execution_config(tmp_path)) is True
    assert terminal.wait(2.0) is True
    runner.shutdown()

    assert any(event.event_type == "completed" for event in events)
    assert not (tmp_path / "prompt-audits").exists()


def test_runner_prompt_audit_disabled_skips_projection(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """审计关闭时不能读取 options 或构建安全投影。"""

    def fail_projection(*_args: object, **_kwargs: object) -> dict[str, object]:
        """一旦关闭态错误执行审计投影就立即暴露。"""

        raise AssertionError("关闭态不应构建审计投影")

    monkeypatch.setattr(
        "companion.background_agent.runner.ClaudeSDKClient",
        lambda _options: SuccessClient(),
    )
    monkeypatch.setattr(
        "companion.background_agent.prompt_audit.build_background_prompt_audit_request",
        fail_projection,
    )
    events: list[AgentEvent] = []
    terminal = Event()
    recorder = PromptAuditRecorder(
        enabled=False,
        output_dir=tmp_path / "prompt-audits",
        max_runs=5,
        process_role="assistant-runtime",
    )
    runner = _runner(tmp_path, events, terminal, prompt_audit_recorder=recorder)

    assert runner.start(_task(tmp_path), _execution_config(tmp_path)) is True
    assert terminal.wait(2.0) is True
    runner.shutdown()

    assert any(event.event_type == "completed" for event in events)
    assert not (tmp_path / "prompt-audits").exists()


def test_runner_prompt_audit_failure_never_breaks_task(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """审计旁路抛错只记诊断日志，真实后台任务仍正常完成。"""

    recorder = PromptAuditRecorder(
        enabled=True,
        output_dir=tmp_path / "prompt-audits",
        max_runs=5,
        process_role="assistant-runtime",
    )

    def fail_record(**_kwargs: object) -> tuple[Path, int]:
        """模拟 Markdown 或索引写入持续失败。"""

        raise OSError("审计磁盘写入失败")

    monkeypatch.setattr(recorder, "_record_request_locked", fail_record)

    monkeypatch.setattr(
        "companion.background_agent.runner.ClaudeSDKClient",
        lambda _options: SuccessClient(),
    )
    events: list[AgentEvent] = []
    terminal = Event()
    runner = _runner(
        tmp_path,
        events,
        terminal,
        prompt_audit_recorder=recorder,
    )

    assert runner.start(_task(tmp_path), _execution_config(tmp_path)) is True
    assert terminal.wait(2.0) is True
    runner.shutdown()

    completed = next(event for event in events if event.event_type == "completed")
    assert completed.payload["display_content"] == "任务完成"


def _runner(
    tmp_path: Path,
    events: list[AgentEvent],
    terminal: Event,
    *,
    prompt_audit_recorder: object,
) -> AgentTaskRunner:
    """构造携带指定 Prompt 审查记录器的测试 Runner。"""

    def record(event: AgentEvent) -> None:
        events.append(event)
        if event.event_type in {"completed", "failed", "cancelled"}:
            terminal.set()

    return AgentTaskRunner(
        runtime_dir=tmp_path / "runtime",
        workspace_manager=WorkspaceManager(
            asset_root=tmp_path / "assets",
            workspace_root=tmp_path / "workspace",
        ),
        event_callback=record,
        prompt_audit_recorder=prompt_audit_recorder,
    )


def _execution_config(
    tmp_path: Path,
    *,
    environment: dict[str, str] | None = None,
) -> AgentExecutionConfig:
    """构造一项测试任务冻结的可信 SDK 配置。"""

    return AgentExecutionConfig(
        model_config=AgentModelConfig(
            environment=environment or {},
            config_dir=(
                tmp_path / "runtime" / "activities" / _TEST_ACTIVITY_ID
            ),
            model="claude-sonnet-4-5",
        ),
        permission_mode="acceptEdits",
        max_turns=30,
    )


def _task(tmp_path: Path) -> AgentTask:
    """构造与测试工作区一致的后台任务。"""

    workspace_root = (tmp_path / "workspace").resolve()
    task_root = workspace_root / "tasks" / _TEST_ACTIVITY_ID
    agent_config_dir = (
        tmp_path / "runtime" / "activities" / _TEST_ACTIVITY_ID
    ).resolve()
    (task_root / "inputs").mkdir(parents=True, exist_ok=True)
    (task_root / "outputs").mkdir(exist_ok=True)
    agent_config_dir.mkdir(parents=True, exist_ok=True)
    return AgentTask(
        activity_id=_TEST_ACTIVITY_ID,
        interaction_session_id="interaction-a",
        goal="完成测试任务",
        deliverable=True,
        requirements="",
        turn_materials=(),
        material_catalog=(),
        workspace_root=str(workspace_root),
        agent_config_dir=str(agent_config_dir),
        task_output_dir=str((task_root / "outputs").resolve()),
        permission_mode="acceptEdits",
    )
