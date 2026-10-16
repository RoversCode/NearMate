from __future__ import annotations

import logging

import httpx
import pytest

from companion.capabilities.weather.auth import ApiKeyAuthProvider
from companion.capabilities.weather.client import QWeatherApiError, QWeatherClient
from companion.capabilities.weather.config import QWeatherConfig, validate_qweather_api_host
from companion.capabilities.weather.models import LocationCandidate, LocationResolution
from companion.capabilities.weather.service import WeatherService
from companion.capabilities.weather.validation import QWeatherValidationError, validate_qweather_api_key


class MatchedLocationResolver:
    """固定返回厦门地点，隔离本测试与城市表文件。"""

    def resolve(self, *_: object, **__: object) -> LocationResolution:
        """返回唯一命中的厦门地点。"""

        return LocationResolution.matched(
            LocationCandidate(
                location_id="101230201",
                name_zh="厦门",
                latitude=24.48,
                longitude=118.08,
            )
        )


def test_qweather_connect_timeout_becomes_typed_safe_error() -> None:
    """TLS 连接超时应转换成可重试错误码，不能暴露底层实现文本。"""

    def timeout_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("_ssl.c:993 handshake timed out", request=request)

    client = QWeatherClient(
        config=QWeatherConfig(api_host="https://weather.example", api_key="secret"),
        auth_provider=ApiKeyAuthProvider("secret"),
        transport=httpx.MockTransport(timeout_handler),
    )

    try:
        client.get("/v7/weather/now")
    except QWeatherApiError as exception:
        assert exception.reason == "connect_timeout"
        assert exception.retryable is True
        assert exception.safe_message == "暂时无法连接天气服务，请稍后再试。"
        assert "_ssl.c" not in exception.safe_message
    else:
        raise AssertionError("连接超时必须抛出 QWeatherApiError")


@pytest.mark.parametrize(
    ("status_code", "expected_reason", "expected_retryable"),
    [
        (400, "http_error", False),
        (403, "authentication_failed", False),
        (503, "upstream_unavailable", True),
    ],
)
def test_qweather_http_status_preserves_configuration_vs_upstream_classification(
    status_code: int,
    expected_reason: str,
    expected_retryable: bool,
) -> None:
    """上游 HTTP 400 与网络/服务端故障必须保留不同的重试语义。"""

    def status_handler(request: httpx.Request) -> httpx.Response:
        _ = request
        return httpx.Response(
            status_code,
            json={"error": {"status": status_code, "detail": "diagnostic"}},
        )

    client = QWeatherClient(
        config=QWeatherConfig(api_host="https://weather.example", api_key="secret"),
        auth_provider=ApiKeyAuthProvider("secret"),
        transport=httpx.MockTransport(status_handler),
    )

    with pytest.raises(QWeatherApiError) as raised:
        client.get("/v7/weather/now")

    assert raised.value.reason == expected_reason
    assert raised.value.retryable is expected_retryable


@pytest.mark.parametrize(
    "invalid_host",
    [
        "https://weather.example/v7",
        "https://weather.example?location=101010100",
        "https://user:secret@weather.example",
        "http://weather.example",
        "https://weather.example:8443",
    ],
)
def test_qweather_host_validation_rejects_values_that_are_not_an_api_host(invalid_host: str) -> None:
    """Host 输入只允许 HTTPS 主机，不能把路径、查询串或凭据带入请求基址。"""

    with pytest.raises(ValueError):
        validate_qweather_api_host(invalid_host)


def test_qweather_host_validation_accepts_bare_host_and_trailing_slash() -> None:
    """用户从控制台复制不带协议或带尾斜杠的 Host 时仍能得到稳定基址。"""

    assert validate_qweather_api_host("  abc.qweatherapi.com/ ") == "https://abc.qweatherapi.com"


def test_qweather_validation_keeps_network_failure_typed_for_http_layer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """联网校验失败不能降级成普通 ValueError，否则路由会错误返回 400。"""

    def connect_error(*_: object, **__: object) -> dict[str, object]:
        raise QWeatherApiError(
            reason="connection_failed",
            retryable=True,
            safe_message="暂时无法连接天气服务，请稍后再试。",
        )

    monkeypatch.setattr(QWeatherClient, "get", connect_error)
    with pytest.raises(QWeatherValidationError) as raised:
        validate_qweather_api_key(
            settings=QWeatherConfig(api_host="https://weather.example"),
            api_key="secret",
            api_host="weather.example",
        )

    assert raised.value.reason == "connection_failed"
    assert raised.value.retryable is True
    assert "暂时无法连接" in raised.value.safe_message


def test_weather_expected_failure_returns_structured_payload_without_traceback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """预期网络失败只记一条 warning，并返回 Foreground 可解释的失败载荷。"""

    class FailingClient:
        def __init__(self) -> None:
            self.call_count = 0

        def get(self, *_: object, **__: object) -> dict[str, object]:
            self.call_count += 1
            raise QWeatherApiError(
                reason="connect_timeout",
                retryable=True,
                safe_message="暂时无法连接天气服务，请稍后再试。",
                internal_detail="_ssl.c:993 handshake timed out",
            )

    client = FailingClient()
    service = WeatherService(
        config=QWeatherConfig(api_host="https://weather.example", api_key="secret"),
        client=client,
        resolver=MatchedLocationResolver(),
    )
    caplog.set_level(logging.WARNING, logger="companion.capabilities.weather.service")

    payload = service.overview(location="厦门", country="CN").to_tool_payload()

    assert payload == {
        "ok": False,
        "state": "failed",
        "reason": "weather_connect_timeout",
        "retryable": True,
        "text": "暂时无法连接天气服务，请稍后再试。",
        "data": {},
        "source": "qweather",
        "quality": "empty",
        "warnings": [],
        "resolved_location": {
            "location_id": "101230201",
            "name_zh": "厦门",
            "name_en": "",
            "country_code": "",
            "country_name_zh": "",
            "adm1_zh": "",
            "adm2_zh": "",
            "timezone": "",
            "latitude": 24.48,
            "longitude": 118.08,
        },
    }
    records = [
        record
        for record in caplog.records
        if getattr(record, "event_name", "") == "weather_request_failed"
    ]
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    assert records[0].exc_info is None
    assert records[0].structured_fields["reason"] == "connect_timeout"
    assert "_ssl.c" not in records[0].getMessage()
    assert client.call_count == 1
