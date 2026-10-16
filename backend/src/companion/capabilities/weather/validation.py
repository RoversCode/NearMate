from __future__ import annotations

from companion.capabilities.weather.auth import ApiKeyAuthProvider
from companion.capabilities.weather.client import QWeatherApiError, QWeatherClient
from companion.capabilities.weather.config import QWeatherConfig, validate_qweather_api_host


class QWeatherValidationError(ValueError):
    """携带联网验证分类的天气配置错误。

    ``ValueError`` 兼容旧的直接调用方，同时保留足够信息让应用层区分
    用户配置错误和外部天气服务暂时不可达，避免所有失败都被映射成 HTTP 400。
    """

    def __init__(self, *, reason: str, retryable: bool, safe_message: str) -> None:
        super().__init__(safe_message)
        self.reason = reason
        self.retryable = retryable
        self.safe_message = safe_message


def validate_qweather_api_key(*, settings: object, api_key: str, api_host: str) -> None:
    """验证和风天气 API Key 是否可用于真实请求。

    Args:
        settings: 当前应用设置，提供和风主机与超时配置。
        api_key: 用户从控制台提交的候选 API Key。
        api_host: 用户从和风控制台复制的 API Host。

    Returns:
        验证成功时不返回值。

    Raises:
        ValueError: API Key 为空或 Host 结构无效。
        QWeatherValidationError: 和风天气联网验证失败，携带稳定失败分类。
    """

    normalized_api_key = api_key.strip()
    if not normalized_api_key:
        raise ValueError("和风天气 API Key 不能为空")
    normalized_api_host = validate_qweather_api_host(api_host)

    current_config = QWeatherConfig.from_settings(settings)
    config = QWeatherConfig(
        api_host=normalized_api_host,
        api_key=normalized_api_key,
        timeout_s=current_config.timeout_s,
    )
    client = QWeatherClient(
        config=config,
        auth_provider=ApiKeyAuthProvider(normalized_api_key),
    )

    try:
        # 验证天气 Key 时直接命中实时天气接口，避免 GeoAPI 权限差异造成有效 Key 被误拒。
        client.get("/v7/weather/now", {"location": "101010100", "lang": "zh", "unit": "m"})
    except QWeatherApiError as exception:
        raise QWeatherValidationError(
            reason=exception.reason,
            retryable=exception.retryable,
            safe_message=f"和风天气 API Key 无法通过验证：{exception.safe_message}",
        ) from exception
    finally:
        # 密钥验证是一次性请求，及时关闭连接池，避免用户反复保存时累积连接。
        client.close()

