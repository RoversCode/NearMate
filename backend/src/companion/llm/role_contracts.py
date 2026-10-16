from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from companion.llm.model_registry import (
    LlmCapability,
    ModelIntegrationSpec,
    get_model_integration,
)


LlmRoleName = Literal[
    "foreground",
    "summary",
    "cognitive",
    "character_authoring",
]
LLM_ROLE_NAMES: tuple[LlmRoleName, ...] = (
    "foreground",
    "summary",
    "cognitive",
    "character_authoring",
)


@dataclass(frozen=True, slots=True)
class RoleContract:
    """描述一个 LLM 职责必须完成的业务合同。

    Args:
        role_id: LLM 职责稳定标识。
        display_name: Console 展示名称。
        description: 该职责实际负责的工作。
        required: Bootstrap 是否必须让该职责可运行。
        default_integration_id: 新配置使用的默认模型接入规格。
        required_capabilities: 用来校验显式兼容边的描述性能力。
    """

    role_id: LlmRoleName
    display_name: str
    description: str
    required: bool
    default_integration_id: str
    required_capabilities: frozenset[LlmCapability]

    def to_payload(self) -> dict[str, Any]:
        """转换为 Console 可以严格解析的职责合同。

        Returns:
            包含显式候选列表的公开职责字段。
        """

        return {
            "role_id": self.role_id,
            "display_name": self.display_name,
            "description": self.description,
            "required": self.required,
            "default_integration_id": self.default_integration_id,
            "required_capabilities": sorted(
                item.value for item in self.required_capabilities
            ),
            "allowed_integration_ids": list(
                ROLE_INTEGRATION_MATRIX[self.role_id]
            ),
        }


ROLE_CONTRACTS: tuple[RoleContract, ...] = (
    RoleContract(
        role_id="foreground",
        display_name="Foreground",
        description="直接理解本轮桌面与用户图片、生成对话回复并调用前台工具。",
        required=True,
        default_integration_id="deepseek:deepseek-flash:chat",
        required_capabilities=frozenset({LlmCapability.TOOL_CALLING, LlmCapability.IMAGE_INPUT}),
    ),
    RoleContract(
        role_id="summary",
        display_name="Summary",
        description="把运行期消息压缩成可继续对话的短摘要。",
        required=True,
        default_integration_id="deepseek:deepseek-flash:chat",
        required_capabilities=frozenset(),
    ),
    RoleContract(
        role_id="cognitive",
        display_name="Cognitive",
        description="继承本轮图片证据，执行联网核查与深度分析。",
        required=True,
        default_integration_id="qwen:qwen3.6-plus:chat",
        required_capabilities=frozenset(
            {
                LlmCapability.IMAGE_INPUT,
                LlmCapability.WEB_SEARCH,
                LlmCapability.WEB_EXTRACT,
            }
        ),
    ),
    RoleContract(
        role_id="character_authoring",
        display_name="Character Authoring",
        description="生成角色唤醒回复和 StreamVox 示例等结构化文案。",
        required=False,
        default_integration_id="deepseek:deepseek-flash:chat",
        required_capabilities=frozenset({LlmCapability.JSON_OUTPUT}),
    ),
)


ROLE_INTEGRATION_MATRIX: dict[LlmRoleName, tuple[str, ...]] = {
    "foreground": ("deepseek:deepseek-flash:chat",),
    "summary": (
        "deepseek:deepseek-flash:chat",
        "deepseek:deepseek-v4-pro:chat",
    ),
    "cognitive": ("qwen:qwen3.6-plus:chat",),
    "character_authoring": (
        "deepseek:deepseek-flash:chat",
        "deepseek:deepseek-v4-pro:chat",
    ),
}


def list_role_contracts() -> list[RoleContract]:
    """返回全部 LLM 职责合同。

    Returns:
        与声明顺序一致的新列表。
    """

    return list(ROLE_CONTRACTS)


def get_role_contract(role_id: str) -> RoleContract:
    """读取一个 LLM 职责合同。

    Args:
        role_id: 需要查询的职责标识。

    Returns:
        匹配的职责合同。

    Raises:
        ValueError: 职责不存在。
    """

    normalized_role_id = role_id.strip()
    for contract in ROLE_CONTRACTS:
        if contract.role_id == normalized_role_id:
            return contract
    raise ValueError(f"未知 LLM 职责：{normalized_role_id}")


def validate_role_integration(
    role_id: str,
    integration_id: str,
) -> ModelIntegrationSpec:
    """校验一条显式职责兼容边及其描述性能力。

    Args:
        role_id: 需要绑定模型的 LLM 职责。
        integration_id: 配置选择的模型接入规格编号。

    Returns:
        已通过矩阵和能力断言的模型接入规格。

    Raises:
        ValueError: 职责、接入规格或显式兼容边无效。
    """

    contract = get_role_contract(role_id)
    integration = get_model_integration(integration_id)
    allowed_integration_ids = ROLE_INTEGRATION_MATRIX[contract.role_id]
    if integration.integration_id not in allowed_integration_ids:
        raise ValueError(
            f"LLM 职责 {contract.role_id} 不允许使用接入规格："
            f"{integration.integration_id}"
        )

    missing_capabilities = sorted(
        capability.value
        for capability in contract.required_capabilities
        if not integration.supports(capability)
    )
    if missing_capabilities:
        raise ValueError(
            f"显式兼容边 {contract.role_id} × {integration.integration_id} "
            f"缺少能力声明：{', '.join(missing_capabilities)}"
        )
    return integration


def role_contracts_payload() -> list[dict[str, Any]]:
    """构造全部 LLM 职责合同的公开载荷。

    Returns:
        Console 只能据此生成候选的职责合同列表。
    """

    return [contract.to_payload() for contract in ROLE_CONTRACTS]
