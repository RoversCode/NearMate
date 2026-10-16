from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from companion.llm.model_registry import (
    ModelIntegrationSpec,
    get_provider_credential_spec,
    list_provider_credential_specs,
)
from companion.llm.role_contracts import (
    LLM_ROLE_NAMES,
    LlmRoleName,
    RoleContract,
    get_role_contract,
    list_role_contracts,
    validate_role_integration,
)


LLM_CONFIG_SCHEMA_VERSION = 2
QWEN_WORKSPACE_BASE_URL_TEMPLATE = (
    "https://{workspace_id}.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
)
QWEN_WORKSPACE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
# OpenAI SDK 基础网络重试次数：首次请求失败后最多由 SDK 再尝试两次。
# SDK 重试对 Activity 不可见，产品层不设计重试状态；已建立流的中断不会被 SDK 重试。
LLM_SDK_MAX_RETRIES = 2


class LlmSettingsValidationError(ValueError):
    """表示 LLM 设置无法满足已声明的职责合同。"""


class ProviderCredential(BaseModel):
    """保存用户为单个厂商配置的真实凭据。

    Args:
        base_url: OpenAI 兼容接口基础地址或 Qwen WorkspaceId。
        api_key: 厂商鉴权密钥。
    """

    model_config = ConfigDict(extra="forbid")

    base_url: str = ""
    api_key: str = ""

    @field_validator("base_url", "api_key", mode="before")
    @classmethod
    def trim_string_fields(cls, value: Any) -> str:
        """裁剪凭据文本字段。

        Args:
            value: 尚未规范化的配置值。

        Returns:
            去掉首尾空白的字符串。
        """

        return str(value or "").strip()


class LlmRuntimeConfig(BaseModel):
    """保存一个已解析职责调用 Chat SDK 所需的运行时字段。

    Args:
        integration_id: 当前职责绑定的不透明接入规格编号。
        provider_id: 当前调用使用的厂商标识。
        model: 发送给厂商的真实模型名。
        base_url: 已规范化的接口基础地址。
        api_key: 当前厂商鉴权密钥。
        timeout_s: 可选请求超时秒数。
    """

    model_config = ConfigDict(extra="forbid")

    integration_id: str
    provider_id: str
    model: str
    base_url: str
    api_key: str
    timeout_s: float | None = None

    @field_validator(
        "integration_id",
        "provider_id",
        "model",
        "base_url",
        "api_key",
        mode="before",
    )
    @classmethod
    def trim_required_strings(cls, value: Any) -> str:
        """裁剪运行时字符串字段。

        Args:
            value: 尚未规范化的字段值。

        Returns:
            去掉首尾空白的字符串。
        """

        return str(value or "").strip()

    @field_validator("timeout_s")
    @classmethod
    def reject_invalid_timeout(cls, value: float | None) -> float | None:
        """拒绝非正数请求超时。

        Args:
            value: 可选超时秒数。

        Returns:
            合法值或空值。
        """

        if value is not None and value <= 0:
            raise ValueError("timeout_s 必须大于 0")
        return value


def _default_credentials() -> dict[str, ProviderCredential]:
    """创建只含静态默认端点的空凭据池。

    Returns:
        每个 V1 厂商各一份互不共享的凭据对象。
    """

    return {
        spec.provider_id: ProviderCredential(
            base_url=spec.default_base_url if spec.default_base_url_allowed else "",
            api_key="",
        )
        for spec in list_provider_credential_specs()
    }


def _default_role_bindings() -> dict[str, str | None]:
    """创建四个 LLM 职责的默认绑定。

    Returns:
        职责标识到不透明接入规格编号的字典。
    """

    return {
        contract.role_id: contract.default_integration_id
        for contract in list_role_contracts()
    }


