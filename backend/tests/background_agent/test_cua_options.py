from __future__ import annotations

from pathlib import Path

import pytest

from companion.background_agent.agent_session import AgentSession
from companion.background_agent.contracts import AgentTask
from companion.background_agent.cua_integration import (
    CUA_BROWSER_SOCKET_ENVIRONMENT_VARIABLE,
    CUA_MCP_SERVER_NAME,
    CUA_SKILL_ID,
)
from companion.background_agent.interaction_bridge import InteractionBridge
from companion.background_agent.model_config import AgentModelConfig
from companion.background_agent.options_factory import build_agent_options
from companion.background_agent.permission_gate import ActivityPermissionGate
from companion.background_agent.skill_models import SkillExecutionSnapshot


_BROWSER_MCP_TOOL_NAMES = {
    "mcp__cua_driver__browser_prepare",
    "mcp__cua_driver__get_browser_state",
    "mcp__cua_driver__browser_click",
    "mcp__cua_driver__browser_dialog",
    "mcp__cua_driver__browser_download",
    "mcp__cua_driver__browser_navigate",
    "mcp__cua_driver__browser_pointer",
    "mcp__cua_driver__browser_set_input_files",
    "mcp__cua_driver__browser_type",
    "mcp__cua_driver__page",
}


class _FakeCuaIntegration:
    """为 Options 测试提供同一 Runtime 的 MCP 与 socket。"""

    mcp_server_name = CUA_MCP_SERVER_NAME
    browser_socket_path = r"\\.\pipe\nearmate-cua-test"

    def transport_instructions(self, activity_id: str, *, browser_guide_path: Path) -> str:
        """返回包含稳定 Session 和官方指南路径的测试声明。"""

        return f"session={activity_id}; guide={browser_guide_path}"

    def mcp_server_config(self) -> dict[str, object]:
        """返回 Claude Agent SDK 可直接注册的官方 MCP 配置。"""

        return {
            "type": "stdio",
            "command": "cua-driver",
            "args": ["mcp", "--socket", self.browser_socket_path],
            "env": {"CUA_CONNECTION": "shared"},
        }


def _build_options(tmp_path: Path, *, granted: bool):
    """创建冻结 Grant 或无 Grant 的最小 Agent Options。"""

    workspace = tmp_path / "workspace"
    output_root = workspace / "tasks" / "activity-a" / "outputs"
    output_root.mkdir(parents=True)
    config_dir = tmp_path / "config"
    (config_dir / "skills" / CUA_SKILL_ID).mkdir(parents=True)
    system_prompt = tmp_path / "system.md"
    system_prompt.write_text("规则", encoding="utf-8")
    skill_snapshot = SkillExecutionSnapshot(
        enabled_skill_ids=(CUA_SKILL_ID,) if granted else (),
        revision=1,
    )
    task = AgentTask(
        activity_id="activity-a",
        interaction_session_id="session-a",
        goal="完成任务",
        deliverable=False,
        requirements="",
        turn_materials=(),
        material_catalog=(),
        workspace_root=str(workspace),
        agent_config_dir=str(config_dir),
        task_output_dir=str(output_root),
        permission_mode="bypassPermissions",
        cua_capability_granted=granted,
        skill_snapshot=skill_snapshot,
    )
    bridge = InteractionBridge(
        activity_id=task.activity_id,
        request_notifier=lambda _request: None,
    )
    gate = ActivityPermissionGate(
        interaction_bridge=bridge,
        resolution_notifier=lambda _request, _outcome, _suppress_notice: None,
    )
    session = AgentSession(workspace_root=workspace)
    turn = session.bind_turn(
        activity_id=task.activity_id,
        bridge=bridge,
        permission_gate=gate,
        material_catalog=(),
    )
    options = build_agent_options(
        task=task,
        model_config=AgentModelConfig(
            environment={},
            config_dir=config_dir,
            model="claude-sonnet-4-5",
        ),
        system_prompt_path=system_prompt,
        session=session,
        turn_capability=turn,
        cua_integration=_FakeCuaIntegration(),
    )
    return options, task


