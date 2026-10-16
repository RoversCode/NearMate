"""解析 Cognitive Plus 返回的严格标记文本结果。"""

from __future__ import annotations

from pydantic import ValidationError

from companion.foreground_tools.cognitive.result_protocol import (
    CognitiveFailureResult,
    CognitiveResult,
    CognitiveResultProtocolError,
    CognitiveSuccessResult,
    MAX_COGNITIVE_RESULT_CHARS,
)


SUMMARY_TAG = "<summary_tag>"
FAILURE_TAG = "<failure_tag>"


def parse_marked_cognitive_result(raw_output: str) -> CognitiveResult:
    """把 Plus 标记文本校验为现有的强类型成功或失败结果。

    解析器按精确字符串识别标签，不要求模型一定把标签单独放在一行；标签重复、
    缺失、混合或分段为空都会交给 Flash 做一次有限收口。

    Args:
        raw_output: Plus 返回的完整标记文本。

    Returns:
        已通过字段长度和失败说明安全校验的最终结果对象。

    Raises:
        CognitiveResultProtocolError: 正文为空、超长或标记内容不符合协议。
    """

    if not isinstance(raw_output, str) or not raw_output.strip():
        raise CognitiveResultProtocolError(repairable=False)
    if len(raw_output) > MAX_COGNITIVE_RESULT_CHARS:
        raise CognitiveResultProtocolError(repairable=False)

    summary_count = raw_output.count(SUMMARY_TAG)
    failure_count = raw_output.count(FAILURE_TAG)
    if summary_count == 1 and failure_count == 0:
        result_text, summary = raw_output.split(SUMMARY_TAG, 1)
        try:
            return CognitiveSuccessResult(result=result_text, summary=summary)
        except ValidationError:
            raise CognitiveResultProtocolError(repairable=True) from None

    if failure_count == 1 and summary_count == 0:
        prefix, failure_reason = raw_output.split(FAILURE_TAG, 1)
        if prefix.strip():
            raise CognitiveResultProtocolError(repairable=True)
        try:
            return CognitiveFailureResult(failure_reason=failure_reason)
        except ValidationError:
            raise CognitiveResultProtocolError(repairable=True) from None

    raise CognitiveResultProtocolError(repairable=True)
