"""验证前台自我反思的恢复资格与静态工具安全边界。"""

from __future__ import annotations

from inspect import signature

import pytest

from companion.foreground_self_reflection.models import (
    CognitiveRecoverySignal,
    InputDiversionResult,
)
from companion.foreground_self_reflection.policy import (
    evaluate_cognitive_recovery,
    input_diversion_blocks_normal_request,
    input_diversion_was_accepted,
)
from companion.foreground_self_reflection.tool_policy import (
    SelfReflectionToolPolicy,
)
from companion.foreground_tools.catalog.metadata import (
    ToolLatencyClass,
    ToolMetadata,
)
from companion.foreground_tools.catalog.service import ForegroundToolService
from companion.foreground_tools.external.weather_tool import WeatherForegroundTool
from companion.llm.foreground_request_policy import foreground_request_policy


def _signal(
    *,
    failure_code: str | None,
    activity_started: bool = True,
    terminal_state: str = "failed",
) -> CognitiveRecoverySignal:
    """构造一条最小 Cognitive 终态事实。

    Args:
        failure_code: Activity 执行器归一化后的系统错误码。
        activity_started: 来源 Activity 是否已经发布真实 started 事实。
        terminal_state: Activity 的执行终态。

    Returns:
        可直接交给恢复资格策略的事实。
    """

    return CognitiveRecoverySignal(
        runtime_epoch=7,
        source_activity_id="cognitive-activity-1",
        source_request_id="foreground-request-1",
        source_protocol_transaction_id="tool-transaction-1",
        activity_started=activity_started,
        terminal_state=terminal_state,
        failure_code=failure_code,
    )


@pytest.mark.parametrize(
    "failure_code",
    [
        "authentication_failed",
        "provider_unavailable",
        "network_unavailable",
        "request_timed_out",
        "provider_stream_interrupted",
        "invalid_provider_response",
        "unclassified_error",
    ],
)
def test_infrastructure_and_unknown_failures_are_not_eligible(
    failure_code: str,
) -> None:
    """已启动的基础设施或未知失败都不能进入自我反思。

    Args:
        failure_code: 一种基础设施或未知 Activity 错误码。
    """

    decision = evaluate_cognitive_recovery(_signal(failure_code=failure_code))

    assert decision.eligible is False
    assert decision.reason == "non_business_failure_not_recoverable"


def test_only_business_failure_is_eligible() -> None:
    """只有模型合法失败分支产生的业务错误码可以进入一次恢复。"""

    decision = evaluate_cognitive_recovery(
        _signal(failure_code="cognitive_task_failed")
    )

    assert decision.eligible is True
    assert decision.reason == "eligible_business_failure"


def test_recovery_foreground_request_uses_frozen_full_context_and_high_thinking() -> None:
    """可见 Recovery 必须像 Foreground 一样读冻结语义，但不能读取动态 Activity。"""

    policy = foreground_request_policy("self_reflection_recovery")

    assert policy.history_view == "full_protocol"
    assert policy.records_user_message is False
    assert policy.records_assistant_reply is True
    assert policy.accepts_materials is True
    assert policy.includes_activity_state is False
    assert policy.allows_tools is True
    assert policy.requires_notice_batch is False
    assert policy.touches_session is True
    assert policy.thinking_level == "high"


def test_interruption_reply_uses_normal_streaming_without_tools_or_thinking() -> None:
    """打扰回应复用可见输出链，但不能读取历史、调用工具或执行用户内容。"""

    policy = foreground_request_policy("self_reflection_interruption_reply")

    assert policy.history_view == "none"
    assert policy.records_user_message is False
    assert policy.records_assistant_reply is False
    assert policy.accepts_materials is False
    assert policy.includes_activity_state is False
    assert policy.allows_tools is False
    assert policy.requires_notice_batch is False
    assert policy.touches_session is False
    assert policy.thinking_level == "off"


