"""从 Claude Agent SDK 消息中收集后台工作 Token 用量。"""

from __future__ import annotations

import math
from collections import defaultdict
from threading import RLock
from typing import Any, Mapping

from claude_agent_sdk import AssistantMessage, ResultMessage

from companion.background_agent.usage_models import (
    AgentModelTokenUsage,
    AgentTaskUsageSnapshot,
)


class AgentTaskUsageCollector:
    """按消息覆盖累计，并优先采用 SDK 终态模型汇总。

    Assistant 消息可能被 SDK 重复投影，因此过程数据以 ``model + message_id`` 为键覆盖；
    收到 ``ResultMessage.model_usage`` 后，它代表整项任务的权威汇总，会完全替换过程累计。
    """

    def __init__(self) -> None:
        """创建一份只服务于单项后台工作的空收集器。"""

        self._message_usage: dict[tuple[str, str], AgentModelTokenUsage] = {}
        self._result_usage: dict[str, AgentModelTokenUsage] | None = None
        self._reference_cost_usd: float | None = None
        # 应用关闭时 Runner 线程可能需要同步读取最后快照；同一把锁让该读取不会
        # 与 SDK 事件循环正在写入消息用量发生字典遍历竞态。
        self._lock = RLock()

    def record_assistant_message(self, message: AssistantMessage) -> None:
        """覆盖记录一条具有稳定消息编号的 Assistant 用量。

        Args:
            message: Claude Agent SDK 的 Assistant 消息。
        """

        model = message.model.strip()
        message_id = message.message_id.strip() if isinstance(message.message_id, str) else ""
        if not model or not message_id or not isinstance(message.usage, dict):
            return
        with self._lock:
            self._message_usage[(model, message_id)] = _usage_from_mapping(
                model,
                message.usage,
                camel_case=False,
            )

    def record_result_message(self, message: ResultMessage) -> None:
        """记录参考成本，并在存在时采用权威按模型汇总。

        Args:
            message: Claude Agent SDK 的任务终态消息。
        """

        with self._lock:
            self._reference_cost_usd = _safe_reference_cost(message.total_cost_usd)
            if message.model_usage is None:
                return
            self._result_usage = {
                model.strip(): _usage_from_mapping(model.strip(), usage, camel_case=True)
                for model, usage in message.model_usage.items()
                if isinstance(model, str) and model.strip() and isinstance(usage, Mapping)
            }

    def snapshot(self) -> AgentTaskUsageSnapshot:
        """生成当前可靠数据的按模型合并快照。

        Returns:
            收到权威终态汇总时标记完整，否则返回当前可确认的部分数据。
        """

        with self._lock:
            if self._result_usage is not None:
                by_model = tuple(
                    self._result_usage[model]
                    for model in sorted(self._result_usage)
                )
                return AgentTaskUsageSnapshot(
                    by_model=by_model,
                    is_complete=True,
                    reference_cost_usd=self._reference_cost_usd,
                )

            totals: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0, 0])
            for usage in self._message_usage.values():
                model_totals = totals[usage.model]
                model_totals[0] += usage.non_cached_input_tokens
                model_totals[1] += usage.cache_creation_input_tokens
                model_totals[2] += usage.cache_read_input_tokens
                model_totals[3] += usage.output_tokens
            by_model = tuple(
                AgentModelTokenUsage(
                    model=model,
                    non_cached_input_tokens=values[0],
                    cache_creation_input_tokens=values[1],
                    cache_read_input_tokens=values[2],
                    output_tokens=values[3],
                )
                for model, values in sorted(totals.items())
            )
            return AgentTaskUsageSnapshot(
                by_model=by_model,
                is_complete=False,
                reference_cost_usd=self._reference_cost_usd,
            )


def _usage_from_mapping(
    model: str,
    usage: Mapping[str, Any],
    *,
    camel_case: bool,
) -> AgentModelTokenUsage:
    """把 SDK 的两种字段命名统一为 NearMate 用量模型。"""

    key_by_field = (
        {
            "input": "inputTokens",
            "cache_creation": "cacheCreationInputTokens",
            "cache_read": "cacheReadInputTokens",
            "output": "outputTokens",
        }
        if camel_case
        else {
            "input": "input_tokens",
            "cache_creation": "cache_creation_input_tokens",
            "cache_read": "cache_read_input_tokens",
            "output": "output_tokens",
        }
    )
    return AgentModelTokenUsage(
        model=model,
        non_cached_input_tokens=_safe_token_count(usage.get(key_by_field["input"])),
        cache_creation_input_tokens=_safe_token_count(
            usage.get(key_by_field["cache_creation"])
        ),
        cache_read_input_tokens=_safe_token_count(usage.get(key_by_field["cache_read"])),
        output_tokens=_safe_token_count(usage.get(key_by_field["output"])),
    )


def _safe_token_count(value: object) -> int:
    """把 SDK 的缺失或异常计数收敛为零。"""

    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def _safe_reference_cost(value: object) -> float | None:
    """只保留 SDK 返回的非负有限参考成本。"""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    normalized = float(value)
    return normalized if math.isfinite(normalized) and normalized >= 0 else None
