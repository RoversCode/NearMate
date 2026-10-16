from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


SpeakerGateStatus = Literal[
    "disabled",
    "waiting_audio",
    "passed",
    "rejected",
    "uncertain",
]


@dataclass
class BargeInCandidate:
    """记录一次用户插话候选的可确认条件。

    Args:
        turn_id: 候选插话所属的用户输入轮次。
        speaker_status: 当前轮次说话人确认状态。
        text_actionable_seen: 是否已经看到足以触发助手响应的 ASR 文本。
        cancel_sent: 是否已经发送过插话取消命令。
    """

    turn_id: str
    speaker_status: SpeakerGateStatus
    text_actionable_seen: bool = False
    cancel_sent: bool = False


def initial_speaker_gate_status(
    *,
    speaker_verification_enabled: bool,
) -> SpeakerGateStatus:
    """生成新用户轮次的初始说话人 gate 状态。

    Args:
        speaker_verification_enabled: 当前是否存在可用的说话人确认 provider。

    Returns:
        SpeakerGateStatus: 未启用时保持旧行为，已启用时等待足够音频做确认。
    """

    if not speaker_verification_enabled:
        return "disabled"
    return "waiting_audio"


def speaker_gate_allows_barge_in(status: SpeakerGateStatus) -> bool:
    """判断说话人 gate 状态是否允许确认插话。

    Args:
        status: 当前候选插话所属 turn 的说话人 gate 状态。

    Returns:
        bool: 未启用说话人验证或已确认主人时返回 True。
    """

    return status in {"disabled", "passed"}


def can_confirm_barge_in(
    candidate: BargeInCandidate | None,
    *,
    turn_id: str | None,
    assistant_playout_active: bool,
) -> bool:
    """判断候选插话是否已经满足取消当前助手播报的条件。

    Args:
        candidate: 当前用户输入轮次的插话候选；没有候选时不能取消播报。
        turn_id: 当前 ASR 或说话人确认事实所属的用户输入轮次。
        assistant_playout_active: 助手音频是否仍在真实播放链路中。

    Returns:
        bool: 所有插话确认条件都满足时返回 True。
    """

    if candidate is None:
        return False
    if candidate.cancel_sent:
        return False
    if candidate.turn_id != turn_id:
        return False
    if not candidate.text_actionable_seen:
        return False
    if not speaker_gate_allows_barge_in(candidate.speaker_status):
        return False
    return assistant_playout_active