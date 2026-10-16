"""集中定义 Cognitive 失败是否允许进入一次自我反思。"""

from __future__ import annotations

from companion.foreground_self_reflection.models import (
    CognitiveRecoveryDecision,
    CognitiveRecoverySignal,
    InputDiversionResult,
)


def input_diversion_was_accepted(
    result: InputDiversionResult | None,
) -> bool:
    """判断输入是否已经进入未处理插话片段。

    ``duplicate`` 也算已经接纳，因为网络或媒体重试不能把同一条正文重新构造成
    普通 Foreground 请求。

    Args:
        result: Attention 改道端口返回的结构化结果；端口未接线时为空。

    Returns:
        首次接纳或幂等重复时返回真。
    """

    return result is not None and result.diverted


def input_diversion_blocks_normal_request(
    result: InputDiversionResult | None,
) -> bool:
    """判断改道裁决失败时是否必须禁止普通助手请求。

    端口未接线代表产品没有启用 Attention lane，可以继续普通请求。端口已接线但
    返回未知、材料写入失败或基础设施异常时，系统无法证明当前输入没有被恢复状态
    占用，必须把它收口为失败并等待用户重新提交。

    Args:
        result: Attention 改道端口返回的结构化结果；端口未接线时为空。

    Returns:
        需要阻断普通请求时返回真。
    """

    if result is None or input_diversion_was_accepted(result):
        return False
    return result.assistant_request_lease is None


def evaluate_cognitive_recovery(
    signal: CognitiveRecoverySignal,
) -> CognitiveRecoveryDecision:
    """根据已提交 Activity 事实判断是否允许尝试恢复。

    这里不读取正文、模型输出或运行时忙闲状态，只处理执行器已经归一化的机器事实。
    Attention 是否空闲、材料租约能否取得等事务条件由后续主进程服务裁决。

    Args:
        signal: Cognitive 执行器发布的最小终态事实。

    Returns:
        是否具有恢复资格，以及调用方可记录的稳定原因。
    """

    if not signal.activity_started:
        return CognitiveRecoveryDecision(
            eligible=False,
            reason="source_activity_not_started",
        )
    if signal.terminal_state != "failed":
        return CognitiveRecoveryDecision(
            eligible=False,
            reason="terminal_state_not_failed",
        )
    if signal.failure_code is None:
        return CognitiveRecoveryDecision(
            eligible=False,
            reason="failure_code_missing",
        )
    if signal.failure_code != "cognitive_task_failed":
        return CognitiveRecoveryDecision(
            eligible=False,
            reason="non_business_failure_not_recoverable",
        )
    return CognitiveRecoveryDecision(
        eligible=True,
        reason="eligible_business_failure",
    )
