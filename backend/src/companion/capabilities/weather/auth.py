from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class QWeatherAuthProvider(Protocol):
    """定义和风天气请求认证策略。"""

    def apply(self, *, params: dict[str, str], headers: dict[str, str]) -> None:
        """把认证信息写入请求参数或请求头。

        Args:
            params: 即将发送的查询参数字典。
            headers: 即将发送的请求头字典。

        Returns:
            无返回值；认证信息会原地写入传入字典。
        """


@dataclass(frozen=True)
class ApiKeyAuthProvider:
    """使用 API KEY 请求头完成和风天气认证。"""

    api_key: str

    def apply(self, *, params: dict[str, str], headers: dict[str, str]) -> None:
        """把 API KEY 写入请求头。

        Args:
            params: 即将发送的查询参数字典。
            headers: 即将发送的请求头字典。

        Returns:
            无返回值。
        """

        _ = params
        headers["X-QW-Api-Key"] = self.api_key


class MissingAuthProvider:
    """空认证策略，用于缺配置时保持调用对象可构造。"""

    def apply(self, *, params: dict[str, str], headers: dict[str, str]) -> None:
        """不写入任何认证信息。

        Args:
            params: 即将发送的查询参数字典。
            headers: 即将发送的请求头字典。

        Returns:
            无返回值。
        """

        _ = params
        _ = headers
