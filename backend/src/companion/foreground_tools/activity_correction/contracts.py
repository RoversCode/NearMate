"""定义 Activity 纠正跨执行来源传递的稳定领域数据。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from companion.materials.foreground_images import ForegroundImageReference, VisualEvidence


CorrectionCancellationOutcome = Literal[
    "cleaned",
    "activity_no_longer_correctable",
    "cancel_not_accepted",
    "cleanup_timeout",
]


@dataclass(frozen=True, slots=True)
class CorrectionEvidence:
    """保存允许在纠正后跨 Activity 继续使用的证据。

    Args:
        images: 带来源标签、可直接交给 Cognitive 或冻结到工作区的图片证据。
        image_materials: 图片来源引用，供终态后的派生重新校验。
        source_urls: Cognitive 旧目标曾明确授权使用的网页来源。
        cognitive_tool_types: Cognitive 父 Activity 已经包含的认知工具类型。
    """

    images: tuple[VisualEvidence, ...] = ()
    image_materials: tuple[ForegroundImageReference, ...] = ()
    source_urls: tuple[str, ...] = ()
    cognitive_tool_types: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CorrectionReservation:
    """保存纠正任务预留阶段的一次完整结果。

    纠正执行器必须在准备新 Activity 时同时返回稳定编号和事件快照。
    上层不再从事件载荷猜测编号，这样即使事件内容被裁剪，也不会把无法回收的
    预留登记成已接受任务。

    Args:
        accepted: 是否成功预留了新的 Activity。
        activity_id: 成功预留时分配的新 Activity 编号；拒绝时为空。
        events: 准备或拒绝事件的不可变快照。
    """

    accepted: bool
    activity_id: str = ""
    events: tuple[dict[str, object], ...] = ()

    def __post_init__(self) -> None:
        """清洗编号，并拒绝无法回滚的伪成功预留。

        Raises:
            ValueError: accepted 为真但没有新 Activity 编号。
        """

        normalized_activity_id = self.activity_id.strip()
        object.__setattr__(self, "activity_id", normalized_activity_id)
        object.__setattr__(self, "events", tuple(self.events))
        if self.accepted and not normalized_activity_id:
            raise ValueError("已接受的纠正预留必须包含 Activity 编号")
