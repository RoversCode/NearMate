"""保存本次进程内有界、结构化的控制事件日志。"""

from __future__ import annotations

import threading
from collections import deque
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass(frozen=True, slots=True)
class RuntimeLogRow:
    """表示一条本次运行的结构化事件记录。"""

    sequence: int
    event_type: str
    payload: dict[str, object]
    recorded_at: str

    def to_payload(self) -> dict[str, object]:
        """转换为 Console API 使用的独立载荷。"""

        return {
            "sequence": self.sequence,
            "event_type": self.event_type,
            "payload": deepcopy(self.payload),
            "recorded_at": self.recorded_at,
        }


class RuntimeLogBuffer:
    """维护最多五百条、本次进程有效的线程安全事件日志。"""

    def __init__(self, *, capacity: int = 500) -> None:
        """初始化有界日志缓冲。

        Args:
            capacity: 最大记录数量。
        """

        if capacity <= 0:
            raise ValueError("capacity 必须大于零")
        self._rows: deque[RuntimeLogRow] = deque(maxlen=capacity)
        self._next_sequence = 1
        self._capacity = capacity
        self._lock = threading.Lock()

    def append(self, *, event_type: str, payload: dict[str, object]) -> RuntimeLogRow:
        """追加一条结构化日志并按容量淘汰最旧记录。

        Args:
            event_type: 控制事件名称。
            payload: JSON 友好的结构化载荷。

        Returns:
            已写入缓冲区的不可变记录。
        """

        with self._lock:
            row = RuntimeLogRow(
                sequence=self._next_sequence,
                event_type=event_type,
                payload=deepcopy(payload),
                recorded_at=datetime.now(UTC).isoformat(),
            )
            self._next_sequence += 1
            self._rows.append(row)
            return row

    def list_rows(self, limit: int = 100) -> list[RuntimeLogRow]:
        """按最新优先读取日志快照。

        Args:
            limit: 最多返回条数，范围固定为 1 到缓冲容量。

        Returns:
            与内部缓冲解耦的日志记录列表。
        """

        normalized_limit = min(max(int(limit), 1), self._capacity)
        with self._lock:
            rows = list(reversed(self._rows))[:normalized_limit]
        return [
            RuntimeLogRow(
                sequence=row.sequence,
                event_type=row.event_type,
                payload=deepcopy(row.payload),
                recorded_at=row.recorded_at,
            )
            for row in rows
        ]
