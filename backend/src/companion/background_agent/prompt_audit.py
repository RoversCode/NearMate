"""把 Background Agent 的真实 SDK query 安全投影到 Prompt Audit。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

from claude_agent_sdk import ClaudeSDKClient

from companion.background_agent.agent_session import AgentQueryDispatch
from companion.observability.logging_config import get_backend_logger
from companion.observability.prompt_audit import PromptAuditRecorder


logger = get_backend_logger(__name__)

_CREDENTIAL_ENVIRONMENT_NAMES = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
    }
)
_CUSTOM_HEADER_ENVIRONMENT_NAMES = frozenset(
    {
        "ANTHROPIC_CUSTOM_HEADERS",
    }
)
_SAFE_MODEL_ENVIRONMENT_NAMES = frozenset(
    {
        "ANTHROPIC_MODEL",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL",
        "ANTHROPIC_DEFAULT_SONNET_MODEL",
        "ANTHROPIC_DEFAULT_OPUS_MODEL",
        "CLAUDE_CODE_SUBAGENT_MODEL",
    }
)
_SAFE_PATH_ENVIRONMENT_NAMES = frozenset({"CLAUDE_CONFIG_DIR"})
_SAFE_PLAINTEXT_ENVIRONMENT_NAMES = (
    _SAFE_MODEL_ENVIRONMENT_NAMES | _SAFE_PATH_ENVIRONMENT_NAMES
)
_CREDENTIAL_PLACEHOLDER = "[已屏蔽凭据]"
_REQUEST_HEADER_PLACEHOLDER = "[已屏蔽请求头]"
_OMITTED_VALUE_PLACEHOLDER = "[值已省略]"


class BackgroundPromptAuditDispatcher:
    """在 Session 最终门禁之后原子记录并提交 Background query。

    Args:
        recorder: 子进程共享的 Prompt Audit 记录器；为空时直接调用 SDK。
    """

    def __init__(self, recorder: PromptAuditRecorder | None) -> None:
        """保存共享记录器，不在构造阶段访问文件系统。

        Args:
            recorder: 可选的子进程共享 Prompt Audit 记录器。
        """

        self._recorder = recorder

    async def __call__(
        self,
        client: ClaudeSDKClient,
        dispatch: AgentQueryDispatch,
    ) -> None:
        """先写审计，再在同一临界区把 query 写入 SDK transport。

        Args:
            client: 已连接且属于当前 AgentSession Turn 的 SDK Client。
            dispatch: Session 最终校验后冻结的 Activity、Prompt 和有效 options。

        Returns:
            无；真实 SDK 异常或协程取消原样传播。
        """

        recorder = self._recorder
        if recorder is None or not recorder.enabled:
            # 关闭态连 options 字段都不读取，避免无意义复制和脱敏成本。
            await client.query(dispatch.prompt)
            return

        try:
            request = build_background_prompt_audit_request(
                dispatch.options,
                dispatch.prompt,
            )
            model = str(getattr(dispatch.options, "model", None) or "")
        except Exception:
            # 投影失败发生在 SDK 调用之前，可以安全地直接执行且只执行一次。
            logger.warning(
                "后台智能体 Prompt 审查投影失败 activity_id=%s",
                dispatch.activity_id or "unknown",
                exc_info=True,
            )
            await client.query(dispatch.prompt)
            return

        await recorder.record_request_and_await(
            role="background_agent",
            source="background_work",
            protocol="claude_agent_sdk",
            model=model,
            operation="execute",
            request=request,
            trace_fields={
                "activity_id": dispatch.activity_id,
                "activity_kind": "background_work",
            },
            sdk_call=lambda: client.query(dispatch.prompt),
        )


def build_background_prompt_audit_request(
    options: Any,
    prompt_text: str,
) -> dict[str, Any]:
    """把真实 SDK options 和 query 文本投影成安全审计副本。

    Args:
        options: 当前 Client 构造时实际生效的 SDK options。
        prompt_text: 已经由 AgentSession 规范化的真实 query 文本。

    Returns:
        保留行为参数、但不包含复合环境凭据和可执行回调的普通字典。
    """

    system_prompt = getattr(options, "system_prompt", None)
    append_text = ""
    preset_name = ""
    if isinstance(system_prompt, Mapping):
        raw_append = system_prompt.get("append")
        append_text = raw_append if isinstance(raw_append, str) else ""
        raw_preset = system_prompt.get("preset")
        preset_name = raw_preset if isinstance(raw_preset, str) else ""

    return {
        "messages": [
            {
                "role": "system",
                "content": append_text,
                "system_prompt_preset": preset_name,
            },
            {"role": "user", "content": prompt_text},
        ],
        "model": getattr(options, "model", None),
        "effort": getattr(options, "effort", None),
        "max_turns": getattr(options, "max_turns", None),
        "permission_mode": getattr(options, "permission_mode", None),
        "cwd": getattr(options, "cwd", None),
        "skills": list(getattr(options, "skills", None) or []),
        "allowed_tools": list(getattr(options, "allowed_tools", None) or []),
        "disallowed_tools": list(
            getattr(options, "disallowed_tools", None) or []
        ),
        "setting_sources": list(
            getattr(options, "setting_sources", None) or []
        ),
        "settings": getattr(options, "settings", None),
        "output_format": getattr(options, "output_format", None),
        "enable_file_checkpointing": getattr(
            options,
            "enable_file_checkpointing",
            None,
        ),
        "strict_mcp_config": getattr(options, "strict_mcp_config", None),
        "resume": getattr(options, "resume", None),
        "session_id": getattr(options, "session_id", None),
        "continue_conversation": getattr(options, "continue_conversation", None),
        "fork_session": getattr(options, "fork_session", None),
        "extra_args": getattr(options, "extra_args", None),
        "session_store_enabled": getattr(options, "session_store", None) is not None,
        "session_store_flush": getattr(options, "session_store_flush", None),
        "callbacks": {
            "can_use_tool": callable(getattr(options, "can_use_tool", None)),
            "stderr": callable(getattr(options, "stderr", None)),
            "debug_stderr": getattr(options, "debug_stderr", None) is not None,
        },
        "hook_events": _enabled_hook_event_names(getattr(options, "hooks", None)),
        "env": project_background_audit_environment(
            getattr(options, "env", None)
        ),
    }


def project_background_audit_environment(environment: object) -> dict[str, object]:
    """按安全值白名单投影 Claude Code 环境变量。

    Args:
        environment: SDK options 中最终生效的环境变量映射。

    Returns:
        凭据和请求头已屏蔽、Base URL 已拆分、未知值已省略的映射。
    """

    if not isinstance(environment, Mapping):
        return {}

    projected: dict[str, object] = {}
    for raw_name, raw_value in environment.items():
        name = str(raw_name).strip().upper()
        if not name:
            continue
        if name in _CREDENTIAL_ENVIRONMENT_NAMES:
            projected[name] = _CREDENTIAL_PLACEHOLDER
            continue
        if name in _CUSTOM_HEADER_ENVIRONMENT_NAMES:
            projected[name] = _REQUEST_HEADER_PLACEHOLDER
            continue
        if name == "ANTHROPIC_BASE_URL":
            projected[name] = _project_base_url(raw_value)
            continue
        if name in _SAFE_PLAINTEXT_ENVIRONMENT_NAMES:
            projected[name] = str(raw_value)
            continue
        projected[name] = _OMITTED_VALUE_PLACEHOLDER
    return projected


def _project_base_url(value: object) -> dict[str, object] | str:
    """只保留 Base URL 中不会携带认证信息的结构字段。

    Args:
        value: 原始 Base URL 值。

    Returns:
        scheme、host、port、path；URL 无法安全解析时返回省略占位符。
    """

    try:
        parsed = urlsplit(str(value))
        if not parsed.scheme or not parsed.hostname:
            return _OMITTED_VALUE_PLACEHOLDER
        return {
            "scheme": parsed.scheme,
            "host": parsed.hostname,
            "port": parsed.port,
            "path": parsed.path,
        }
    except (TypeError, ValueError):
        return _OMITTED_VALUE_PLACEHOLDER


def _enabled_hook_event_names(hooks: object) -> list[str]:
    """列出真正配置了 Hook 的事件名，不序列化 Matcher 或函数对象。

    Args:
        hooks: SDK options 中的 Hook 映射。

    Returns:
        有非空配置的稳定排序事件名列表。
    """

    if not isinstance(hooks, Mapping):
        return []
    return sorted(
        str(event_name)
        for event_name, matchers in hooks.items()
        if matchers
    )
