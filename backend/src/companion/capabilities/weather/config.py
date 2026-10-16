from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit


def normalize_qweather_api_host(api_host: str) -> str:
    """归一化和风天气 API Host。

    Args:
        api_host: 用户从和风控制台复制的 API Host，允许不带协议。

    Returns:
        去除尾随斜杠且带 https 协议的 API Host；空字符串返回空字符串。
    """

    normalized_api_host = api_host.strip().rstrip("/")
    if not normalized_api_host:
        return ""
    if normalized_api_host.lower().startswith(("http://", "https://")):
        return normalized_api_host
    return f"https://{normalized_api_host}"


def validate_qweather_api_host(api_host: str) -> str:
    """校验用户提交的和风天气 API Host，并返回安全的规范值。

    这里只校验 URL 结构，不强行限制域名后缀。这样既能接受和风控制台
    分配的自定义 Host，也不会把企业代理或测试环境误判成非法；真正的可达性
    和 API 权限仍由联网验证负责。

    Args:
        api_host: 用户填写或从和风控制台复制的 API Host，可省略协议和尾斜杠。

    Returns:
        带 ``https://``、不带路径和查询串的 Host。

    Raises:
        ValueError: Host 为空、不是 HTTPS 主机，或混入了路径、凭据等内容。
    """

    normalized_api_host = normalize_qweather_api_host(api_host)
    if not normalized_api_host:
        raise ValueError("和风天气 API Host 不能为空")
    if any(character.isspace() or ord(character) < 32 for character in normalized_api_host):
        raise ValueError("和风天气 API Host 不能包含空白或控制字符")

    try:
        parsed = urlsplit(normalized_api_host)
        hostname = parsed.hostname
        # 访问 port 会触发 urlsplit 对非法端口的解析校验。
        port = parsed.port
    except ValueError as exception:
        raise ValueError("和风天气 API Host 格式无效") from exception

    if parsed.scheme.lower() != "https":
        raise ValueError("和风天气 API Host 必须使用 HTTPS")
    if not hostname:
        raise ValueError("和风天气 API Host 必须包含主机名")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("和风天气 API Host 不能包含用户名或密码")
    if port is not None:
        raise ValueError("和风天气 API Host 只能填写主机名，不能包含端口")
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise ValueError("和风天气 API Host 不能包含路径、查询参数或片段")

    # 重新从解析后的网络位置构造，避免保留用户输入中的尾斜杠或其它 URL 部分；
    # 使用 netloc 还能正确保留合法 IPv6 主机名的方括号。
    return f"https://{parsed.netloc}"


def is_valid_qweather_api_host(api_host: str) -> bool:
    """判断持久化的天气 Host 是否仍符合可连接的 URL 结构。

    Args:
        api_host: 已保存的天气 API Host。

    Returns:
        结构合法时返回 True；历史坏值只返回 False，不让启动流程崩溃。
    """

    try:
        validate_qweather_api_host(api_host)
    except ValueError:
        return False
    return True


@dataclass(frozen=True)
class QWeatherConfig:
    """保存和风天气调用所需的运行时配置。

    Args:
        api_host: 和风天气 API 主机地址。
        api_key: API KEY 模式使用的密钥。
        timeout_s: 请求超时秒数。
    """

    api_host: str = ""
    api_key: str = ""
    timeout_s: float = 5.0

    @property
    def normalized_api_host(self) -> str:
        """返回可直接交给 HTTP 客户端使用的 API Host。"""

        return normalize_qweather_api_host(self.api_host)

    @classmethod
    def from_settings(cls, settings: object) -> QWeatherConfig:
        """从应用 Settings 快照创建和风天气配置。

        Args:
            settings: 具备 qweather_* 字段的配置对象。

        Returns:
            标准化后的和风天气配置。
        """

        return cls(
            api_host=normalize_qweather_api_host(str(getattr(settings, "qweather_api_host", cls.api_host))),
            api_key=str(getattr(settings, "qweather_api_key", "")).strip(),
            timeout_s=float(getattr(settings, "qweather_timeout_s", 5.0)),
        )

    @property
    def is_ready(self) -> bool:
        """判断当前配置是否足以发起真实和风天气请求。

        Returns:
            配置完整时返回 True，否则返回 False。
        """

        return bool(is_valid_qweather_api_host(self.api_host) and self.api_key)