def test_granted_activity_registers_full_mcp_skill_and_shared_socket(tmp_path: Path) -> None:
    """获准 Activity 得到 Computer MCP、Browser CLI socket 和官方 Skill。"""

    options, _task = _build_options(tmp_path, granted=True)

    assert options.skills == [CUA_SKILL_ID]
    assert options.mcp_servers[CUA_MCP_SERVER_NAME]["args"][-1] == _FakeCuaIntegration.browser_socket_path
    assert options.env[CUA_BROWSER_SOCKET_ENVIRONMENT_VARIABLE] == _FakeCuaIntegration.browser_socket_path
    assert _BROWSER_MCP_TOOL_NAMES <= set(options.disallowed_tools)
    assert "mcp__cua_driver__list_apps" not in options.disallowed_tools
    assert "mcp__cua_driver__launch_app" not in options.disallowed_tools


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command",
    [
        "cua-driver call list_apps",
        "cua-\\\ndriver call list_apps",
    ],
)
async def test_ungranted_activity_hides_cua_and_returns_cua_unavailable(
    tmp_path: Path,
    command: str,
) -> None:
    """无 Grant 时 Skill、MCP、socket 均不可见，直接 CLI 调用快速失败。

    Args:
        tmp_path: pytest 提供的隔离目录。
        command: 当前要验证的 CUA CLI 命令写法。

    Returns:
        无；拒绝结果由断言验证。
    """

    options, _task = _build_options(tmp_path, granted=False)
    hook = options.hooks["PreToolUse"][0].hooks[0]

    decision = await hook(
        {
            "tool_name": "Bash",
            "tool_input": {"command": command},
        },
        "tool-use-a",
        None,
    )

    assert options.skills == []
    assert options.mcp_servers == {}
    assert CUA_BROWSER_SOCKET_ENVIRONMENT_VARIABLE not in options.env
    assert decision["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "cua_unavailable" in decision["hookSpecificOutput"]["permissionDecisionReason"]


@pytest.mark.asyncio
async def test_granted_activity_requires_cli_shared_embedded_socket(tmp_path: Path) -> None:
    """获 Grant 的 CLI 调用也不能悄悄连接独立的 Cua Driver daemon。"""

    options, _task = _build_options(tmp_path, granted=True)
    hook = options.hooks["PreToolUse"][0].hooks[0]

    missing_socket = await hook(
        {
            "tool_name": "Bash",
            "tool_input": {"command": "printf '%s' '{}' | cua-driver call list_apps"},
        },
        "tool-use-missing-socket",
        None,
    )
    shared_socket = await hook(
        {
            "tool_name": "Bash",
            "tool_input": {
                "command": (
                    "printf '%s' '{}' | cua-driver call list_apps "
                    '--socket "$NEARMATE_CUA_BROWSER_SOCKET"'
                )
            },
        },
        "tool-use-shared-socket",
        None,
    )

    assert missing_socket["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "NEARMATE_CUA_BROWSER_SOCKET" in missing_socket["hookSpecificOutput"]["permissionDecisionReason"]
    assert shared_socket == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["browser_set_input_files", "browser_download"])
async def test_cli_file_transfer_commands_are_allowed_with_grant_and_shared_socket(
    tmp_path: Path,
    tool_name: str,
) -> None:
    """可信 Agent 可通过 Browser CLI 自主上传或下载文件。"""

    options, _task = _build_options(tmp_path, granted=True)
    hook = options.hooks["PreToolUse"][0].hooks[0]

    decision = await hook(
        {
            "tool_name": "Bash",
            "tool_input": {
                "command": (
                    f"printf '%s' '{{}}' | cua-driver call {tool_name} "
                    '--socket "$NEARMATE_CUA_BROWSER_SOCKET"'
                )
            },
        },
        "tool-use-transfer",
        None,
    )

    assert decision == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", sorted(_BROWSER_MCP_TOOL_NAMES))
async def test_browser_mcp_tools_are_denied_by_pre_tool_hook(
    tmp_path: Path,
    tool_name: str,
) -> None:
    """即使 SDK 绕过声明层，Browser MCP 也不能进入官方 transport。

    Args:
        tmp_path: pytest 提供的隔离目录。
        tool_name: 当前验证的 Browser MCP 工具名。

    Returns:
        无返回值；拒绝结果由断言验证。
    """

    options, _task = _build_options(tmp_path, granted=True)
    hook = options.hooks["PreToolUse"][0].hooks[0]

    decision = await hook(
        {
            "tool_name": tool_name,
            "tool_input": {},
        },
        "browser-mcp-a",
        None,
    )

    assert decision["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "Browser Use" in decision["hookSpecificOutput"]["permissionDecisionReason"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "tool_input"),
    [
        ("mcp__cua_driver__list_apps", {}),
        ("mcp__cua_driver__launch_app", {"urls": ["https://example.com"]}),
    ],
)
async def test_computer_mcp_tool_remains_available(
    tmp_path: Path,
    tool_name: str,
    tool_input: dict[str, object],
) -> None:
    """Computer MCP 继续使用官方 Schema，不被 Browser transport 隔离误伤。

    Args:
        tmp_path: pytest 提供的隔离目录。
        tool_name: 当前验证的 Computer MCP 工具名。
        tool_input: 直接交给官方工具的原始参数。

    Returns:
        无返回值；Hook 放行结果由断言验证。
    """

    options, _task = _build_options(tmp_path, granted=True)
    hook = options.hooks["PreToolUse"][0].hooks[0]

    decision = await hook(
        {
            "tool_name": tool_name,
            "tool_input": tool_input,
        },
        "computer-mcp-a",
        None,
    )

    assert decision == {}
