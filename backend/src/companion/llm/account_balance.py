from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
import re
from typing import Any
from urllib.parse import urlparse

import httpx

from companion.llm.config import LlmSettings, ProviderCredential
from companion.llm.model_registry import (
    get_model_integration,
    get_provider_credential_spec,
)


ACCOUNT_STATUS_AVAILABLE = "available"
ACCOUNT_STATUS_NOT_CONFIGURED = "not_configured"
ACCOUNT_STATUS_UNSUPPORTED = "unsupported"
ACCOUNT_STATUS_ERROR = "error"
_CURRENCY_PATTERN = re.compile(r"^[A-Za-z]{3,8}$")


@dataclass(frozen=True, slots=True)
class ProviderAccountStatus:
    """保存一个模型接入规格厂商的安全余额投影。

    Args:
        provider_id: 厂商稳定标识。
        display_name: 面向用户展示的厂商名称。
        active: 当前是否被 Foreground 职责使用。
        status: 余额读取状态。
        balance: 厂商返回的剩余余额文本，无法读取时为空。
        currency: 余额币种，无法读取时为空。
        fetched_at: 本次查询完成时间，未查询时为空。
        message: 可直接展示给用户的稳定说明。
    """

    provider_id: str
    display_name: str
    active: bool
    status: str
    balance: str | None = None
    currency: str | None = None
    fetched_at: str | None = None
    message: str | None = None

    def to_payload(self) -> dict[str, object]:
        """转换成不包含 API Key 或厂商原始响应的 JSON 载荷。

        Returns:
            可安全交给控制面和 Desktop 的账户状态字段。
        """

        return {
            "provider_id": self.provider_id,
            "display_name": self.display_name,
            "active": self.active,
            "status": self.status,
            "balance": self.balance,
            "currency": self.currency,
            "fetched_at": self.fetched_at,
            "message": self.message,
        }


def fetch_provider_account_statuses(settings: LlmSettings) -> dict[str, object]:
    """查询所有已登记厂商的当前余额并返回安全快照。

    Args:
        settings: 当前已经提交的 schema v2 LLM 设置。

    Returns:
        包含所有已登记厂商状态的安全 JSON 载荷。

    余额接口不是 OpenAI 兼容 Chat 接口的一部分，因此这里单独按厂商能力
    查询；无法查询时返回稳定状态，不影响聊天和 Bootstrap。
    """

    active_provider_ids = _foreground_provider_ids(settings)
    providers: list[dict[str, object]] = []
    for provider_id in _registered_provider_ids(settings):
        credential = settings.credentials[provider_id]
        provider_spec = get_provider_credential_spec(provider_id)
        status = _fetch_provider_account_status(
            provider_id=provider_id,
            display_name=provider_spec.display_name,
            credential=credential,
            active=provider_id in active_provider_ids,
        )
        providers.append(status.to_payload())
    return {"providers": providers}


def _registered_provider_ids(settings: LlmSettings) -> tuple[str, ...]:
    """按注册表顺序返回厂商，避免配置字典顺序成为 UI 契约。"""

    # 读取注册表时只使用配置中已有的键，未来新增厂商即可自动进入快照。
    from companion.llm.model_registry import list_provider_credential_specs

    configured_ids = set(settings.credentials)
    return tuple(
        spec.provider_id
        for spec in list_provider_credential_specs()
        if spec.provider_id in configured_ids
    )


def _foreground_provider_ids(settings: LlmSettings) -> set[str]:
    """读取 Foreground 当前绑定的厂商，用于 UI 标记当前对话账户。"""

    integration_id = settings.role_bindings.get("foreground")
    if not integration_id:
        return set()
    try:
        return {get_model_integration(integration_id).provider_id}
    except ValueError:
        return set()


