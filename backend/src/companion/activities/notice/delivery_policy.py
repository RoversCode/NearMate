"""定义 Notice 批次选择与候选替换的纯规则。"""

from __future__ import annotations

from companion.activities.notice.delivery_state import (
    InteractionSessionNoticeState,
    NoticeReadMode,
    NoticeRecord,
)


def select_notice_batch(
    session_state: InteractionSessionNoticeState,
    *,
    mode: NoticeReadMode,
) -> list[NoticeRecord]:
    """按读取模式选择一批尚未绑定请求的提醒。

    这里不改变任何提醒状态，只从当前会话里挑出“这次请求可以领取”的记录。
    用户主动发起的请求只能顺带处理强提醒，避免后台进度打断用户正在进行的话题；
    独立 System Notice 在有强提醒时也只领取强提醒，没有强提醒时才领取弱进度。
    普通生命周期按活动来源串行；权限和资料请求按各自 request_id 分车道，因此同一
    Activity 的并列请求可以在同一批完整表达。

    Args:
        session_state: 当前交互会话的提醒状态。
        mode: 用户请求附着或独立 System Notice 模式。

    Returns:
        按业务优先级和创建顺序排列、且交付车道互不重复的提醒记录。
    """

    # 同一个活动来源可能产生多条弱进度；只有 Coordinator 当前保留的那一条
    # 才有资格进入候选，已经被新进度替换的旧记录不能再次播报。
    weak_candidate_ids = set(
        session_state.pending_progress_notice_id_by_source.values()
    )

    # 一条 Notice 从预留开始到请求最终收口前，始终占用自己的来源车道。即使它已经
    # 被新事实标成 superseded，也要等旧请求确认不再输出后才能让后来提醒进入另一请求，
    # 否则用户插话与 System Notice 可能把同来源终态倒序说出。
    occupied_lanes = {
        notice_lane_key(notice)
        for notice in session_state.notices_by_id.values()
        if notice.request_id is not None
    }

    # pending 表示尚未被某个请求预留；强提醒无需竞争弱候选槽，弱提醒还必须是来源
    # 的最新候选。最后一个条件按读取模式隔离：真实用户请求不能领取弱进度，避免
    # 系统插话抢走回答焦点。
    candidates = [
        notice
        for notice in session_state.notices_by_id.values()
        if notice.state == "pending"
        and notice_lane_key(notice) not in occupied_lanes
        and (notice.strength == "strong" or notice.notice_id in weak_candidate_ids)
        and (mode == "system_notice" or notice.strength == "strong")
    ]

    # LLM 请求一旦提交就不能只删掉其中一条提醒。强弱混在同一批时，任何会持续
    # 更新的弱进度都会迫使整批取消，连带延迟已经稳定的结果或待答请求。因此只要
    # 当前存在强提醒，本批就完全排除弱进度；没有强提醒时才组织弱进度批次。
    if any(notice.strength == "strong" for notice in candidates):
        candidates = [
            notice for notice in candidates if notice.strength == "strong"
        ]

    # 先按业务紧急程度，再按创建先后稳定排序；编号兜底可避免同一时刻创建时顺序漂移。
    candidates.sort(key=notice_sort_key)

    # 扫描排序结果时，每条交付车道只取最靠前的一项。普通生命周期仍按来源串行；
    # 并列交互请求各自占用独立车道，因此不会互相吞掉。
    selected: list[NoticeRecord] = []
    selected_lanes: set[str] = set()
    for notice in candidates:
        lane_key = notice_lane_key(notice)
        if lane_key in selected_lanes:
            continue
        selected.append(notice)
        selected_lanes.add(lane_key)
    return selected


def should_replace_pending_progress(
    candidate: NoticeRecord,
    current: NoticeRecord,
) -> bool:
    """判断新的弱进度是否应替换当前来源候选。

    一个活动来源同时只保留一条可播报的弱进度，防止“正在分析”“正在生成”等
    过期提示在稍后被重复说出。优先级更高的候选直接胜出；优先级相同则保留更新的一条。

    Args:
        candidate: 新进入候选竞争的提醒。
        current: 当前已经占据来源候选槽的提醒。

    Returns:
        新提醒优先级更高或同优先级下更新时返回真。
    """

    # 先比较业务等级，确保例如等待用户处理的提醒不会被普通进度覆盖。
    candidate_priority = notice_priority(candidate)
    current_priority = notice_priority(current)
    if candidate_priority != current_priority:
        return candidate_priority < current_priority
    # 创建时间相同在高并发下仍可能发生，单调编号是最终的确定性裁决依据。
    return (
        candidate.created_at,
        notice_number(candidate),
    ) > (
        current.created_at,
        notice_number(current),
    )


def notice_sort_key(notice: NoticeRecord) -> tuple[int, float, int]:
    """返回提醒批次使用的稳定排序键。

    返回值可直接交给排序函数：元组从左到右比较，因此先说更紧急的提醒，
    同级提醒则遵循先创建先说的顺序。

    Args:
        notice: 待排序的提醒记录。

    Returns:
        业务优先级、创建时间和内部编号组成的排序键。
    """

    return notice_priority(notice), notice.created_at, notice_number(notice)


def notice_priority(notice: NoticeRecord) -> int:
    """返回数值越小越优先的提醒业务级别。

    终态最需要用户立刻知道；需要用户处理的提醒排第二；普通进度最后。
    使用数值而非字符串比较，是为了让批次选择和候选替换共享同一套明确规则。

    Args:
        notice: 待判断优先级的提醒记录。

    Returns:
        终态为零、用户介入为一、进度为二。
    """

    # 后台任务已经完成、失败、取消或中断时，结果不能被普通状态更新压后。
    if notice.intent in {
        "cognitive_result",
        "work_result",
        "terminal_status",
    }:
        return 0
    if notice.intent in {
        "permission_request",
        "information_request",
    }:
        return 1
    return 2


def notice_lane_key(notice: NoticeRecord) -> str:
    """返回一条提醒占用的串行交付车道。

    普通进度和终态仍按来源串行，避免同一执行器的生命周期倒序。权限和资料请求
    是用户需要逐项处理的并列事实，因此每个请求使用自己的车道，允许同一 Activity
    在一个 System Notice 批次里完整表达多项请求。

    Args:
        notice: 待分配交付车道的提醒。

    Returns:
        只在进程内使用的稳定车道键。
    """

    if notice.intent in {"permission_request", "information_request"}:
        return (
            f"{notice.source}:interaction:{notice.activity_id}:"
            f"{notice.interaction_request_id}"
        )
    return notice.source


def notice_number(notice: NoticeRecord) -> int:
    """返回提醒创建时分配的单调内部序号。

    ``notice_id`` 的格式由 Notice 创建边界保证为 ``notice-<整数>``；此处只负责
    取出整数，供排序在时间戳相同的情况下得到稳定结果。

    Args:
        notice: 待读取创建序号的提醒记录。

    Returns:
        从稳定 Notice 编号中解析出的整数序号。
    """

    return int(notice.notice_id.removeprefix("notice-"))
