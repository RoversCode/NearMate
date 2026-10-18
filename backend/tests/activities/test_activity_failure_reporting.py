from __future__ import annotations

import httpx
import openai
import pytest

from companion.activities.runtime.failure_reporting import (
    FailureDiagnostics,
    failure_from_exception,
    failure_from_reason,
    parse_activity_failure,
    runtime_interruption_failure,
)
from companion.activities.runtime.models import ACTIVITY_FAILURE_CODES


def _request() -> httpx.Request:
    return httpx.Request("POST", "https://provider.example/v1/chat/completions")


def _status_error(status: int, message: str = "provider error") -> openai.APIStatusError:
    response = httpx.Response(
        status,
        request=_request(),
        headers={"x-request-id": "req-test-1"},
    )
    return openai.APIStatusError(message, response=response, body=None)


def test_every_failure_code_has_user_copy() -> None:
    """所有稳定错误码都必须有摘要与行动建议，不允许漏配文案。"""

    for code in sorted(ACTIVITY_FAILURE_CODES):
        exc = RuntimeError("boom")
        exc.activity_failure_code = code  # type: ignore[attr-defined]
        failure = failure_from_exception(exc, stage_code="processing")
        assert failure.code == code
        assert failure.summary.strip()
        assert failure.suggested_action.strip()


def test_known_structured_reasons_map_to_stable_codes() -> None:
    cases = {
        "model_not_configured": "configuration_invalid",
        "material_changed_or_unreadable": "material_unavailable",
        "no_processable_material": "material_unavailable",
        "material_workload_exceeded": "material_unavailable",
        "empty_model_output": "invalid_provider_response",
        "invalid_provider_response": "invalid_provider_response",
        "agent_max_turns": "agent_max_turns",
        "agent_execution_error": "agent_execution_error",
        "agent_cli_crash": "agent_cli_crash",
    }
    for reason, expected_code in cases.items():
        failure = failure_from_reason(reason, stage_code="loading_materials")
        assert failure.code == expected_code, reason
        assert failure.stage_code == "loading_materials"
        assert failure.stage_label == "读取材料"
        assert failure.summary.strip()
        assert failure.suggested_action.strip()


def test_unknown_structured_reason_falls_back_to_unclassified() -> None:
    failure = failure_from_reason("something_new", stage_code="processing")

    assert failure.code == "unclassified_error"
    assert "something_new" not in failure.technical_detail
    assert "尚未分类" in failure.summary


def test_openai_and_httpx_exceptions_map_to_stable_codes() -> None:
    cases: list[tuple[BaseException, str]] = [
        (openai.AuthenticationError("bad key", response=httpx.Response(401, request=_request()), body=None), "authentication_failed"),
        (openai.PermissionDeniedError("forbidden", response=httpx.Response(403, request=_request()), body=None), "authentication_failed"),
        (openai.RateLimitError("slow down", response=httpx.Response(429, request=_request()), body=None), "rate_limited"),
        (_status_error(500), "provider_unavailable"),
        (_status_error(503), "provider_unavailable"),
        (_status_error(400), "invalid_provider_response"),
        (_status_error(422), "invalid_provider_response"),
        (openai.APITimeoutError(request=_request()), "request_timed_out"),
        (openai.APIConnectionError(message="Connection error.", request=_request()), "network_unavailable"),
        (httpx.ConnectError("connect failed"), "network_unavailable"),
        (httpx.ReadTimeout("read timed out"), "request_timed_out"),
        (httpx.RemoteProtocolError("peer closed connection without sending complete message body"), "provider_stream_interrupted"),
    ]
    for exc, expected_code in cases:
        failure = failure_from_exception(exc, stage_code="processing")
        assert failure.code == expected_code, f"{type(exc).__name__} -> {failure.code}"


def test_explicit_provider_error_type_wins_over_guessing() -> None:
    class ProviderStreamFailed(RuntimeError):
        activity_failure_code = "provider_stream_interrupted"

    failure = failure_from_exception(
        ProviderStreamFailed("stream dropped"),
        stage_code="generating_result",
    )

    assert failure.code == "provider_stream_interrupted"
    assert "error_type=ProviderStreamFailed" in failure.technical_detail


def test_unknown_exception_degrades_to_unclassified_with_type_only() -> None:
    class WeirdBackendError(Exception):
        pass

    failure = failure_from_exception(
        WeirdBackendError("odd\nmultiline payload with secrets api_key=sk-1234567890abcdef"),
        stage_code="processing",
    )

    assert failure.code == "unclassified_error"
    # 原始异常正文无条件不进入公开报告，只保留结构化异常类型。
    assert failure.technical_detail == "error_type=WeirdBackendError"


def test_http_status_and_request_id_enter_technical_detail() -> None:
    failure = failure_from_exception(_status_error(503), stage_code="processing")

    assert "error_type=APIStatusError" in failure.technical_detail
    assert "http_status=503" in failure.technical_detail
    assert "provider_request_id=req-test-1" in failure.technical_detail


