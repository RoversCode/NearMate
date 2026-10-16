from __future__ import annotations

import json

import pytest

from companion.foreground_tools.cognitive.result_protocol import (
    MAX_COGNITIVE_FAILURE_REASON_CHARS,
    MAX_COGNITIVE_RESULT_CHARS,
    MAX_COGNITIVE_SUMMARY_CHARS,
    CognitiveResultProtocolError,
    parse_cognitive_result,
)


@pytest.mark.parametrize(
    ("payload", "succeeded"),
    [
        (
            {
                "result": "完整结果",
                "summary": "结果摘要",
            },
            True,
        ),
        (
            {"failure_reason": "证据不足，任务没有完整完成。"},
            False,
        ),
    ],
)
def test_protocol_accepts_exact_success_and_failure_shapes(
    payload: dict[str, object],
    succeeded: bool,
) -> None:
    """二分支合同只接受完整成功和明确失败两种对象。

    Args:
        payload: 待校验的二分支对象。
        succeeded: 预期的业务成功标记。
    """

    result = parse_cognitive_result(json.dumps(payload, ensure_ascii=False))

    assert result.succeeded is succeeded
    assert result.model_dump() == payload


@pytest.mark.parametrize(
    "raw_output",
    [
        "not-json",
        "\u0060\u0060\u0060json\n{\"result\":\"结果\"}\n\u0060\u0060\u0060",
        '{"result":"结果","summary":"摘要"}残文',
        '{"result":"结果","summary":"摘要"}'
        '{"result":"第二个","summary":"摘要"}',
        "[]",
    ],
)
def test_syntax_object_and_residue_errors_are_repairable(raw_output: str) -> None:
    """非空且未超长的语法、围栏、残文和对象类型错误允许修复一次。

    Args:
        raw_output: Plus 返回的错误正文。
    """

    with pytest.raises(CognitiveResultProtocolError) as captured:
        parse_cognitive_result(raw_output)

    assert captured.value.repairable is True


@pytest.mark.parametrize(
    "payload",
    [
        {"result": "结果", "summary": "摘要", "extra": "禁止"},
        {"result": 1, "summary": "摘要"},
        {"result": "结果", "summary": False},
        {"result": " ", "summary": "摘要"},
        {"result": "结果", "summary": " "},
        {"failure_reason": " "},
        {"result": "结果"},
        {"summary": "摘要"},
        {"result": "结果", "failure_reason": "失败"},
        {"result": "结果", "summary": "摘要", "failure_reason": "又失败"},
        {},
    ],
)
def test_schema_type_blank_and_exclusion_errors_are_repairable(
    payload: dict[str, object],
) -> None:
    """字段缺失、多余、类型、空白和混合错误都允许 Flash 整理一次。

    Args:
        payload: 字段层面违反二分支合同的对象。
    """

    with pytest.raises(CognitiveResultProtocolError) as captured:
        parse_cognitive_result(json.dumps(payload, ensure_ascii=False))

    assert captured.value.repairable is True


@pytest.mark.parametrize(
    "payload",
    [
        {
            "result": "x" * (MAX_COGNITIVE_RESULT_CHARS + 1),
            "summary": "摘要",
        },
        {
            "result": "结果",
            "summary": "x" * (MAX_COGNITIVE_SUMMARY_CHARS + 1),
        },
        {
            "failure_reason": "x" * (MAX_COGNITIVE_FAILURE_REASON_CHARS + 1),
        },
    ],
    ids=["result_too_long", "summary_too_long", "failure_reason_too_long"],
)
def test_field_length_errors_never_pass_local_protocol(
    payload: dict[str, object],
) -> None:
    """结果、摘要和失败说明都保留各自的现有容量门禁。

    Args:
        payload: 包含超长字段的对象。
    """

    with pytest.raises(CognitiveResultProtocolError):
        parse_cognitive_result(json.dumps(payload, ensure_ascii=False))


def test_blank_primary_body_skips_repair() -> None:
    """空白正文不能继续复制给 Flash。"""

    for raw_output in ("", "   "):
        with pytest.raises(CognitiveResultProtocolError) as captured:
            parse_cognitive_result(raw_output)
        assert captured.value.repairable is False


def test_oversized_primary_body_skips_repair() -> None:
    """超过主结果容量的正文不能继续复制给 Flash。"""

    with pytest.raises(CognitiveResultProtocolError) as captured:
        parse_cognitive_result("x" * (MAX_COGNITIVE_RESULT_CHARS + 1))

    assert captured.value.repairable is False


@pytest.mark.parametrize(
    "unsafe_reason",
    [
        "Traceback (most recent call last): provider failed",
        r"读取 C:\Users\someone\secret.txt 时失败",
        "读取 /home/someone/private/secret.txt 时失败",
        "api_key=sk-secret-value",
        "Bearer abcdefghijklmnop",
    ],
)
def test_failure_reason_rejects_diagnostics_and_secrets(
    unsafe_reason: str,
) -> None:
    """失败说明不能把堆栈、绝对路径或凭据带入恢复与任务记忆。

    Args:
        unsafe_reason: 不允许跨越 Cognitive 协议边界的诊断文本。
    """

    with pytest.raises(CognitiveResultProtocolError):
        parse_cognitive_result(
            json.dumps(
                {"failure_reason": unsafe_reason},
                ensure_ascii=False,
            )
        )


def test_protocol_error_never_exposes_raw_output() -> None:
    """协议异常对象只暴露稳定码，不能保存或复述供应商原始响应。"""

    secret_raw_output = (
        "\u0060\u0060\u0060json\n"
        "{\"api_key\":\"secret-provider-response\"}\n"
        "\u0060\u0060\u0060"
    )

    with pytest.raises(CognitiveResultProtocolError) as captured:
        parse_cognitive_result(secret_raw_output)

    assert captured.value.failure_code == "cognitive_result_protocol_failed"
    assert "secret-provider-response" not in str(captured.value)
    assert not hasattr(captured.value, "raw_output")
