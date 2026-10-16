"""定义助手可见输出在主进程与子进程之间共享的窄契约。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, TypeAlias


AssistantHistoryPolicy: TypeAlias = Literal[
    "stable_dialogue",
    "activity_progress",
    "unattended_episode",
]
AssistantOutputSettlementOutcome: TypeAlias = Literal[
    "completed",
    "interrupted",
    "superseded",
    "failed",
    "output_unavailable",
]


@dataclass(frozen=True, slots=True)
class ActivityProgressReference:
    """关联一次 Progress 表达与对应 Activity 的冻结进度。

    Args:
        activity_id: Progress 所属的 Activity 编号。
        source: Activity 的内部来源类别。
        notice_sequence: 执行器为这次冻结进度分配的递增序号。
    """

    activity_id: str
    source: str
    notice_sequence: int

    def to_payload(self) -> dict[str, object]:
        """转换为跨进程事件使用的普通字典。

        Returns:
            不包含共享可变对象的 Progress 引用载荷。
        """

        return {
            "activity_id": self.activity_id,
            "source": self.source,
            "notice_sequence": self.notice_sequence,
        }


def normalize_progress_references(
    values: object,
) -> tuple[ActivityProgressReference, ...]:
    """清洗并去重一组跨进程 Progress 引用。

    Args:
        values: dataclass、字典或二者混合组成的输入序列。

    Returns:
        按首次出现顺序保留的合法不可变引用。
    """

    if not isinstance(values, (list, tuple)):
        return ()

    normalized: list[ActivityProgressReference] = []
    seen: set[tuple[str, str, int]] = set()
    for value in values:
        if isinstance(value, ActivityProgressReference):
            activity_id = value.activity_id.strip()
            source = value.source.strip()
            notice_sequence = value.notice_sequence
        elif isinstance(value, dict):
            raw_activity_id = value.get("activity_id")
            raw_source = value.get("source")
            activity_id = (
                raw_activity_id.strip()
                if isinstance(raw_activity_id, str)
                else ""
            )
            source = raw_source.strip() if isinstance(raw_source, str) else ""
            notice_sequence = value.get("notice_sequence")
        else:
            continue

        if (
            not activity_id
            or not source
            or not isinstance(notice_sequence, int)
            or isinstance(notice_sequence, bool)
            or notice_sequence <= 0
        ):
            continue
        identity = (activity_id, source, notice_sequence)
        if identity in seen:
            continue
        seen.add(identity)
        normalized.append(
            ActivityProgressReference(
                activity_id=activity_id,
                source=source,
                notice_sequence=notice_sequence,
            )
        )
    return tuple(normalized)


def progress_references_payload(
    references: tuple[ActivityProgressReference, ...],
) -> list[dict[str, object]]:
    """复制一组 Progress 引用，供 RuntimeEvent 安全携带。

    Args:
        references: 已清洗的不可变 Activity Progress 引用。

    Returns:
        保持输入顺序的普通字典列表。
    """

    return [reference.to_payload() for reference in references]


def resolve_assistant_output_contract(
    *,
    request_source: str | None,
    foreground_context: object,
) -> tuple[AssistantHistoryPolicy, tuple[ActivityProgressReference, ...]]:
    """根据请求来源和冻结的 Notice 批次确定输出应该写到哪里。

    Args:
        request_source: 助手请求来源，例如 text_input、final_asr、system_notice
            或 Self-Reflection 的入场与 Recovery 来源。
        foreground_context: 主进程刚冻结的前台态势字典。

    Returns:
        历史策略和纯 Progress 批次关联的 Activity 引用。
    """

    if request_source in {
        "self_reflection_entry",
        "self_reflection_recovery",
    }:
        return "stable_dialogue", ()
    if request_source == "self_reflection_interruption_reply":
        return "unattended_episode", ()
    if request_source != "system_notice" or not isinstance(
        foreground_context, dict
    ):
        return "stable_dialogue", ()
    raw_notices = foreground_context.get("notice_batch")
    if not isinstance(raw_notices, list) or not raw_notices:
        return "stable_dialogue", ()
    if any(
        not isinstance(notice, dict)
        or notice.get("intent") != "progress_report"
        for notice in raw_notices
    ):
        return "stable_dialogue", ()

    references = normalize_progress_references(
        [
            {
                "activity_id": notice.get("activity_id"),
                "source": notice.get("source"),
                "notice_sequence": notice.get("sequence"),
            }
            for notice in raw_notices
            if isinstance(notice, dict)
        ]
    )
    return "activity_progress", references