def _fetch_provider_account_status(
    *,
    provider_id: str,
    display_name: str,
    credential: ProviderCredential,
    active: bool,
) -> ProviderAccountStatus:
    """根据厂商能力读取余额，并把网络异常收口为安全状态。"""

    if not credential.api_key.strip() or not credential.base_url.strip():
        return ProviderAccountStatus(
            provider_id=provider_id,
            display_name=display_name,
            active=active,
            status=ACCOUNT_STATUS_NOT_CONFIGURED,
            message="尚未配置 API Key",
        )
    if provider_id != "deepseek":
        return ProviderAccountStatus(
            provider_id=provider_id,
            display_name=display_name,
            active=active,
            status=ACCOUNT_STATUS_UNSUPPORTED,
            message="该厂商暂不提供余额查询接口",
        )

    try:
        balance, currency = _fetch_deepseek_balance(credential)
    except httpx.HTTPStatusError as exception:
        status = ACCOUNT_STATUS_ERROR
        message = "API Key 无法读取余额" if exception.response.status_code in {401, 403} else "余额服务暂时不可用"
    except ValueError as exception:
        if str(exception) == "自定义 DeepSeek 端点不支持余额查询":
            status = ACCOUNT_STATUS_UNSUPPORTED
            message = "自定义端点暂不支持余额查询"
        else:
            status = ACCOUNT_STATUS_ERROR
            message = "余额服务暂时不可用"
    except (httpx.HTTPError, TypeError, KeyError):
        status = ACCOUNT_STATUS_ERROR
        message = "余额服务暂时不可用"
    except Exception:
        # 账户查询只服务右栏展示；第三方 SDK 或响应库的未知异常不能让整个接口变成 500。
        status = ACCOUNT_STATUS_ERROR
        message = "余额服务暂时不可用"
    else:
        return ProviderAccountStatus(
            provider_id=provider_id,
            display_name=display_name,
            active=active,
            status=ACCOUNT_STATUS_AVAILABLE,
            balance=balance,
            currency=currency,
            fetched_at=datetime.now(UTC).isoformat(),
        )
    return ProviderAccountStatus(
        provider_id=provider_id,
        display_name=display_name,
        active=active,
        status=status,
        fetched_at=datetime.now(UTC).isoformat(),
        message=message,
    )


def _fetch_deepseek_balance(credential: ProviderCredential) -> tuple[str, str]:
    """读取 DeepSeek 账户余额并校验最小响应结构。"""

    parsed_url = urlparse(credential.base_url)
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
        raise ValueError("DeepSeek base_url 无效")
    if parsed_url.hostname != "api.deepseek.com":
        raise ValueError("自定义 DeepSeek 端点不支持余额查询")

    with httpx.Client(timeout=httpx.Timeout(8.0, connect=3.0)) as client:
        response = client.get(
            "https://api.deepseek.com/user/balance",
            headers={"Authorization": f"Bearer {credential.api_key}"},
        )
        response.raise_for_status()
        payload: Any = response.json()

    if not isinstance(payload, dict):
        raise ValueError("DeepSeek 余额响应不可用")
    balance_infos = payload.get("balance_infos")
    if not isinstance(balance_infos, list) or not balance_infos:
        raise ValueError("DeepSeek 余额响应缺少余额项")
    first_balance = balance_infos[0]
    if not isinstance(first_balance, dict):
        raise ValueError("DeepSeek 余额项格式错误")
    balance = first_balance.get("total_balance")
    currency = first_balance.get("currency")
    if not isinstance(balance, (str, int, float)) or isinstance(balance, bool):
        raise ValueError("DeepSeek 余额值格式错误")
    normalized_balance = str(balance).strip()
    if len(normalized_balance) > 64:
        raise ValueError("DeepSeek 余额值过长")
    try:
        parsed_balance = Decimal(normalized_balance)
    except (InvalidOperation, ValueError):
        raise ValueError("DeepSeek 余额值格式错误") from None
    if not parsed_balance.is_finite():
        raise ValueError("DeepSeek 余额值格式错误")
    if not isinstance(currency, str) or not _CURRENCY_PATTERN.fullmatch(currency.strip()):
        raise ValueError("DeepSeek 币种格式错误")
    return normalized_balance, currency.strip().upper()
