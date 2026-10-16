from __future__ import annotations

import json
import logging
import threading
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any


class ProcessRoleFilter(logging.Filter):
    """为当前进程的日志记录补充稳定角色。

    Args:
        process_role: 主进程或助手运行时的稳定角色名。
    """

    def __init__(self, process_role: str) -> None:
        super().__init__()
        self._process_role = process_role

    @property
    def process_role(self) -> str:
        """返回过滤器绑定的进程角色。"""

        return self._process_role

    def filter(self, record: logging.LogRecord) -> bool:
        """为日志记录写入进程角色。

        Args:
            record: 即将进入 handler 的日志记录。

        Returns:
            固定返回真，允许该记录继续输出。
        """

        record.process_role = self._process_role
        return True


class JsonSequenceFilter(ProcessRoleFilter):
    """为权威 JSONL 日志补充进程内单调序号。"""

    def __init__(self, process_role: str) -> None:
        super().__init__(process_role)
        self._lock = threading.Lock()
        self._sequence = 0

    def filter(self, record: logging.LogRecord) -> bool:
        """原子分配序号并补充进程角色。

        Args:
            record: 即将写入 JSONL 的日志记录。

        Returns:
            固定返回真，允许该记录继续输出。
        """

        super().filter(record)
        with self._lock:
            self._sequence += 1
            record.process_sequence = self._sequence
        return True


class StructuredJsonFormatter(logging.Formatter):
    """把业务日志格式化为一行 UTF-8 JSON。"""

    def format(self, record: logging.LogRecord) -> str:
        """生成单行 JSONL 记录。

        Args:
            record: 已补充进程角色和序号的日志记录。

        Returns:
            不包含换行符的 JSON 字符串。
        """

        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "event": getattr(record, "event_name", "log"),
            "message": record.getMessage(),
            "logger": record.name,
            "process_role": getattr(record, "process_role", "unknown"),
            "process_name": record.processName,
            "pid": record.process,
            "thread": record.threadName,
            "sequence": getattr(record, "process_sequence", 0),
        }
        structured_fields = getattr(record, "structured_fields", None)
        if isinstance(structured_fields, Mapping):
            payload.update(
                {
                    str(key): _json_safe(value)
                    for key, value in structured_fields.items()
                    if str(key) not in payload
                }
            )
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def log_event(
    logger: logging.Logger,
    level: int,
    event: str,
    /,
    **fields: object,
) -> None:
    """同时生成适合终端阅读和 JSONL 检索的结构化事件。

    Args:
        logger: 负责输出事件的业务 logger。
        level: Python logging 级别。
        event: 稳定事件名称。
        **fields: 不含密钥和大段正文的结构化诊断字段。
    """

    normalized_event = event.strip()
    if not normalized_event:
        raise ValueError("结构化日志事件名不能为空")
    normalized_fields = {str(key): _json_safe(value) for key, value in fields.items()}
    field_text = " ".join(
        f"{key}={_human_field(value)}" for key, value in normalized_fields.items()
    )
    message = f"{normalized_event} {field_text}" if field_text else normalized_event
    logger.log(
        level,
        message,
        extra={
            "event_name": normalized_event,
            "structured_fields": normalized_fields,
        },
    )


def _json_safe(value: object) -> object:
    """把结构化字段转换为 JSON 兼容的轻量值。

    Args:
        value: 调用方提供的诊断字段。

    Returns:
        可直接交给 JSON 编码器的值。
    """

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(item) for item in value]
    return str(value)


def _human_field(value: object) -> str:
    """把结构化字段压缩成终端单行文本。

    Args:
        value: 已完成 JSON 兼容转换的字段。

    Returns:
        适合追加到事件名后的单行字符串。
    """

    if isinstance(value, str):
        return repr(value)
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
