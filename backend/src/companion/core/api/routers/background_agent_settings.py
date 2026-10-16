"""提供后台智能体 Claude Code 配置的 Console 接口。"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator

from companion.background_agent.settings import (
    AgentEffortLevel,
    AgentPermissionMode,
    BackgroundAgentSettings,
    normalize_background_agent_environment,
    normalize_background_agent_model,
)
from companion.core.api.dependencies import BackgroundAgentDependency


router = APIRouter()


class BackgroundAgentConfigSettingsRequest(BaseModel):
    """描述一次 PUT 请求中可公开保存的后台工作设置。

    与允许空模型的本地持久化状态不同，用户一旦提交配置就必须明确给出主模型。

    Args:
        environment: Claude Code 官方非密钥环境变量。
        model: 后台工作明确使用的非空主模型。
        effort_level: 可选推理强度；为空时使用模型默认值。
        max_turns: 单任务最多执行的 Agent 轮数；零表示不限制。
        permission_mode: 后台工作采用的权限模式。
    """

    model_config = ConfigDict(extra="forbid")

    environment: dict[str, str] = Field(default_factory=dict)
    model: str
    effort_level: AgentEffortLevel | None = None
    max_turns: StrictInt = Field(default=30, ge=0, le=200)
    permission_mode: AgentPermissionMode = "acceptEdits"

    @field_validator("environment", mode="before")
    @classmethod
    def normalize_environment(cls, value: Any) -> dict[str, str]:
        """过滤不能进入普通用户设置的环境变量。

        Args:
            value: Console 提交的原始环境变量对象。

        Returns:
            已统一键名并移除密钥和一等配置字段的环境变量。
        """

        return normalize_background_agent_environment(value)

    @field_validator("model")
    @classmethod
    def require_model(cls, value: str) -> str:
        """裁剪并拒绝空主模型。

        Args:
            value: Console 提交的主模型名称。

        Returns:
            裁剪后的非空模型名称。

        Raises:
            ValueError: 模型名称为空时抛出。
        """

        normalized = normalize_background_agent_model(value)
        if not normalized:
            raise ValueError("后台工作主模型不能为空")
        return normalized

    def to_settings(self) -> BackgroundAgentSettings:
        """转换为应用服务使用的持久化设置模型。

        Returns:
            已通过 PUT 边界严格校验的后台工作设置。
        """

        return BackgroundAgentSettings.model_validate(self.model_dump())


class BackgroundAgentConfigRequest(BaseModel):
    """描述 Console 提交的后台智能体配置。

    Args:
        settings: 不含密钥的 Claude Code 环境和任务护栏。
        credential_type: Console 当前选择的官方凭据类型。
        credential_value: 新凭据；空字符串表示保留已有值。
        clear_credential: 是否明确清除后台智能体凭据。
    """

    model_config = ConfigDict(extra="forbid")

    settings: BackgroundAgentConfigSettingsRequest
    credential_type: Literal["api_key", "auth_token"] = "api_key"
    credential_value: str = ""
    clear_credential: bool = False

    @field_validator("credential_value")
    @classmethod
    def trim_credential_value(cls, value: str) -> str:
        """裁剪凭据输入，但不把值写入普通设置对象。

        Args:
            value: Console 提交的原始凭据。

        Returns:
            去掉首尾空白的凭据。
        """

        return value.strip()


@router.get("/api/background-agent/config")
async def get_background_agent_config(
    container: BackgroundAgentDependency,
) -> dict[str, object]:
    """读取后台智能体安全配置视图。

    Args:
        container: 当前 Backend 组合根依赖。

    Returns:
        不含 API Key 或 Auth Token 明文的 Console 配置。
    """

    return await container.user_settings.current_background_agent_config_payload()


@router.put("/api/background-agent/config", status_code=202)
async def update_background_agent_config(
    body: BackgroundAgentConfigRequest,
    container: BackgroundAgentDependency,
) -> dict[str, object]:
    """保存后台智能体配置，并让之后创建的新任务读取新值。

    Args:
        body: 已通过 Pydantic 校验的配置请求。
        container: 当前 Backend 组合根依赖。

    Returns:
        保存后的安全配置视图，以及同一设置事务生成的完整公开设置快照。
    """

    try:
        return await container.user_settings.update_background_agent_config(
            settings=body.settings.to_settings(),
            credential_type=body.credential_type,
            credential_value=body.credential_value,
            clear_credential=body.clear_credential,
        )
    except ValueError as exception:
        raise HTTPException(status_code=400, detail=str(exception)) from exception
