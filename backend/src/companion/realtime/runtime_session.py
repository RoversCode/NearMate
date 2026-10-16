from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class InputTurnSession:
    """维护用户语音输入 turn 的轻量状态机。"""

    # 当前活跃或最近一次完成的 turn 编号；空值表示本轮尚未开始收集用户话语。
    current_turn_id: str | None = None
    # 当前 Turn 的数值顺序；消费者必须使用它排序，不能解析可读的 turn_id。
    current_turn_sequence: int | None = None
    # 当前 turn 的音频阶段，使用可读字符串便于测试断言和日志观测。
    turn_audio_state: str = "empty"  # empty -> collecting -> finalized
    # 输入 turn 内部自增计数器；局部持有可以避免跨测试或跨连接互相污染编号。
    _next_turn_number: int = field(default=1, repr=False)

    @classmethod
    def empty(cls) -> "InputTurnSession":
        """创建一个空的输入 turn 状态机。

        Returns:
            InputTurnSession: `current_turn_id` 为空、`turn_audio_state` 为 `empty` 的独立输入 turn 状态机。"""

        # 显式空状态入口让调用方表达“重新开始输入 turn 跟踪”，而不是泛化成整个 session 重建。
        return cls()

    def begin_turn(self) -> None:
        """开始新的用户 turn，或继续尚未结束的当前 turn。

        Returns:
            None: 状态原地更新，不返回额外结果。"""

        # Smart Turn 尚未确认结束时，新的音频仍属于同一个当前 turn，不能误切新编号。
        if self.current_turn_id is not None and self.turn_audio_state != "finalized":
            self.turn_audio_state = "collecting"
            return

        # 没有 turn 或上一 turn 已 finalized 时，才分配新的 turn 编号。
        self.current_turn_id = f"turn-{self._next_turn_number}"
        self.current_turn_sequence = self._next_turn_number
        self._next_turn_number += 1
        self.turn_audio_state = "collecting"

    def reset_active_turn(self) -> None:
        """清空当前输入 Turn，同时保留后续编号进度。

        输入轨切换、短语音忽略或 VAD 重置只代表当前采集上下文失效，不代表 Backend
        生命周期重新开始。保留序号可以让迟到的 ASR 结果和新 Turn 继续可靠区分。

        Returns:
            None: 当前 Turn 原地回到空状态，下一个编号保持递增。
        """

        self.current_turn_id = None
        self.current_turn_sequence = None
        self.turn_audio_state = "empty"

    def mark_smart_turn_result(self, *, is_turn_finished: bool) -> None:
        """应用 Smart Turn 对当前 turn 的结束判断。

        Args:
            is_turn_finished (bool): `True` 表示模型确认当前用户话语已经结束，`False` 表示仍应继续收集。

        Returns:
            None: 只更新当前 turn 的音频阶段，不创建新 turn。
        """

        # 没有当前 turn 时忽略模型结果，避免异步迟到结果创建错误状态。
        if self.current_turn_id is None:
            return

        # finalized 的 turn 已归档，任何迟到的 Smart Turn 结果都不能重新打开旧 turn。
        if self.turn_audio_state == "finalized":
            return

        # 模型确认结束后，当前音频属于上一 turn，下一次 begin_turn 才能新建 turn。
        if is_turn_finished:
            self.turn_audio_state = "finalized"
            return

        # 模型认为没说完时，继续当前 turn，后续用户音频会与此前音频合并再判断。
        self.turn_audio_state = "collecting"