class LlmSettings(BaseModel):
    """保存 schema v2 的厂商凭据和显式职责绑定。

    Args:
        schema_version: 固定为 2 的配置结构版本。
        credentials: 按厂商标识保存的用户凭据。
        role_bindings: 按 LLM 职责保存的模型接入规格编号。
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[2] = LLM_CONFIG_SCHEMA_VERSION
    credentials: dict[str, ProviderCredential] = Field(
        default_factory=_default_credentials
    )
    role_bindings: dict[str, str | None] = Field(
        default_factory=_default_role_bindings
    )

    @field_validator("credentials", mode="before")
    @classmethod
    def normalize_credentials_shape(cls, value: Any) -> dict[str, Any]:
        """补齐已支持厂商并拒绝未知厂商。

        Args:
            value: 配置文件或 API 提交的凭据对象。

        Returns:
            键已裁剪且包含全部 V1 厂商的凭据字典。
        """

        if value is None:
            raw_credentials: dict[Any, Any] = {}
        elif isinstance(value, dict):
            raw_credentials = value
        else:
            raise ValueError("credentials 必须是对象")

        supported_provider_ids = {
            spec.provider_id for spec in list_provider_credential_specs()
        }
        normalized: dict[str, Any] = {}
        for raw_provider_id, credential in raw_credentials.items():
            provider_id = str(raw_provider_id or "").strip()
            if provider_id not in supported_provider_ids:
                raise ValueError(f"credentials 包含未知厂商：{provider_id}")
            normalized[provider_id] = credential
        for provider_id, credential in _default_credentials().items():
            normalized.setdefault(provider_id, credential)
        return normalized

    @field_validator("role_bindings", mode="before")
    @classmethod
    def normalize_role_bindings_shape(cls, value: Any) -> dict[str, Any]:
        """补齐职责键并拒绝未知职责。

        Args:
            value: 配置文件或 API 提交的职责绑定对象。

        Returns:
            键已裁剪且包含全部职责的绑定字典。
        """

        if value is None:
            raw_bindings: dict[Any, Any] = {}
        elif isinstance(value, dict):
            raw_bindings = value
        else:
            raise ValueError("role_bindings 必须是对象")

        normalized: dict[str, Any] = {}
        for raw_role_id, integration_id in raw_bindings.items():
            role_id = str(raw_role_id or "").strip()
            if role_id not in LLM_ROLE_NAMES:
                raise ValueError(f"role_bindings 包含未知职责：{role_id}")
            normalized[role_id] = integration_id
        for role_id, integration_id in _default_role_bindings().items():
            normalized.setdefault(role_id, integration_id)
        return normalized

    @model_validator(mode="after")
    def trim_and_validate_binding_values(self) -> LlmSettings:
        """裁剪绑定值并保证必需职责没有被关闭。

        Returns:
            绑定值完成规范化的当前设置。

        Raises:
            ValueError: 必需职责为空或绑定值不是字符串。
        """

        normalized_bindings: dict[str, str | None] = {}
        for role_id in LLM_ROLE_NAMES:
            raw_integration_id = self.role_bindings[role_id]
            if raw_integration_id is None:
                if get_role_contract(role_id).required:
                    raise ValueError(f"必需 LLM 职责不可关闭：{role_id}")
                normalized_bindings[role_id] = None
                continue
            if not isinstance(raw_integration_id, str):
                raise ValueError(f"LLM 职责绑定必须是字符串或 null：{role_id}")
            integration_id = raw_integration_id.strip()
            if not integration_id:
                if get_role_contract(role_id).required:
                    raise ValueError(f"必需 LLM 职责不可为空：{role_id}")
                normalized_bindings[role_id] = None
                continue
            normalized_bindings[role_id] = integration_id
        self.role_bindings = normalized_bindings
        return self


@dataclass(frozen=True, slots=True)
class ResolvedRoleBinding:
    """保存完成静态校验的职责、接入规格和运行时凭据。

    Args:
        role_contract: 当前 LLM 职责合同。
        integration: 已通过显式矩阵校验的模型接入规格。
        credential: 当前接入规格使用的用户凭据。
        runtime_config: 可以创建 Chat Client 的运行时配置。
    """

    role_contract: RoleContract
    integration: ModelIntegrationSpec
    credential: ProviderCredential
    runtime_config: LlmRuntimeConfig


@dataclass(frozen=True, slots=True)
class LlmRoleValidationStatus:
    """描述一个 LLM 职责的静态就绪状态。

    Args:
        enabled: 可选职责当前是否启用。
        is_ready: 绑定、兼容边和凭据是否完整。
        integration_id: 当前绑定的接入规格编号。
        provider_id: 成功解析时使用的厂商标识。
        missing: 缺失的凭据字段。
        errors: 绑定或兼容边错误。
    """

    enabled: bool
    is_ready: bool
    integration_id: str | None
    provider_id: str | None
    missing: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, object]:
        """转换为不包含明文凭据的 API 状态。

        Returns:
            Console 可以直接展示的职责状态。
        """

        return {
            "enabled": self.enabled,
            "is_ready": self.is_ready,
            "integration_id": self.integration_id,
            "provider_id": self.provider_id,
            "missing": list(self.missing),
            "errors": list(self.errors),
        }


@dataclass(frozen=True, slots=True)
class LlmValidationResult:
    """描述整套 LLM 配置及逐职责静态就绪状态。

    Args:
        is_ready: 所有必需和已启用职责是否都可运行。
        missing: 去重后的全部缺失凭据字段。
        errors: 去重后的全部静态错误。
        role_statuses: 按职责保存的详细状态。
    """

    is_ready: bool
    missing: tuple[str, ...]
    errors: tuple[str, ...]
    role_statuses: dict[LlmRoleName, LlmRoleValidationStatus]

    def to_payload(self) -> dict[str, object]:
        """转换为 LLM 配置页使用的完整安全载荷。

        Returns:
            总体状态及逐职责状态；不包含任何明文凭据。
        """

        return {
            "is_ready": self.is_ready,
            "missing": list(self.missing),
            "errors": list(self.errors),
            "roles": {
                role_id: status.to_payload()
                for role_id, status in self.role_statuses.items()
            },
        }

    def to_bootstrap_payload(self) -> dict[str, object]:
        """转换为首次初始化门禁使用的窄状态摘要。

        Returns:
            只有总体就绪、缺失字段和错误摘要的状态；逐职责详情由 LLM 配置接口单独提供。
        """

        return {
            "is_ready": self.is_ready,
            "missing": list(self.missing),
            "errors": list(self.errors),
        }


def resolve_role_binding(
    settings: LlmSettings,
    role_id: str,
) -> ResolvedRoleBinding | None:
    """解析一个职责的显式绑定和厂商凭据。

    Args:
        settings: 当前 schema v2 LLM 设置。
        role_id: 需要解析的 LLM 职责。

    Returns:
        可选职责关闭时返回空，否则返回完整运行时绑定。

    Raises:
        LlmSettingsValidationError: 绑定、显式兼容边或凭据无效。
    """

    try:
        role_contract = get_role_contract(role_id)
    except ValueError as exception:
        raise LlmSettingsValidationError(str(exception)) from exception
    integration_id = settings.role_bindings[role_contract.role_id]
    if integration_id is None:
        if role_contract.required:
            raise LlmSettingsValidationError(
                f"必需 LLM 职责不可关闭：{role_contract.role_id}"
            )
        return None

    try:
        integration = validate_role_integration(
            role_contract.role_id,
            integration_id,
        )
    except ValueError as exception:
        raise LlmSettingsValidationError(str(exception)) from exception

    credential = settings.credentials.get(
        integration.provider_id,
        ProviderCredential(),
    )
    missing = _missing_credential_fields(
        provider_id=integration.provider_id,
        credential=credential,
    )
    if missing:
        raise LlmSettingsValidationError(
            f"LLM 职责 {role_contract.role_id} 缺少厂商凭据："
            f"{', '.join(missing)}"
        )
    return ResolvedRoleBinding(
        role_contract=role_contract,
        integration=integration,
        credential=credential,
        runtime_config=LlmRuntimeConfig(
            integration_id=integration.integration_id,
            provider_id=integration.provider_id,
            model=integration.model,
            base_url=credential.base_url,
            api_key=credential.api_key,
        ),
    )


def validate_llm_settings_ready(settings: LlmSettings) -> LlmValidationResult:
    """静态校验全部必需职责和已启用可选职责。

    Args:
        settings: 需要检查的 schema v2 LLM 设置。

    Returns:
        总体与逐职责就绪状态，不执行任何网络请求。
    """

    role_statuses: dict[LlmRoleName, LlmRoleValidationStatus] = {}
    all_missing: set[str] = set()
    all_errors: list[str] = []
    for contract in list_role_contracts():
        integration_id = settings.role_bindings[contract.role_id]
        if integration_id is None:
            role_statuses[contract.role_id] = LlmRoleValidationStatus(
                enabled=False,
                is_ready=not contract.required,
                integration_id=None,
                provider_id=None,
                errors=(
                    (f"必需 LLM 职责不可关闭：{contract.role_id}",)
                    if contract.required
                    else ()
                ),
            )
            if contract.required:
                all_errors.append(f"必需 LLM 职责不可关闭：{contract.role_id}")
            continue

        try:
            integration = validate_role_integration(
                contract.role_id,
                integration_id,
            )
        except ValueError as exception:
            error = str(exception)
            all_errors.append(error)
            role_statuses[contract.role_id] = LlmRoleValidationStatus(
                enabled=True,
                is_ready=False,
                integration_id=integration_id,
                provider_id=None,
                errors=(error,),
            )
            continue

        credential = settings.credentials.get(
            integration.provider_id,
            ProviderCredential(),
        )
        missing = tuple(
            _missing_credential_fields(
                provider_id=integration.provider_id,
                credential=credential,
            )
        )
        all_missing.update(missing)
        role_statuses[contract.role_id] = LlmRoleValidationStatus(
            enabled=True,
            is_ready=not missing,
            integration_id=integration.integration_id,
            provider_id=integration.provider_id,
            missing=missing,
        )

    return LlmValidationResult(
        is_ready=not all_missing and not all_errors,
        missing=tuple(sorted(all_missing)),
        errors=tuple(dict.fromkeys(all_errors)),
        role_statuses=role_statuses,
    )


def normalize_provider_credentials(
    credentials: dict[str, ProviderCredential],
) -> dict[str, ProviderCredential]:
    """规范化所有 V1 厂商端点。

    Args:
        credentials: API 提交并完成旧 Key 合并后的凭据池。

    Returns:
        可直接持久化和解析运行时绑定的凭据池。

    Raises:
        LlmSettingsValidationError: 厂商未知或端点格式非法。
    """

    normalized: dict[str, ProviderCredential] = {}
    for provider_id, credential in credentials.items():
        try:
            provider_spec = get_provider_credential_spec(provider_id)
        except ValueError as exception:
            raise LlmSettingsValidationError(str(exception)) from exception
        base_url = credential.base_url
        if provider_spec.endpoint_normalizer == "qwen_workspace":
            base_url = normalize_qwen_base_url_or_workspace_id(base_url)
        elif base_url:
            base_url = normalize_standard_base_url(base_url)
        normalized[provider_id] = credential.model_copy(
            update={"base_url": base_url}
        )
    return normalized


def normalize_standard_base_url(value: str) -> str:
    """规范化普通 OpenAI 兼容端点。

    Args:
        value: 用户输入的完整 HTTP 或 HTTPS 地址。

    Returns:
        去掉尾部斜杠的完整地址。

    Raises:
        LlmSettingsValidationError: 地址缺少合法协议或主机。
    """

    normalized_value = value.strip().rstrip("/")
    parsed_url = urlparse(normalized_value)
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
        raise LlmSettingsValidationError("LLM base_url 必须是完整 HTTP 或 HTTPS 地址")
    return normalized_value


def normalize_qwen_base_url_or_workspace_id(value: str) -> str:
    """把 Qwen WorkspaceId 或完整地址规范化为固定兼容端点。

    Args:
        value: Console 输入的 WorkspaceId 或完整地址。

    Returns:
        完整 Qwen OpenAI 兼容 base_url；空输入保持为空。

    Raises:
        LlmSettingsValidationError: 地址或 WorkspaceId 不符合固定规则。
    """

    normalized_value = value.strip()
    if not normalized_value:
        return ""
    if normalized_value.startswith(("http://", "https://")):
        workspace_id = extract_qwen_workspace_id_from_base_url(normalized_value)
        if workspace_id is None:
            raise LlmSettingsValidationError(
                "Qwen base_url 必须匹配 "
                "https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/"
                "compatible-mode/v1"
            )
        return QWEN_WORKSPACE_BASE_URL_TEMPLATE.format(workspace_id=workspace_id)
    if not QWEN_WORKSPACE_ID_PATTERN.fullmatch(normalized_value):
        raise LlmSettingsValidationError(
            "Qwen WorkspaceId 只能包含字母、数字、下划线或连字符"
        )
    return QWEN_WORKSPACE_BASE_URL_TEMPLATE.format(workspace_id=normalized_value)


def extract_qwen_workspace_id_from_base_url(value: str) -> str | None:
    """从 Qwen 完整端点提取 WorkspaceId。

    Args:
        value: 完整 Qwen OpenAI 兼容地址。

    Returns:
        匹配成功时返回 WorkspaceId，否则返回空。
    """

    parsed_url = urlparse(value.strip())
    suffix = ".cn-beijing.maas.aliyuncs.com"
    if parsed_url.scheme != "https" or not parsed_url.netloc.endswith(suffix):
        return None
    workspace_id = parsed_url.netloc[: -len(suffix)]
    if parsed_url.path.rstrip("/") != "/compatible-mode/v1":
        return None
    if not QWEN_WORKSPACE_ID_PATTERN.fullmatch(workspace_id):
        return None
    return workspace_id


def _missing_credential_fields(
    *,
    provider_id: str,
    credential: ProviderCredential,
) -> list[str]:
    """返回一个厂商仍缺少的运行时凭据字段。

    Args:
        provider_id: 当前凭据所属厂商。
        credential: 已规范化的用户凭据。

    Returns:
        形如 deepseek.api_key 的缺失字段列表。
    """

    missing: list[str] = []
    if not credential.base_url:
        missing.append(f"{provider_id}.base_url")
    if not credential.api_key:
        missing.append(f"{provider_id}.api_key")
    return missing
