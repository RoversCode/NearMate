"""把后台活动快照和提醒批次整理成 prompt 能用的上下文。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from companion.activities.foreground_snapshot import ForegroundActivitySnapshot


def compile_activity_context(
    foreground_context: dict[str, object],
    snapshots: Sequence[ForegroundActivitySnapshot],
    *,
    previous_reports_by_activity: Mapping[tuple[str, str], Sequence[str]],
) -> dict[str, object]:
    """把后台活动快照和提醒批次整理成本轮 prompt 能用的上下文。

    Args:
        foreground_context: 主进程传入的材料、环境与提醒态势。
        snapshots: 两类 Activity Executor 在 Prompt 构建前现场读取的活动快照。
        previous_reports_by_activity: 按来源和活动编号冻结的历史实际播报。

    Returns:
        包含本地活动事实和合格提醒批次的新 foreground_context。
    """

    # 复制输入，避免修改原字典。
    compiled = dict(foreground_context)
    snapshot_by_key = {
        (snapshot.source, snapshot.activity_id): snapshot
        for snapshot in snapshots
    }
    # 用 Executor 快照构建活动列表，并附上该活动之前播报过的内容。
    compiled["active_activities"] = [
        snapshot.with_previous_reports(
            _previous_reports_for(
                previous_reports_by_activity,
                source=snapshot.source,
                activity_id=snapshot.activity_id,
            )
        ).to_payload()
        for snapshot in snapshots
    ]

    # 处理提醒批次；弱提醒必须与活动快照对齐，否则丢弃。
    raw_notices = foreground_context.get("notice_batch")
    if not isinstance(raw_notices, list):
        compiled["notice_batch"] = []
        return compiled

    compiled_notices: list[dict[str, object]] = []
    for raw_notice in raw_notices:
        if not isinstance(raw_notice, dict):
            continue
        notice = dict(raw_notice)

        # 强提醒代表已发生的成果、终态或用户交互事实，直接保留。
        if notice.get("intent") != "progress_report":
            if (
                notice.get("intent") == "permission_request"
                and notice.get("grant_scope") != "activity"
            ):
                continue
            if notice.get("intent") in {
                "cognitive_result",
                "work_result",
                "terminal_status",
            }:
                # 终态提醒附带历史播报，让上下文连贯。
                previous_reports = _previous_reports_for(
                    previous_reports_by_activity,
                    source=_clean_text(notice.get("source")),
                    activity_id=_clean_text(notice.get("activity_id")),
                )
                if previous_reports:
                    notice["previous_reports"] = list(previous_reports)
            compiled_notices.append(notice)
            continue

        # 弱提醒过滤：必须与快照当前进度匹配，且快照有可播报的新正文。
        source = _clean_text(notice.get("source"))
        activity_id = _clean_text(notice.get("activity_id"))
        sequence = notice.get("sequence")
        snapshot = snapshot_by_key.get((source, activity_id))
        if (
            snapshot is None
            or not isinstance(sequence, int)
            or isinstance(sequence, bool)
            or sequence != snapshot.progress_sequence
            or not snapshot.progress_reasoning_text.strip()
        ):
            # 活动不存在、序号过期或没有进度正文则丢弃。
            continue
        compiled_notices.append(notice)
    compiled["notice_batch"] = compiled_notices
    return compiled

def _clean_text(value: object) -> str:
    """把值转成干净的字符串，非字符串返回空串。"""

    return value.strip() if isinstance(value, str) else ""


def _previous_reports_for(
    reports_by_activity: Mapping[tuple[str, str], Sequence[str]],
    *,
    source: str,
    activity_id: str,
) -> tuple[str, ...]:
    """读取某个活动此前实际播报过的非空正文。"""

    if not source or not activity_id:
        return ()
    return tuple(
        report.strip()
        for report in reports_by_activity.get((source, activity_id), ())
        if isinstance(report, str) and report.strip()
    )


def progress_reports_from_payload(
    value: object,
    *,
    activity_keys: set[tuple[str, str]],
) -> dict[tuple[str, str], tuple[str, ...]]:
    """从主进程冻结载荷中读取指定 Activity 的已公开 Progress。

    Args:
        value: `AssistantRequest.foreground_context` 中的 Progress 快照列表。
        activity_keys: 当前 active Activity 和终态 Notice 实际会用到的身份。

    Returns:
        按来源和 Activity 编号索引的公开正文；无关或非法条目被忽略。
    """

    normalized_keys = {
        (source.strip(), activity_id.strip())
        for source, activity_id in activity_keys
        if source.strip() and activity_id.strip()
    }
    if not normalized_keys or not isinstance(value, list):
        return {}

    reports_by_activity: dict[tuple[str, str], tuple[str, ...]] = {}
    for item in value:
        if not isinstance(item, dict):
            continue
        key = (
            _clean_text(item.get("source")),
            _clean_text(item.get("activity_id")),
        )
        if key not in normalized_keys:
            continue
        raw_reports = item.get("reports")
        if not isinstance(raw_reports, list):
            continue
        reports = tuple(
            report.strip()
            for report in raw_reports
            if isinstance(report, str) and report.strip()
        )
        if reports:
            reports_by_activity[key] = reports
    return reports_by_activity