@pytest.mark.parametrize(
    ("signal", "expected_reason"),
    [
        (_signal(failure_code="cognitive_task_failed", activity_started=False), "source_activity_not_started"),
        (_signal(failure_code="cognitive_task_failed", terminal_state="completed"), "terminal_state_not_failed"),
        (_signal(failure_code=None), "failure_code_missing"),
        (_signal(failure_code="cognitive_result_protocol_failed"), "non_business_failure_not_recoverable"),
        (_signal(failure_code="runtime_reconfigured"), "non_business_failure_not_recoverable"),
        (_signal(failure_code="runtime_interrupted"), "non_business_failure_not_recoverable"),
        (_signal(failure_code=None, terminal_state="cancelled"), "terminal_state_not_failed"),
        (_signal(failure_code=None, terminal_state="interrupted"), "terminal_state_not_failed"),
        (_signal(failure_code=None, terminal_state="rejected"), "terminal_state_not_failed"),
    ],
)
def test_non_recoverable_terminal_facts_never_enter_attention(
    signal: CognitiveRecoverySignal,
    expected_reason: str,
) -> None:
    """未启动、澄清、拒绝、取消和运行时中断都不能误触发恢复。"""

    decision = evaluate_cognitive_recovery(signal)

    assert decision.eligible is False
    assert decision.reason == expected_reason


@pytest.mark.parametrize(
    "reason",
    [
        "attention_not_accepting_inputs",
        "runtime_epoch_mismatch",
        "reflection_deadline_exceeded",
    ],
)
def test_input_diversion_policy_blocks_unleased_no_attention_reasons(
    reason: str,
) -> None:
    """没有同锁 lease 时，任何旧式“无 Attention”判断都不能继续普通请求。"""

    result = InputDiversionResult(status="not_diverted", reason=reason)

    assert input_diversion_was_accepted(result) is False
    assert input_diversion_blocks_normal_request(result) is True


def test_input_diversion_policy_only_allows_normal_request_with_admission_lease() -> None:
    """只有 Attention owner 签发的 lease 能证明普通请求赢得提交竞态。"""

    class AdmissionLease:
        """提供满足结构类型的最小测试 lease。"""

        def commit_enqueued(self) -> bool:
            """模拟请求成功入队。"""

            return True

        def rollback(self) -> bool:
            """模拟请求入队前失败。"""

            return True

    result = InputDiversionResult(
        status="not_diverted",
        reason="assistant_request_reserved",
        assistant_request_lease=AdmissionLease(),
    )

    assert input_diversion_was_accepted(result) is False
    assert input_diversion_blocks_normal_request(result) is False


@pytest.mark.parametrize(
    "reason",
    [
        "runtime_epoch_unavailable",
        "diversion_failed",
        "episode_memory_append_failed",
        "unknown_reason",
    ],
)
def test_input_diversion_policy_fails_closed_for_unproven_ownership(
    reason: str,
) -> None:
    """无法证明输入未被恢复状态占用时，普通 AssistantRequest 必须被阻断。"""

    result = InputDiversionResult(status="not_diverted", reason=reason)

    assert input_diversion_was_accepted(result) is False
    assert input_diversion_blocks_normal_request(result) is True


def test_input_diversion_policy_treats_duplicate_as_accepted_and_unwired_as_normal() -> None:
    """重复接纳不能重建请求，未接线的端口则代表产品没有启用 Attention lane。"""

    duplicate = InputDiversionResult(
        status="duplicate",
        reason="input_already_accepted",
    )

    assert input_diversion_was_accepted(duplicate) is True
    assert input_diversion_blocks_normal_request(duplicate) is False
    assert input_diversion_was_accepted(None) is False
    assert input_diversion_blocks_normal_request(None) is False