def test_sensitive_material_never_enters_public_failure() -> None:
    secret_message = (
        "Authorization: Bearer sk-abc123def456ghi789 failed for "
        "C:\\Users\\fixture-user\\secret\\config.json"
    )
    failure = failure_from_exception(RuntimeError(secret_message), stage_code="processing")

    public_text = failure.summary + failure.technical_detail + failure.suggested_action
    assert "sk-abc123def456ghi789" not in public_text
    assert "Bearer" not in public_text
    assert "C:\\Users\\fixture-user" not in public_text
    # 原始异常正文无条件不进入公开报告，只保留结构化异常类型。
    assert failure.technical_detail == "error_type=RuntimeError"


def test_request_body_like_message_is_dropped() -> None:
    body_message = 'bad request: {"messages": [{"role": "system", "content": "prompt"}]}'

    failure = failure_from_exception(RuntimeError(body_message), stage_code="processing")

    assert "prompt" not in failure.technical_detail
    assert failure.technical_detail == "error_type=RuntimeError"


def test_invalid_provider_identifiers_are_dropped_without_exposing_message() -> None:
    """非法 Provider 标识符与异常正文都不得进入公共故障报告。"""

    class ProviderError(RuntimeError):
        provider_code = "provider code with spaces and prompt"
        request_id = "C:\\Users\\fixture-user\\secret\\config.json"

    failure = failure_from_exception(
        ProviderError("system prompt and api_key=sk-secret"),
        stage_code="processing",
    )

    assert failure.technical_detail == "error_type=ProviderError"


def test_unknown_exception_cannot_smuggle_provider_diagnostics_by_attribute_name() -> None:
    """只有受信任 Provider 异常类型才能提供结构化 Provider 诊断字段。"""

    class InternalWorkflowError(RuntimeError):
        status_code = 503
        request_id = "system_prompt"
        provider_code = "internal_rule"

    failure = failure_from_exception(
        InternalWorkflowError("ignored"),
        stage_code="processing",
    )

    assert failure.technical_detail == "error_type=InternalWorkflowError"


def test_secret_shaped_provider_identifiers_are_never_public() -> None:
    """结构化字段也必须拒绝常见密钥外形，不能依赖字段名天然安全。"""

    class ProviderError(RuntimeError):
        provider_code = "AIzaSyDUMMYKEYVALUE1234567890"
        request_id = "sk-proj-secretvalue"

    failure = failure_from_exception(ProviderError("ignored"), stage_code="processing")

    assert failure.technical_detail == "error_type=ProviderError"
    with pytest.raises(ValueError):
        FailureDiagnostics(provider_request_id="sk-proj-secretvalue")


def test_structured_diagnostics_render_in_fixed_order() -> None:
    failure = failure_from_reason(
        "empty_model_output",
        stage_code="generating_result",
        diagnostics=FailureDiagnostics(
            error_type="ProviderStreamFailed",
            http_status=503,
            provider_request_id="req-1",
            provider_error_code="server_error",
            material_count=2,
        ),
    )

    assert failure.technical_detail == (
        "error_type=ProviderStreamFailed; http_status=503; "
        "provider_request_id=req-1; provider_error_code=server_error; material_count=2"
    )


def test_runtime_interruption_failures_have_distinct_copy() -> None:
    reconfigured = runtime_interruption_failure(
        "runtime_reconfigured", stage_code="processing"
    )
    interrupted = runtime_interruption_failure(
        "runtime_interrupted", stage_code="delivering_result"
    )

    assert reconfigured.code == "runtime_reconfigured"
    assert "配置已更新" in reconfigured.summary
    assert interrupted.code == "runtime_interrupted"
    assert "意外中断" in interrupted.summary
    assert interrupted.stage_label == "交付结果"
    with pytest.raises(ValueError):
        runtime_interruption_failure("unclassified_error", stage_code="processing")  # type: ignore[arg-type]


def test_illegal_stage_code_is_rejected() -> None:
    with pytest.raises(ValueError):
        failure_from_reason("empty_model_output", stage_code="reasoning")


def test_parse_activity_failure_accepts_valid_payload() -> None:
    failure = failure_from_reason("empty_model_output", stage_code="generating_result")

    parsed = parse_activity_failure(failure.to_payload())

    assert parsed == failure


def test_parse_activity_failure_rejects_illegal_structure() -> None:
    valid = failure_from_reason("empty_model_output", stage_code="processing").to_payload()

    with pytest.raises(ValueError):
        parse_activity_failure("not-a-dict")
    with pytest.raises(ValueError):
        parse_activity_failure({**valid, "extra": True})
    with pytest.raises(ValueError):
        parse_activity_failure({**valid, "code": "some_sdk_error"})
    with pytest.raises(ValueError):
        parse_activity_failure({**valid, "stage_code": "reasoning"})
    with pytest.raises(ValueError):
        parse_activity_failure({**valid, "stage_label": "伪造文案"})
    with pytest.raises(ValueError):
        parse_activity_failure({**valid, "summary": "  "})
    with pytest.raises(ValueError):
        parse_activity_failure(
            {
                **valid,
                "summary": "内部 system prompt 执行失败。",
            }
        )
    with pytest.raises(ValueError):
        parse_activity_failure(
            {
                **valid,
                "technical_detail": (
                    "error_type=RuntimeError; prompt=C:\\Users\\user\\secret.txt"
                ),
            }
        )
    with pytest.raises(ValueError):
        parse_activity_failure(
            {
                **valid,
                "technical_detail": "provider_request_id=sk-proj-secretvalue",
            }
        )
