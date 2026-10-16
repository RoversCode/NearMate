"""把内部失败事实转换为安全的公开 Activity 故障报告。

本模块只有纯函数：所有面向用户的故障文案都在这里生成，
来源 Activity Executor 与 Registry 不得各自维护错误文案。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, cast

import httpx
import openai

from companion.activities.runtime.models import (
    ACTIVITY_FAILURE_CODES,
    ACTIVITY_STAGE_CODES,
    ActivityFailure,
    ActivityFailureCode,
    activity_stage_label,
)
# 每种稳定错误码的用户文案：摘要说清发生了什么，建议给出下一步行动。
_FAILURE_COPY: dict[str, tuple[str, str]] = {
    "configuration_invalid": (
        "模型或相关功能配置不完整，任务无法继续。",
        "请打开控制台检查模型与功能配置，保存后重新发起任务。",
    ),
    "authentication_failed": (
        "模型服务凭证无效或已失效。",
        "请在控制台设置中更新 API Key 后重新发起任务。",
    ),
    "rate_limited": (
        "模型服务当前请求过多，暂时无法处理。",
        "请稍等片刻后重新发起任务。",
    ),
    "provider_unavailable": (
        "模型服务暂时不可用。",
        "请稍后重新发起任务；若持续失败，请确认服务提供商状态。",
    ),
    "network_unavailable": (
        "当前网络无法连接模型服务。",
        "请检查网络连接与代理设置后重新发起任务。",
    ),
    "request_timed_out": (
        "等待模型服务响应超时。",
        "请检查网络状况后重新发起任务。",
    ),
    "provider_stream_interrupted": (
        "结果传输过程中连接中断。",
        "请检查网络状况后重新发起任务。",
    ),
    "invalid_provider_response": (
        "模型服务返回了无法正常处理的结果。",
        "请重新发起任务；若反复出现，请把任务编号提供给开发者排查。",
    ),
    "material_unavailable": (
        "任务所需的材料缺失、损坏或无法读取。",
        "请确认材料仍然可以访问后重新提交任务。",
    ),
    "agent_max_turns": (
        "后台工作已达到本任务的最大执行轮数，尚未完成。",
        "请缩小任务范围，或在控制台提高 max_turns 后重新发起任务。",
    ),
    "agent_execution_error": (
        "后台工作在执行过程中停止，任务未能完成。",
        "请重新发起任务；若反复出现，请把任务编号提供给开发者排查。",
    ),
    "agent_cli_crash": (
        "Claude Code 运行时意外中断，后台任务未能完成。",
        "请重新发起任务；若反复出现，请把任务编号提供给开发者排查。",
    ),
    "agent_transport_overflow": (
        "后台工作返回的数据超过安全传输上限，任务未能完成。",
        "请缩小页面或应用的观察范围后重新发起任务；若反复出现，请把任务编号提供给开发者排查。",
    ),
    "runtime_reconfigured": (
        "运行配置已更新，任务随旧后台运行时结束。",
        "新配置已经生效，请重新发起任务。",
    ),
    "runtime_interrupted": (
        "后台运行时意外中断，任务未能完成。",
        "请重新发起任务；若反复出现，请把任务编号提供给开发者排查。",
    ),
    "cognitive_task_failed": (
        "认知任务没有完成用户目标。",
        "请补充必要信息后重新发起任务。",
    ),
    "cognitive_result_protocol_failed": (
        "认知模型连续返回了无法可靠解析的结果。",
        "请重新发起任务；若反复出现，请把任务编号提供给开发者排查。",
    ),
    "unclassified_error": (
        "任务遇到尚未分类的错误。",
        "请重新发起任务；若反复出现，请把任务编号提供给开发者排查。",
    ),
}

# 已知结构化失败原因到稳定错误码的映射；来源只汇报原因，不自行决定产品语义。
_STRUCTURED_REASON_CODES: dict[str, ActivityFailureCode] = {
    "model_not_configured": "configuration_invalid",
    "material_changed_or_unreadable": "material_unavailable",
    "no_processable_material": "material_unavailable",
    "material_workload_exceeded": "material_unavailable",
    "empty_model_output": "invalid_provider_response",
    "invalid_provider_response": "invalid_provider_response",
    "cognitive_task_failed": "cognitive_task_failed",
    "cognitive_result_protocol_failed": "cognitive_result_protocol_failed",
    "agent_max_turns": "agent_max_turns",
    "agent_execution_error": "agent_execution_error",
    "agent_cli_crash": "agent_cli_crash",
    "agent_transport_overflow": "agent_transport_overflow",
}

_ERROR_TYPE_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_.]{0,127}")
_PROVIDER_IDENTIFIER_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_SECRET_IDENTIFIER_PATTERNS = (
    re.compile(r"(?i)^sk-[A-Za-z0-9_-]{4,}$"),
    re.compile(r"^AIza[A-Za-z0-9_-]{20,}$"),
    re.compile(r"^AKIA[A-Z0-9]{16}$"),
    re.compile(r"^eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$"),
)
_FAILURE_PAYLOAD_KEYS = frozenset(
    {
        "stage_code",
        "stage_label",
        "code",
        "summary",
        "technical_detail",
        "suggested_action",
    }
)
_DIAGNOSTIC_FIELD_ORDER = (
    "error_type",
    "http_status",
    "provider_request_id",
    "provider_error_code",
    "material_count",
)


@dataclass(frozen=True, slots=True)
class FailureDiagnostics:
    """保存允许进入公共故障报告的结构化诊断字段。

    Args:
        error_type: 异常类型名称，不包含异常正文。
        http_status: 模型服务返回的 HTTP 状态码。
        provider_request_id: 模型服务生成的请求编号。
        provider_error_code: 模型服务返回的稳定错误码。
        material_count: 本次失败涉及的材料数量。
    """

    error_type: str | None = None
    http_status: int | None = None
    provider_request_id: str | None = None
    provider_error_code: str | None = None
    material_count: int | None = None

    def __post_init__(self) -> None:
        """校验所有诊断字段均满足公共载荷白名单约束。"""

        if self.error_type is not None and not _ERROR_TYPE_PATTERN.fullmatch(
            self.error_type
        ):
            raise ValueError("故障诊断异常类型格式非法")
        for field_name, value in (
            ("provider_request_id", self.provider_request_id),
            ("provider_error_code", self.provider_error_code),
        ):
            if value is not None and (
                not _PROVIDER_IDENTIFIER_PATTERN.fullmatch(value)
                or _looks_like_secret_identifier(value)
            ):
                raise ValueError(f"故障诊断字段 {field_name} 格式非法")
        if self.http_status is not None and not 100 <= self.http_status <= 599:
            raise ValueError("故障诊断 HTTP 状态码非法")
        if self.material_count is not None and self.material_count < 0:
            raise ValueError("故障诊断材料数量不能为负数")

    def to_technical_detail(self) -> str:
        """按固定顺序渲染公共技术线索。

        Returns:
            仅由白名单字段组成的单行技术详情。
        """

        parts: list[str] = []
        if self.error_type is not None:
            parts.append(f"error_type={self.error_type}")
        if self.http_status is not None:
            parts.append(f"http_status={self.http_status}")
        if self.provider_request_id is not None:
            parts.append(f"provider_request_id={self.provider_request_id}")
        if self.provider_error_code is not None:
            parts.append(f"provider_error_code={self.provider_error_code}")
        if self.material_count is not None:
            parts.append(f"material_count={self.material_count}")
        return "; ".join(parts)


def parse_activity_failure(payload: object) -> ActivityFailure:
    """严格校验跨进程来源携带的公开故障报告。

    Args:
        payload: Assistant 子进程事件载荷中的 failure 字段。

    Returns:
        由固定文案和白名单诊断字段重建的 ActivityFailure。

    Raises:
        ValueError: 结构、阶段、错误码、固定文案或诊断字段非法。
    """

    if not isinstance(payload, dict):
        raise ValueError("Activity 故障报告必须是对象")
    keys = frozenset(payload.keys())
    if keys != _FAILURE_PAYLOAD_KEYS:
        raise ValueError(f"Activity 故障报告字段集合非法：{sorted(keys)!r}")

    stage_code = payload["stage_code"]
    stage_label = payload["stage_label"]
    code = payload["code"]
    if not isinstance(stage_code, str) or stage_code not in ACTIVITY_STAGE_CODES:
        raise ValueError(f"Activity 故障报告阶段代码非法：{stage_code!r}")
    if stage_label != activity_stage_label(stage_code):
        raise ValueError(f"Activity 故障报告阶段文案非法：{stage_label!r}")
    if not isinstance(code, str) or code not in ACTIVITY_FAILURE_CODES:
        raise ValueError(f"Activity 故障报告错误码非法：{code!r}")

    expected_summary, expected_action = _FAILURE_COPY[code]
    if payload["summary"] != expected_summary:
        raise ValueError("Activity 故障报告摘要不是固定公开文案")
    if payload["suggested_action"] != expected_action:
        raise ValueError("Activity 故障报告建议不是固定公开文案")
    diagnostics = _parse_technical_detail(payload["technical_detail"])
    return _build_failure(
        cast(ActivityFailureCode, code),
        stage_code=stage_code,
        diagnostics=diagnostics,
    )


def failure_from_reason(
    reason: str,
    *,
    stage_code: str,
    diagnostics: FailureDiagnostics | None = None,
) -> ActivityFailure:
    """把已知结构化失败原因转换为公开故障报告。

    Args:
        reason: 来源执行器汇报的稳定失败原因。
        stage_code: 失败发生时所在阶段。
        diagnostics: 允许公开的结构化诊断字段。

    Returns:
        归类完成的公开故障报告；未知原因保守降级为未分类错误。
    """

    _require_stage_code(stage_code)
    code = _STRUCTURED_REASON_CODES.get(reason, "unclassified_error")
    return _build_failure(
        code,
        stage_code=stage_code,
        diagnostics=diagnostics,
    )


def failure_from_exception(
    exc: BaseException,
    *,
    stage_code: str,
) -> ActivityFailure:
    """把捕获到的异常对象转换为公开故障报告。

    Args:
        exc: 最靠近真实失败处捕获到的异常。
        stage_code: 失败发生时所在阶段。

    Returns:
        按用户行动归类的公开故障报告；未知异常只暴露安全的字段。
    """

    _require_stage_code(stage_code)
    code = _classify_exception(exc)
    return _build_failure(
        code,
        stage_code=stage_code,
        diagnostics=_exception_diagnostics(exc),
    )


def runtime_interruption_failure(
    code: Literal["runtime_reconfigured", "runtime_interrupted"],
    *,
    stage_code: str,
) -> ActivityFailure:
    """为运行时中断生成公开故障报告。

    Args:
        code: 计划内热替换或意外中断对应的错误码。
        stage_code: 中断时任务所在阶段。

    Returns:
        两类运行时中断之一的公开故障报告。

    Raises:
        ValueError: 错误码不是两类运行时中断之一。
    """

    _require_stage_code(stage_code)
    if code not in ("runtime_reconfigured", "runtime_interrupted"):
        raise ValueError(f"非法运行时中断错误码：{code!r}")
    return _build_failure(code, stage_code=stage_code)


def failure_notice_text(code: ActivityFailureCode) -> str:
    """返回稳定错误码的原因摘要与行动建议联排文本。

    Args:
        code: 十二个稳定产品错误码之一。

    Returns:
        供 Foreground 转述的完整短文案。
    """

    summary, suggested_action = _FAILURE_COPY[code]
    return f"{summary}{suggested_action}"


def _classify_exception(exc: BaseException) -> ActivityFailureCode:
    """按用户行动把异常归为稳定错误码。"""

    # 本进程 LLM 客户端保留的稳定 Provider 错误类型优先于猜测式分类。
    explicit_code = getattr(exc, "activity_failure_code", None)
    if isinstance(explicit_code, str) and explicit_code in ACTIVITY_FAILURE_CODES:
        return cast(ActivityFailureCode, explicit_code)
    # APITimeoutError 是 APIConnectionError 的子类，必须先判断超时。
    if isinstance(exc, openai.APITimeoutError):
        return "request_timed_out"
    if isinstance(exc, openai.APIConnectionError):
        return "network_unavailable"
    if isinstance(exc, openai.APIStatusError):
        return _classify_http_status(exc.status_code)
    if isinstance(exc, httpx.RemoteProtocolError):
        return "provider_stream_interrupted"
    if isinstance(exc, httpx.TimeoutException):
        return "request_timed_out"
    if isinstance(exc, httpx.TransportError):
        return "network_unavailable"
    return "unclassified_error"


def _classify_http_status(status_code: int) -> ActivityFailureCode:
    """把 HTTP 状态码归为稳定错误码。"""

    if status_code in (401, 403):
        return "authentication_failed"
    if status_code == 429:
        return "rate_limited"
    if status_code >= 500:
        return "provider_unavailable"
    return "invalid_provider_response"


def _exception_diagnostics(exc: BaseException) -> FailureDiagnostics:
    """从异常对象提取允许公开的结构化诊断字段。"""

    error_type = _safe_error_type(type(exc).__name__)
    status_code: int | None = None
    request_id: str | None = None
    provider_error_code: str | None = None
    # 诊断字段必须来自明确受信任的异常类型，不能仅凭同名属性越过公开边界。
    if isinstance(exc, openai.APIStatusError):
        status_code = exc.status_code
        request_id = exc.request_id
    return FailureDiagnostics(
        error_type=error_type,
        http_status=(
            status_code
            if isinstance(status_code, int) and 100 <= status_code <= 599
            else None
        ),
        provider_request_id=_safe_provider_identifier(request_id),
        provider_error_code=_safe_provider_identifier(provider_error_code),
    )


def _build_failure(
    code: ActivityFailureCode,
    *,
    stage_code: str,
    diagnostics: FailureDiagnostics | None = None,
) -> ActivityFailure:
    """用唯一文案目录组装公开故障报告。"""

    summary, suggested_action = _FAILURE_COPY[code]
    return ActivityFailure(
        stage_code=stage_code,
        stage_label=activity_stage_label(stage_code),
        code=code,
        summary=summary,
        technical_detail=(
            diagnostics.to_technical_detail() if diagnostics is not None else ""
        ),
        suggested_action=suggested_action,
    )


def _safe_error_type(value: object) -> str | None:
    """仅保留合法的异常类型名称。"""

    if not isinstance(value, str) or not _ERROR_TYPE_PATTERN.fullmatch(value):
        return None
    return value


def _safe_provider_identifier(value: object) -> str | None:
    """仅保留可安全公开的模型服务标识符。"""

    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if (
        not _PROVIDER_IDENTIFIER_PATTERN.fullmatch(normalized)
        or _looks_like_secret_identifier(normalized)
    ):
        return None
    return normalized


def _looks_like_secret_identifier(value: str) -> bool:
    """判断结构化标识符是否具有常见凭据外形。"""

    return any(pattern.fullmatch(value) for pattern in _SECRET_IDENTIFIER_PATTERNS)


def _parse_technical_detail(value: object) -> FailureDiagnostics | None:
    """把公共技术详情反向校验为固定顺序的白名单诊断字段。"""

    if not isinstance(value, str):
        raise ValueError("Activity 故障报告技术详情必须是字符串")
    if value == "":
        return None

    fields: dict[str, str] = {}
    last_field_index = -1
    for part in value.split("; "):
        field_name, separator, field_value = part.partition("=")
        if not separator or not field_value or field_name not in _DIAGNOSTIC_FIELD_ORDER:
            raise ValueError("Activity 故障报告技术详情字段非法")
        field_index = _DIAGNOSTIC_FIELD_ORDER.index(field_name)
        if field_index <= last_field_index or field_name in fields:
            raise ValueError("Activity 故障报告技术详情字段顺序非法")
        fields[field_name] = field_value
        last_field_index = field_index

    try:
        diagnostics = FailureDiagnostics(
            error_type=fields.get("error_type"),
            http_status=(
                int(fields["http_status"])
                if "http_status" in fields
                else None
            ),
            provider_request_id=fields.get("provider_request_id"),
            provider_error_code=fields.get("provider_error_code"),
            material_count=(
                int(fields["material_count"])
                if "material_count" in fields
                else None
            ),
        )
    except (TypeError, ValueError) as exception:
        raise ValueError("Activity 故障报告技术详情字段非法") from exception
    if diagnostics.to_technical_detail() != value:
        raise ValueError("Activity 故障报告技术详情不是规范格式")
    return diagnostics


def _require_stage_code(stage_code: str) -> None:
    """校验故障报告的阶段代码属于五个稳定阶段。"""

    if stage_code not in ACTIVITY_STAGE_CODES:
        raise ValueError(f"非法 Activity 阶段代码：{stage_code!r}")
