from __future__ import annotations

import json
import time


def log_event(event_type: str, **payload) -> str:
    """生成结构化日志文本。

    当前阶段先返回 JSON 字符串，
    后续若接真实日志系统，可在不改调用点的前提下替换实现。
    """

    record = {
        "timestamp": time.time(),
        "type": event_type,
        **payload,
    }
    return json.dumps(record, ensure_ascii=False)