def test_tool_policy_only_exposes_background_and_statically_safe_inline_tools() -> None:
    """白名单只保留稳定后台入口和明确标记的天气类 Inline 工具。"""

    metadata = [
        _metadata(
            name="lookup_current_fact",
            family_id="cognitive",
            safe=False,
            latency=ToolLatencyClass.LONG_RUNNING,
        ),
        _metadata(
            name="start_background_work",
            family_id="background_work",
            safe=True,
            latency=ToolLatencyClass.LONG_RUNNING,
        ),
        _metadata(
            name="weather_overview",
            family_id="weather",
            safe=True,
            latency=ToolLatencyClass.EXTERNAL_API,
        ),
        _metadata(
            name="unsafe_long_running_tool",
            family_id="weather",
            safe=True,
            latency=ToolLatencyClass.LONG_RUNNING,
        ),
        _metadata(
            name="mislabelled_inline_tool",
            family_id="other",
            safe=True,
            latency=ToolLatencyClass.INSTANT,
        ),
    ]
    schemas = [_schema(item.name) for item in metadata]

    selected = SelfReflectionToolPolicy().select(
        schemas=schemas,
        metadata=metadata,
    )

    assert [item.name for item in selected] == [
        "start_background_work",
        "weather_overview",
    ]
    assert selected[0].execution_channel == "background"
    assert selected[1].execution_channel == "inline"
    assert "队列" in selected[0].capability_description
    assert "真实天气" in selected[1].capability_description


def test_tool_policy_has_no_request_or_runtime_routing_inputs() -> None:
    """工具安全策略的接口不能逐渐长成读取正文或瞬时状态的 Router。"""

    parameter_names = set(signature(SelfReflectionToolPolicy.select).parameters)

    assert parameter_names == {"self", "schemas", "metadata"}


def test_production_catalog_builds_recovery_tools_without_dynamic_foreground_set() -> None:
    """生产目录直接组装固定安全能力，不先取得普通 Foreground 全量工具。"""

    service = ForegroundToolService(
        weather_tools=WeatherForegroundTool(service=object()),
    )

    selected = service.self_reflection_tools()
    selected_names = {item.name for item in selected}
    ordinary_names = {
        schema["function"]["name"]
        for schema in service.tool_schemas()
        if schema.get("type") == "function"
    }

    assert "start_background_work" in selected_names
    assert "weather_overview" in selected_names
    assert "lookup_current_fact" in ordinary_names
    assert "lookup_current_fact" not in selected_names
    assert "recall_task_memory" in ordinary_names
    assert "recall_task_memory" not in selected_names
    assert not any(name.startswith("correct_") for name in selected_names)


def test_tool_policy_rejects_duplicate_or_incomplete_catalog_entries() -> None:
    """目录冲突和缺少能力说明时直接失败，避免模型看到含糊工具。"""

    valid = _metadata(
        name="weather_overview",
        family_id="weather",
        safe=True,
        latency=ToolLatencyClass.EXTERNAL_API,
    )
    duplicate = [valid, valid]
    with pytest.raises(ValueError, match="重复"):
        SelfReflectionToolPolicy().select(
            schemas=[_schema(valid.name)],
            metadata=duplicate,
        )
    with pytest.raises(ValueError, match="capability_description"):
        ToolMetadata(
            name="weather_forecast",
            display_name="天气预报",
            family_id="weather",
            family_display_name="天气",
            latency_class=ToolLatencyClass.EXTERNAL_API,
            capability_description="   ",
            self_reflection_safe=True,
        )


def _metadata(
    *,
    name: str,
    family_id: str,
    safe: bool,
    latency: ToolLatencyClass,
) -> ToolMetadata:
    """构造工具安全策略测试使用的目录项。

    Args:
        name: 工具函数名。
        family_id: 工具所属能力族。
        safe: 是否由开发者静态允许自我反思使用。
        latency: 工具的执行通道提示。

    Returns:
        带有完整能力说明的工具元数据。
    """

    descriptions = {
        "start_background_work": "把明确目标提交给真实后台执行器；可能已开始、进入队列或被拒绝，不能当场交付最终结果。",
        "weather_overview": "查询指定地点的真实天气，成功后返回经过系统校验的天气结果。",
    }
    return ToolMetadata(
        name=name,
        display_name=name,
        family_id=family_id,
        family_display_name=family_id,
        latency_class=latency,
        capability_description=descriptions.get(name, "测试能力说明。"),
        self_reflection_safe=safe,
    )


def _schema(name: str) -> dict[str, object]:
    """构造只包含稳定名称的测试工具声明。

    Args:
        name: 工具函数名。

    Returns:
        最小 OpenAI 兼容函数工具声明。
    """

    return {
        "type": "function",
        "function": {
            "name": name,
            "description": "此处描述不会取代目录中的权威能力说明。",
            "parameters": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
    }
