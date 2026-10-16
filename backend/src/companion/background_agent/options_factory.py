"""组装 Claude Agent SDK 选项和产品级工具边界。"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable

from claude_agent_sdk import ClaudeAgentOptions
from claude_agent_sdk.types import HookMatcher, PermissionResultAllow, PermissionResultDeny

from companion.background_agent.contracts import (
    AGENT_COMPLETION_OUTPUT_FORMAT,
    AgentMaterial,
    AgentPermissionMode,
    AgentTask,
)
from companion.background_agent.cua_integration import (
    CUA_BROWSER_MCP_TOOL_NAMES,
    CUA_BROWSER_SOCKET_ENVIRONMENT_VARIABLE,
    CUA_SKILL_ID,
    CUA_TOOL_PREFIX,
    CuaIntegration,
)
from companion.background_agent.interaction_bridge import AgentInteractionAnswer
from companion.background_agent.material_access import resolve_allowed_task_path
from companion.background_agent.material_presentation import (
    build_task_material_presentations,
    render_task_material_entries,
)
from companion.filesystem.safety import lexical_absolute_path
from companion.background_agent.model_config import AgentModelConfig
from companion.background_agent.permission_presentation import (
    build_permission_action_summary,
)
from companion.background_agent.agent_session import AgentSession, AgentSessionTurn


_CUA_DRIVER_TOKEN_PATTERN = re.compile(r"(?<![\w-])cua-driver(?:\.exe)?\b", re.IGNORECASE)
_BASH_LINE_CONTINUATION_PATTERN = re.compile(r"\\\r?\n")
_CUA_SHARED_SOCKET_PATTERN = re.compile(
    r"(?:^|\s)--socket\s+[\"']?\$\{?NEARMATE_CUA_BROWSER_SOCKET\}?[\"']?(?=\s|$)",
    re.IGNORECASE,
)
def build_agent_options(
    *,
    task: AgentTask,
    model_config: AgentModelConfig,
    system_prompt_path: Path,
    session: AgentSession,
    turn_capability: AgentSessionTurn,
    cua_integration: CuaIntegration | None = None,
    stderr: Callable[[str], None] | None = None,
    max_turns: int = 30,
) -> ClaudeAgentOptions:
    """为一项 NearMate 任务组装隔离的 Claude Code 运行选项。

    Args:
        task: 已冻结的后台任务。
        model_config: 官方环境变量透传配置。
        system_prompt_path: 产品边界提示词文件路径。
        session: 保存产品会话上下文并验证 Turn Capability 的会话。
        turn_capability: 当前 Activity 独占且由回调闭包冻结的不可变 Adapter。
        cua_integration: 官方 Cua Driver 版本、doctor 与 stdio 配置入口。
        stderr: CLI 标准错误输出回调。
        max_turns: 单任务最多允许的 Agent 轮数；0 表示不限制。

    Returns:
        SDK 可直接使用的 `ClaudeAgentOptions`。
    """

    cua_enabled = task.cua_capability_granted
    if cua_enabled and cua_integration is None:
        raise ValueError("冻结 CUA Grant 时必须提供官方集成")
    managed_cua_skill_root = (
        Path(task.agent_config_dir) / "skills" / CUA_SKILL_ID
        if cua_enabled
        else None
    )
    async def can_use_tool(
        tool_name: str,
        input_data: dict[str, Any],
        context: Any,
    ) -> PermissionResultAllow | PermissionResultDeny:
        """把 SDK 权限请求和 AskUserQuestion 转成 NearMate 用户交互。

        Args:
            tool_name: Claude Code 请求使用的工具名称。
            input_data: 工具准备执行的输入。
            context: SDK 提供的权限回调上下文。

        Returns:
            允许后的规范输入，或拒绝和取消结果。
        """

        is_cua_mcp_tool = tool_name.startswith(CUA_TOOL_PREFIX)
        uses_cua_cli = _bash_uses_cua_driver(tool_name, input_data)
        if (is_cua_mcp_tool or uses_cua_cli) and not cua_enabled:
            return PermissionResultDeny(
                message="cua_unavailable：当前 Activity 没有获得 CUA 能力。",
                interrupt=False,
            )
        if _is_browser_mcp_tool(tool_name):
            return PermissionResultDeny(
                message="Browser Use 必须全程使用官方 CLI，不能调用 Browser MCP 工具。",
                interrupt=False,
            )
        if uses_cua_cli and not _uses_shared_cua_socket(input_data):
            return PermissionResultDeny(
                message=(
                    "CUA CLI 必须显式使用同一 Embedded Runtime 的 "
                    f"${CUA_BROWSER_SOCKET_ENVIRONMENT_VARIABLE} socket。"
                ),
                interrupt=False,
            )
        is_cua_operation = is_cua_mcp_tool or uses_cua_cli

        questions = input_data.get("questions") if tool_name == "AskUserQuestion" else None
        normalized_questions = questions if isinstance(questions, list) else []
        if tool_name == "AskUserQuestion":
            answer = await session.request_interaction(
                tool_name=tool_name,
                input_data=input_data,
                questions=normalized_questions,
                turn_token=turn_capability.turn_token,
            )
        else:
            action_summary = (
                "操作完成任务所需的桌面或网页"
                if is_cua_operation
                else build_permission_action_summary(tool_name, context)
            )
            answer = await session.request_permission(
                tool_name=tool_name,
                input_data=_permission_event_input(
                    input_data,
                    is_cua_operation=is_cua_operation,
                ),
                action_summary=action_summary,
                turn_token=turn_capability.turn_token,
            )
        if answer.outcome == "allow":
            if is_cua_operation:
                if not cua_enabled:
                    return PermissionResultDeny(
                        message="cua_unavailable：当前 Activity 没有获得 CUA 能力。",
                        interrupt=False,
                    )
            return PermissionResultAllow(updated_input=input_data)
        if answer.outcome == "answer":
            return PermissionResultAllow(
                updated_input=build_ask_user_question_updated_input(
                    questions=normalized_questions,
                    answer=answer,
                )
            )
        return PermissionResultDeny(
            message=answer.reason or "用户拒绝了这项后台操作。",
            interrupt=answer.outcome == "cancel",
        )

    async def pre_tool_use(input_data: dict[str, Any], _tool_use_id: str | None, _context: Any) -> dict[str, object]:
        """阻止越界工具调用，并给未获 Grant 的 CUA 调用返回明确错误。

        Bash 的任意命令无法靠字符串解析可靠限制，因此仍按权限模式处理；这里覆盖
        SDK 自带文件工具、官方 Browser 本地文件入口和 V1 禁用的子代理入口。
        `bypassPermissions` 不会调用 `can_use_tool`，所以必须在这个必经钩子保留 CUA
        能力和 transport 隔离的快速失败反馈。Browser CLI 的具体操作和文件路径交给
        可信 Agent，不在这里解释 Shell 或 JSON。

        Args:
            input_data: SDK Hook 提供的工具名和工具输入。
            _tool_use_id: SDK 工具调用编号，本护栏无需使用。
            _context: SDK Hook 上下文，本护栏无需使用。

        Returns:
            允许继续时返回空字典，拒绝时返回规范 Hook 决策。
        """

        active_material_capabilities = session.material_capabilities_for_turn(
            turn_capability.turn_token
        )
        if active_material_capabilities is None:
            return _deny_pre_tool_use("当前 Activity Turn 已结束，拒绝迟到的工具回调。")

        tool_name = str(input_data.get("tool_name") or "")
        tool_input = input_data.get("tool_input")
        normalized_tool_input = tool_input if isinstance(tool_input, dict) else {}
        is_cua_mcp_tool = tool_name.startswith(CUA_TOOL_PREFIX)
        uses_cua_cli = _bash_uses_cua_driver(tool_name, normalized_tool_input)
        if (is_cua_mcp_tool or uses_cua_cli) and not cua_enabled:
            return _deny_pre_tool_use("cua_unavailable：当前 Activity 没有获得 CUA 能力。")
        if _is_browser_mcp_tool(tool_name):
            return _deny_pre_tool_use(
                "Browser Use 必须全程使用官方 CLI，不能调用 Browser MCP 工具。"
            )
        if uses_cua_cli and not _uses_shared_cua_socket(normalized_tool_input):
            return _deny_pre_tool_use(
                "CUA CLI 必须显式使用同一 Embedded Runtime 的 "
                f"${CUA_BROWSER_SOCKET_ENVIRONMENT_VARIABLE} socket。"
            )
        is_cua_operation = is_cua_mcp_tool or uses_cua_cli
        if tool_name in {"Agent", "Task"}:
            return _deny_pre_tool_use("V1 暂不允许后台任务再启动子智能体。")
        if not isinstance(tool_input, dict):
            if not is_cua_operation:
                return {}
            tool_input = {}
        candidate_paths = _tool_paths(tool_name, tool_input)
        if tool_name in {"Glob", "Grep"} and not candidate_paths:
            candidate_paths = (turn_capability.workspace_root,)
        for candidate_path in candidate_paths:
            # 回调先通过当前 Turn Token 校验，再读取 AgentSession 累计材料能力。
            # 因此 B 可以继续读取旧材料，A 的迟到回调却不能借用 B 新增的权限。
            active_scene_path = turn_capability.current_scene_path
            active_workspace_root = turn_capability.workspace_root
            if tool_name in {"Write", "Edit", "NotebookEdit"} and _path_is_current_scene(
                candidate_path,
                active_scene_path,
                active_workspace_root,
            ):
                return _deny_pre_tool_use("当前屏幕截图是本轮冻结的现场输入，不能修改。")
            if resolve_allowed_task_path(
                candidate_path,
                active_workspace_root,
                active_material_capabilities,
                recursive=tool_name in {"Glob", "Grep"},
                allow_file_material=True,
                read_only_roots=(str(managed_cua_skill_root),)
                if tool_name in {"Read", "Glob", "Grep"}
                and managed_cua_skill_root is not None
                else (),
            ) is not None:
                continue
            return _deny_pre_tool_use("这项文件操作超出了本次任务允许的工作目录和材料路径。")
        return {}

    system_prompt = system_prompt_path.resolve().read_text(encoding="utf-8")
    mcp_servers: dict[str, object] = {}
    sdk_environment = dict(model_config.to_sdk_environment())
    if cua_enabled and cua_integration is not None:
        system_prompt = (
            f"{system_prompt.rstrip()}\n\n"
            f"{cua_integration.transport_instructions(
                task.activity_id,
                browser_guide_path=managed_cua_skill_root / "BROWSER.md",
            )}"
        )
        mcp_servers[cua_integration.mcp_server_name] = (
            cua_integration.mcp_server_config()
        )
        sdk_environment[CUA_BROWSER_SOCKET_ENVIRONMENT_VARIABLE] = (
            cua_integration.browser_socket_path
        )

    # 默认读取和任务进度工具不弹气泡；Write/Edit 由 acceptEdits 放行，Bash 和问题工具仍由回调接管。
    # Skill 名单来自任务准备时冻结的产品 Registry 快照。传空列表也必须保持显式，
    # 否则 SDK 可能回落到本机 Claude Code 的用户或插件 Skill。
    return ClaudeAgentOptions(
        allowed_tools=[
            "Read",
            "Glob",
            "Grep",
            "TaskCreate",
            "TaskUpdate",
            "TaskList",
            "TaskGet",
        ],
        skills=list(task.skill_snapshot.enabled_skill_ids),
        system_prompt={
            "type": "preset",
            "preset": "claude_code",
            "append": system_prompt,
        },
        cwd=task.workspace_root,
        env=sdk_environment,
        # 每项 Activity 使用自己的 CLAUDE_CONFIG_DIR，只读取该目录的产品规则和
        # Skill。业务 cwd 保持共享，但其中任何项目设置都不能覆盖冻结任务配置。
        setting_sources=["user"],
        permission_mode=task.permission_mode,
        can_use_tool=can_use_tool,
        hooks={"PreToolUse": [HookMatcher(matcher="*", hooks=[pre_tool_use])]},
        mcp_servers=mcp_servers,
        max_turns=None if max_turns == 0 else max_turns,
        # 官方 CUA MCP 可直接返回页面语义与截图；这里保留协议余量，避免 SDK
        # 默认 1 MiB reader 在完整 ToolResult 到达前断开，不改写上游结果。
        max_buffer_size=8 * 1024 * 1024,
        model=model_config.model,
        effort=model_config.effort_level,
        output_format=AGENT_COMPLETION_OUTPUT_FORMAT,
        enable_file_checkpointing=True,
        extra_args={"replay-user-messages": None},
        disallowed_tools=[
            "Agent",
            "Task",
            *sorted(CUA_BROWSER_MCP_TOOL_NAMES),
        ],
        strict_mcp_config=True,
        stderr=stderr,
    )


def build_ask_user_question_updated_input(
    *,
    questions: list[dict[str, Any]],
    answer: AgentInteractionAnswer,
) -> dict[str, object]:
    """生成 SDK AskUserQuestion 能直接交回 Claude 的完整回答。

    没有新增材料时保留原来的结构化答案。存在新增材料时，再用官方支持的
    ``response`` 字段复用任务材料展示规则，明确告诉 Claude 材料来源、媒体类别、
    文件形态、当前真实可读路径以及必要时的完整短文本。

    Args:
        questions: Claude 原始提交的问题列表。
        answer: Foreground 已校验的规范答案和本轮新增正式材料。

    Returns:
        可放入 ``PermissionResultAllow.updated_input`` 的工具输入。
    """

    updated_input: dict[str, object] = {
        "questions": questions,
        "answers": dict(answer.answers),
    }
    if not answer.materials:
        return updated_input

    response_lines = ["用户已经回答当前资料问题："]
    response_lines.extend(
        f"- {question}：{user_answer}"
        for question, user_answer in answer.answers.items()
    )
    if answer.user_text.strip():
        response_lines.extend(["", "本轮用户补充：", answer.user_text.strip()])
    response_lines.extend(
        [
            "",
            "本轮新增正式材料：",
            "这些材料由用户主动补充，默认与当前任务直接相关。继续任务时如果所需信息仍缺失或不明确，必须优先检查这些材料是否包含所需信息。",
            render_task_material_entries(
                build_task_material_presentations(answer.materials)
            ),
        ]
    )
    updated_input["response"] = "\n".join(response_lines)
    return updated_input


def _permission_event_input(
    input_data: dict[str, Any],
    *,
    is_cua_operation: bool,
) -> dict[str, object]:
    """删除 CUA 权限事件中的 URL、正文、路径和底层句柄。

    Args:
        input_data: SDK 真正准备执行的完整工具参数。
        is_cua_operation: 当前调用是否通过官方 CUA MCP 或规范 CLI transport。

    Returns:
        普通工具保留原输入；官方 CUA 工具不携带任何调用参数。
    """

    if not is_cua_operation:
        return input_data
    return {}


def _bash_uses_cua_driver(
    tool_name: str,
    tool_input: dict[str, Any],
) -> bool:
    """判断当前 Bash 是否提到了官方 Cua Driver CLI。

    这里只为 Grant 和共享 socket 两条快速失败门禁识别入口，不尝试理解整条 Shell 命令。

    Args:
        tool_name: Claude Code 准备调用的工具名。
        tool_input: Bash 工具的完整输入。

    Returns:
        Bash 命令中出现 ``cua-driver`` 可执行入口时返回真。
    """

    if tool_name != "Bash":
        return False
    command = tool_input.get("command")
    if not isinstance(command, str):
        return False
    normalized_command = _BASH_LINE_CONTINUATION_PATTERN.sub("", command)
    return _CUA_DRIVER_TOKEN_PATTERN.search(normalized_command) is not None


def _is_browser_mcp_tool(tool_name: str) -> bool:
    """判断官方 MCP 工具是否属于 Browser transport。

    Args:
        tool_name: Claude Agent SDK 准备执行的完整工具名。

    Returns:
        固定版本已知 Browser 工具或未来同前缀 Browser 工具返回真。
    """

    if tool_name in CUA_BROWSER_MCP_TOOL_NAMES:
        return True
    if not tool_name.startswith(CUA_TOOL_PREFIX):
        return False
    official_tool_name = tool_name.removeprefix(CUA_TOOL_PREFIX)
    return official_tool_name.startswith("browser_")


def _uses_shared_cua_socket(tool_input: dict[str, Any]) -> bool:
    """判断 CUA CLI 是否显式绑定宿主注入的共享 socket。

    这里只识别固定的环境变量写法，不尝试解析任意 Shell。这样可以阻止
    `cua-driver call` 意外连接独立 daemon，同时不把 NearMate 变成 Shell 解释器。

    Args:
        tool_input: Claude Code Bash 工具的完整输入。

    Returns:
        命令明确传入 `$NEARMATE_CUA_BROWSER_SOCKET` 时返回真。
    """

    command = tool_input.get("command")
    if not isinstance(command, str):
        return False
    normalized_command = _BASH_LINE_CONTINUATION_PATTERN.sub("", command)
    return _CUA_SHARED_SOCKET_PATTERN.search(normalized_command) is not None


def _tool_paths(tool_name: str, tool_input: dict[str, Any]) -> tuple[str, ...]:
    """从文件工具输入中提取需要检查的路径。

    Args:
        tool_name: Claude Code 工具名称。
        tool_input: 工具准备执行的输入。

    Returns:
        需要经过工作区护栏检查的路径元组。
    """

    if tool_name in {"Read", "Write", "Edit", "NotebookEdit"}:
        path = tool_input.get("file_path") or tool_input.get("notebook_path")
        return (path,) if isinstance(path, str) and path.strip() else ()
    if tool_name in {"Glob", "Grep"}:
        path = tool_input.get("path")
        return (path,) if isinstance(path, str) and path.strip() else ()
    return ()


def _path_is_current_scene(
    candidate: str,
    current_scene_path: str | None,
    workspace_root: str,
) -> bool:
    """判断写入目标是否是本轮冻结截图。

    Args:
        candidate: 文件工具准备写入的路径。
        current_scene_path: 当前任务现场截图的绝对路径；没有现场输入时为空。
        workspace_root: Claude Code 当前工作目录。

    Returns:
        目标与冻结截图是同一个路径时返回真。
    """

    if not current_scene_path:
        return False
    try:
        normalized_workspace_root = Path(workspace_root).resolve(strict=False)
        normalized_candidate = _resolve_candidate_path(candidate, normalized_workspace_root)
        normalized_scene_path = lexical_absolute_path(Path(current_scene_path))
        if normalized_candidate == normalized_scene_path:
            return True
        if normalized_candidate.exists() and normalized_scene_path.exists():
            return normalized_candidate.samefile(normalized_scene_path)
        return normalized_candidate.resolve(strict=False) == normalized_scene_path.resolve(
            strict=False
        )
    except (OSError, RuntimeError, ValueError):
        return False


def _resolve_candidate_path(candidate: str, workspace_root: Path) -> Path:
    """按 Claude Code 的任务工作区解释相对文件路径。

    Args:
        candidate: 工具输入中的绝对或相对路径。
        workspace_root: Claude Code 当前工作目录。

    Returns:
        不要求目标已存在的规范绝对路径。
    """

    candidate_path = Path(candidate).expanduser()
    if not candidate_path.is_absolute():
        candidate_path = workspace_root / candidate_path
    return lexical_absolute_path(candidate_path)


def _deny_pre_tool_use(reason: str) -> dict[str, object]:
    """返回 SDK PreToolUse 拒绝载荷。

    Args:
        reason: 向 Claude Code 解释拒绝原因的安全文案。

    Returns:
        符合 SDK Hook 契约的拒绝字典。
    """

    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }
