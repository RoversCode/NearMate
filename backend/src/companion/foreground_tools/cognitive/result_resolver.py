"""编排 Cognitive 本地校验与一次性 Flash 修复。"""

from __future__ import annotations

from companion.foreground_tools.cognitive.contracts import CognitiveExecutionPlan
from companion.foreground_tools.cognitive.marker_result_protocol import (
    parse_marked_cognitive_result,
)
from companion.foreground_tools.cognitive.result_protocol import (
    CognitiveResult,
    CognitiveResultProtocolError,
    parse_cognitive_result,
)
from companion.foreground_tools.cognitive.result_repairer import (
    CognitiveResultRepairerProtocol,
)
from companion.llm.stream_lifecycle import CancellationSignal


class CognitiveResultResolver:
    """把 Plus 原文解析为可信结果，必要时只允许修复一次。"""

    def __init__(self, *, repairer: CognitiveResultRepairerProtocol) -> None:
        """保存固定修复端口。

        Args:
            repairer: 只调用 Qwen Flash 一次的格式修复器。
        """

        self._repairer = repairer

    def resolve(
        self,
        *,
        activity_id: str,
        plan: CognitiveExecutionPlan,
        raw_output: str,
        cancellation_signal: CancellationSignal | None,
    ) -> CognitiveResult:
        """执行本地校验、可选修复和第二次本地校验。

        Args:
            activity_id: 来源 Cognitive Activity 编号。
            plan: Flash 理解字段所需的结构化任务目标。
            raw_output: Plus 返回的完整正文。
            cancellation_signal: 与来源 Activity 共用的取消信号。

        Returns:
            Plus 或 Flash 产生并通过同一协议门禁的二分支结果。

        Raises:
            CognitiveResultProtocolError: 原文不可修复、Flash 调用失败或修复后仍不合格。
        """

        try:
            return parse_marked_cognitive_result(raw_output)
        except CognitiveResultProtocolError as exception:
            if not exception.repairable:
                raise

        try:
            repaired_output = self._repairer.repair(
                activity_id=activity_id,
                plan=plan,
                raw_output=raw_output,
                cancellation_signal=cancellation_signal,
            )
        except Exception:
            # 第三方异常可能夹带响应正文；这里只把它收敛成不含原文的稳定协议故障。
            raise CognitiveResultProtocolError(repairable=False) from None

        try:
            return parse_cognitive_result(repaired_output)
        except CognitiveResultProtocolError:
            raise CognitiveResultProtocolError(repairable=False) from None
