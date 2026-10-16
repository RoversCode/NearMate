from __future__ import annotations

import json
from typing import Any

import httpx

from companion.capabilities.weather.auth import QWeatherAuthProvider
from companion.capabilities.weather.config import QWeatherConfig


class QWeatherApiError(RuntimeError):
    """携带稳定失败分类的和风天气预期错误。

    Args:
        reason: 供业务逻辑和监控判断的稳定原因码。
        retryable: 稍后重试是否可能恢复。
        safe_message: 可以安全交给用户和 Foreground LLM 的短消息。
        internal_detail: 只供异常链诊断的底层细节。
    """

    def __init__(
        self,
        *,
        reason: str,
        retryable: bool,
        safe_message: str,
        internal_detail: str = "",
    ) -> None:
        super().__init__(internal_detail or safe_message)
        self.reason = reason
        self.retryable = retryable
        self.safe_message = safe_message
        self.internal_detail = internal_detail


class QWeatherClient:
    """封装和风天气 HTTP 请求细节。"""

    def __init__(
        self,
        *,
        config: QWeatherConfig,
        auth_provider: QWeatherAuthProvider,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        """初始化和风天气客户端。

        Args:
            config: 和风天气运行时配置。
            auth_provider: 请求认证策略。
            transport: 可选 httpx 传输层，主要用于测试注入。
        """

        self._config = config
        self._auth_provider = auth_provider
        self._client = httpx.Client(
            base_url=config.normalized_api_host,
            timeout=config.timeout_s,
            transport=transport,
        )

    def close(self) -> None:
        """释放 HTTP 连接池。

        临时的密钥验证客户端在一次请求后必须关闭；常驻天气服务仍可在
        应用生命周期结束时由其拥有者调用此方法。
        """

        self._client.close()

    @staticmethod
    def _format_http_status_error(exception: httpx.HTTPStatusError) -> str:
        """格式化 HTTP 状态码异常，保留和风返回的业务诊断信息。

        Args:
            exception: httpx 抛出的 HTTP 状态码异常。

        Returns:
            适合展示给调用方的错误消息。
        """

        response = exception.response
        detail = response.text.strip()
        try:
            payload = response.json()
        except ValueError:
            payload = None

        if isinstance(payload, dict):
            # 保留和风错误字段，方便判断 Host、Key、订阅或安全限制问题。
            detail = json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
            )

        if detail:
            return f"QWeather request failed: HTTP {response.status_code}: {detail}"
        return f"QWeather request failed: HTTP {response.status_code}"

    def get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        require_business_code: bool = True,
    ) -> dict[str, Any]:
        """发送 GET 请求并返回和风 JSON 载荷。

        Args:
            path: API 路径。
            params: 业务查询参数。
            require_business_code: 是否按旧版天气接口校验顶层业务码。

        Returns:
            和风天气响应 JSON 对象。

        Raises:
            QWeatherApiError: 网络、HTTP 状态码或业务码异常。
        """

        query_params = {
            key: str(value)
            for key, value in (params or {}).items()
            if value is not None
        }
        headers: dict[str, str] = {}
        self._auth_provider.apply(params=query_params, headers=headers)

        try:
            response = self._client.get(path, params=query_params, headers=headers)
            response.raise_for_status()
            payload = response.json()
        except httpx.ConnectTimeout as exception:
            raise QWeatherApiError(
                reason="connect_timeout",
                retryable=True,
                safe_message="暂时无法连接天气服务，请稍后再试。",
                internal_detail=str(exception),
            ) from exception
        except httpx.TimeoutException as exception:
            raise QWeatherApiError(
                reason="request_timeout",
                retryable=True,
                safe_message="天气服务响应超时，请稍后再试。",
                internal_detail=str(exception),
            ) from exception
        except httpx.ProxyError as exception:
            raise QWeatherApiError(
                reason="proxy_error",
                retryable=True,
                safe_message="天气服务代理连接失败，请检查系统代理后重试。",
                internal_detail=str(exception),
            ) from exception
        except httpx.ConnectError as exception:
            detail = str(exception).lower()
            if "ssl" in detail or "eof" in detail:
                reason = "tls_connection_failed"
                safe_message = "天气服务 TLS 连接失败，请检查 API Host、系统代理和网络后重试。"
            else:
                reason = "connection_failed"
                safe_message = "暂时无法连接天气服务，请稍后再试。"
            raise QWeatherApiError(
                reason=reason,
                retryable=True,
                safe_message=safe_message,
                internal_detail=str(exception),
            ) from exception
        except httpx.InvalidURL as exception:
            raise QWeatherApiError(
                reason="invalid_host",
                retryable=False,
                safe_message="天气服务 API Host 格式无效，请检查后重试。",
                internal_detail=str(exception),
            ) from exception
        except httpx.HTTPStatusError as exception:
            message = self._format_http_status_error(exception)
            status_code = exception.response.status_code
            if status_code in {401, 403}:
                reason = "authentication_failed"
                retryable = False
                safe_message = "天气服务认证失败，请检查服务配置。"
            elif status_code == 429:
                reason = "rate_limited"
                retryable = True
                safe_message = "天气服务请求过于频繁，请稍后再试。"
            elif status_code in {408, 425} or status_code >= 500:
                reason = "upstream_unavailable"
                safe_message = "天气服务暂时不可用，请稍后再试。"
                retryable = True
            else:
                reason = "http_error"
                retryable = False
                safe_message = "天气服务拒绝了本次查询。"
            raise QWeatherApiError(
                reason=reason,
                retryable=retryable,
                safe_message=safe_message,
                internal_detail=message,
            ) from exception
        except httpx.HTTPError as exception:
            raise QWeatherApiError(
                reason="network_error",
                retryable=True,
                safe_message="天气服务网络异常，请稍后再试。",
                internal_detail=str(exception),
            ) from exception
        except ValueError as exception:
            raise QWeatherApiError(
                reason="invalid_response",
                retryable=False,
                safe_message="天气服务返回了无法识别的数据。",
                internal_detail=str(exception),
            ) from exception

        if not isinstance(payload, dict):
            raise QWeatherApiError(
                reason="invalid_response",
                retryable=False,
                safe_message="天气服务返回了无法识别的数据。",
                internal_detail="QWeather returned non-object JSON payload",
            )

        if not require_business_code:
            return payload

        if "code" not in payload:
            raise QWeatherApiError(
                reason="invalid_response",
                retryable=False,
                safe_message="天气服务返回了无法识别的数据。",
                internal_detail="QWeather returned missing business code",
            )

        code = str(payload.get("code", "")).strip()
        if not code:
            raise QWeatherApiError(
                reason="invalid_response",
                retryable=False,
                safe_message="天气服务返回了无法识别的数据。",
                internal_detail="QWeather returned invalid business code",
            )
        if code != "200":
            message = str(payload.get("message", "")).strip()
            detail = f": {message}" if message else ""
            if code == "429":
                reason = "rate_limited"
                retryable = True
                safe_message = "天气服务请求过于频繁，请稍后再试。"
            elif code.startswith("5"):
                reason = "upstream_unavailable"
                retryable = True
                safe_message = "天气服务暂时不可用，请稍后再试。"
            elif code in {"401", "403"}:
                reason = "authentication_failed"
                retryable = False
                safe_message = "天气服务认证失败，请检查服务配置。"
            else:
                reason = "business_error"
                retryable = False
                safe_message = "天气服务未能完成本次查询。"
            raise QWeatherApiError(
                reason=reason,
                retryable=retryable,
                safe_message=safe_message,
                internal_detail=f"QWeather returned business code {code}{detail}",
            )

        return payload
