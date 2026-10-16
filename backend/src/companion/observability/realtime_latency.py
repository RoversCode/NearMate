from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# 敏感 metadata 字段不进入 JSONL
_SENSITIVE_METADATA_KEYS = {"user_text", "assistant_text"}


class RealtimeLatencyRecorder:
    """实时语音耗时本地落盘器。

    只负责把每个延迟 marker 以 JSONL 行追加落盘，不维护任何内存状态，因此跨进程安全：
    多个进程写同一文件路径时，按 trace_id（turn_id）事后聚合即可还原完整端到端链路。
    汇总分析见 backend/tests/observability/offline_latency_summary.py。
    """

    def __init__(self, jsonl_path: Path | str) -> None:
        """创建耗时记录器。

        Args:
            jsonl_path: JSONL 落盘路径，父目录不存在时自动创建。
        """

        self.jsonl_path = Path(jsonl_path)

    def record_marker(
        self,
        *,
        trace_id: str,
        marker: str,
        at_wall: float,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """记录延迟标记到 JSONL。

        Args:
            trace_id: 追踪编号，通常是 turn_id；多进程共用同一文件时按它事后聚合。
            marker: 标记名称。
            at_wall: 壁钟时间（秒），需与链路其他打点使用同一时钟源。
            metadata: 可选结构化上下文，敏感字段不会进入 JSONL。
        """

        self._append_jsonl(
            {
                "event": "marker",
                "trace_id": trace_id,
                "marker": marker,
                "at_wall": at_wall,
                "metadata": _filter_sensitive(metadata or {}),
            }
        )

    def _append_jsonl(self, record: dict[str, Any]) -> None:
        """追加一行到 JSONL 文件。"""

        self.jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        with self.jsonl_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _filter_sensitive(metadata: dict[str, Any]) -> dict[str, Any]:
    """过滤敏感 metadata 字段。"""

    return {k: v for k, v in metadata.items() if k not in _SENSITIVE_METADATA_KEYS}
