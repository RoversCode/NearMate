"""定义并严格校验 Cognitive 模型的二分支业务结果。"""

from __future__ import annotations

import json
import re
from typing import Annotated, TypeAlias

from pydantic import (
    BaseModel,
    ConfigDict,
    StringConstraints,
    ValidationError,
    field_validator,
)


MAX_COGNITIVE_RESULT_CHARS = 500_000
MAX_COGNITIVE_SUMMARY_CHARS = 2_000
MAX_COGNITIVE_FAILURE_REASON_CHARS = 2_000


class _DuplicateJsonKeyError(ValueError):
    """表示一个 JSON 对象重复声明了同名字段。"""


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """拒绝重复字段，避免解析器静默采用最后一个值。"""

    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKeyError("JSON 对象包含重复字段")
        result[key] = value
    return result

_ResultText = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=MAX_COGNITIVE_RESULT_CHARS,
    ),
]
_SummaryText = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=MAX_COGNITIVE_SUMMARY_CHARS,
    ),
]
_FailureReasonText = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=MAX_COGNITIVE_FAILURE_REASON_CHARS,
    ),
]

# 失败说明会进入恢复提示和任务记忆，因此拒绝最常见的凭据、堆栈和绝对路径外形。
_UNSAFE_FAILURE_REASON_PATTERNS = (
    re.compile(r"(?i)traceback\s*\(most recent call last\)"),
    re.compile(r"(?i)(?:api[_-]?key|authorization)\s*[:=]"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"(?i)\bsk-[A-Za-z0-9_-]{4,}"),
    re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/]"),
    re.compile(r"(?<![:A-Za-z0-9])/(?:home|users?|var|etc|opt|root|tmp)/"),
)


class CognitiveResultProtocolError(ValueError):
    """表示模型正文没有通过 Cognitive 本地结果协议。

    Args:
        repairable: 是否允许把正文交给固定 Flash 模型修复一次。

    Attributes:
        failure_code: Activity 和恢复策略使用的稳定协议故障码。
        repairable: 当前错误是否允许进入一次修复调用。
    """

    failure_code = "cognitive_result_protocol_failed"

    def __init__(self, *, repairable: bool) -> None:
        """创建不携带原始正文或校验器诊断的安全异常。

        Args:
            repairable: 是否允许把原文交给 Flash 修复一次。
        """

        self.repairable = repairable
        super().__init__("Cognitive 结果不符合二分支协议。")


class CognitiveSuccessResult(BaseModel):
    """保存已完成 Cognitive 任务的完整结果和摘要。"""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    result: _ResultText
    summary: _SummaryText

    @property
    def succeeded(self) -> bool:
        """说明本对象代表完整成功结果。

        Returns:
            固定返回真。
        """

        return True


class CognitiveFailureResult(BaseModel):
    """保存 Cognitive 明确无法完成任务时的安全失败原因。"""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    failure_reason: _FailureReasonText

    @field_validator("failure_reason")
    @classmethod
    def reject_unsafe_failure_reason(cls, value: str) -> str:
        """阻止供应商诊断、凭据和本机路径进入恢复与记忆。

        Args:
            value: 模型生成的安全自然语言失败说明。

        Returns:
            不含敏感诊断外形的原说明。

        Raises:
            ValueError: 文本包含凭据、堆栈或绝对路径外形。
        """

        if any(pattern.search(value) for pattern in _UNSAFE_FAILURE_REASON_PATTERNS):
            raise ValueError("failure_reason 包含不允许公开的诊断信息")
        return value

    @property
    def succeeded(self) -> bool:
        """说明本对象代表业务失败结果。

        Returns:
            固定返回假。
        """

        return False


CognitiveResult: TypeAlias = CognitiveSuccessResult | CognitiveFailureResult


def parse_cognitive_result(raw_output: str) -> CognitiveResult:
    """把模型正文校验为唯一可信的 Cognitive 二分支结果。

    JSON Mode 只是供应商提示，不是产品门禁。本函数要求正文只能包含一个 JSON
    对象，并继续校验字段集合、严格类型、长度和失败说明安全性。成功对象只能有
    ``result``、``summary``，失败对象只能有 ``failure_reason``。

    Args:
        raw_output: Cognitive Plus 或 Flash 返回的完整正文。

    Returns:
        已通过全部本地校验、可以安全投影到 Activity 的结果。

    Raises:
        CognitiveResultProtocolError: 正文为空、超长或不符合二分支合同。
    """

    if not isinstance(raw_output, str) or not raw_output.strip():
        raise CognitiveResultProtocolError(repairable=False)
    if len(raw_output) > MAX_COGNITIVE_RESULT_CHARS:
        raise CognitiveResultProtocolError(repairable=False)
    try:
        payload = json.loads(raw_output, object_pairs_hook=_reject_duplicate_json_keys)
    except (json.JSONDecodeError, TypeError, ValueError):
        raise CognitiveResultProtocolError(repairable=True) from None
    if not isinstance(payload, dict):
        raise CognitiveResultProtocolError(repairable=True)

    field_names = set(payload)
    try:
        if field_names == {"result", "summary"}:
            return CognitiveSuccessResult.model_validate(payload, strict=True)
        if field_names == {"failure_reason"}:
            return CognitiveFailureResult.model_validate(payload, strict=True)
    except ValidationError:
        # 字段路径和原始值不能越过协议边界，更不能出现在最终异常或 Activity 中。
        raise CognitiveResultProtocolError(repairable=True) from None
    raise CognitiveResultProtocolError(repairable=True)
