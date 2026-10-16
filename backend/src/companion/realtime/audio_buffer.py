from __future__ import annotations

import numpy as np


class CurrentTurnAudioBuffer:
    """维护当前 turn 的累计音频，并构造 Smart Turn 固定输入窗口。"""

    def __init__(self, sample_rate: int, smart_turn_max_seconds: int) -> None:
        """初始化当前 turn 音频缓存。"""

        # 采样率必须为正数，否则目标样本数会变成 0 或负数，破坏 Smart Turn 固定长度窗口契约。
        if sample_rate <= 0:
            raise ValueError("sample_rate 必须为正数，才能构造 Smart Turn 固定长度音频窗口")

        # 最大窗口秒数必须为正数，否则切片和补零逻辑会产生不可诊断的异常窗口。
        if smart_turn_max_seconds <= 0:
            raise ValueError("smart_turn_max_seconds 必须为正数，才能构造 Smart Turn 固定长度音频窗口")

        # 当前音频采样率，用于把秒级 Smart Turn 窗口换算成样本数量。
        self.sample_rate = sample_rate
        # Smart Turn 模型单次可接收的最大音频秒数，超过后只保留当前 turn 末尾窗口。
        self.smart_turn_max_seconds = smart_turn_max_seconds
        # 当前 turn 尚未确认结束前的累计单声道音频；确认结束后由 clear 清空。
        self._audio = np.zeros(0, dtype=np.float32)

    @property
    def sample_count(self) -> int:
        """返回当前 turn 已累计的样本数量。"""

        return int(self._audio.shape[0])

    @property
    def duration_seconds(self) -> float:
        """返回当前 turn 已累计音频的持续时间。"""

        # 采样率配置异常时，观测属性不应让运行时崩溃；构造窗口仍由主逻辑暴露配置问题。
        if self.sample_rate == 0:
            return 0.0
        return self.sample_count / self.sample_rate

    def append(self, audio: np.ndarray) -> None:
        """追加当前 turn 的一段单声道音频。"""

        # 先转换成 numpy 数组，统一接收 list、tuple 或不同 dtype 的 ndarray 输入。
        audio_array = np.asarray(audio, dtype=np.float32)

        # Smart Turn 当前窗口只接受单声道一维波形；多维输入通常代表尚未混音，必须显式失败。
        if audio_array.ndim != 1:
            raise ValueError("Smart Turn 当前音频缓存只接受一维单声道音频")

        # 空音频没有说话内容，直接忽略可避免制造无意义的 turn 状态变化。
        if audio_array.size == 0:
            return

        # 当前 turn 尚未结束时，后续开口应与前段合并，供模型重新判断这整段是否说完。
        self._audio = np.concatenate([self._audio, audio_array.astype(np.float32, copy=False)])

    def clear(self) -> None:
        """清空当前 turn 音频缓存。"""

        # 模型确认当前话已经说完后，旧音频属于上一个 turn，不能混入下一次 Smart Turn 判断。
        self._audio = np.zeros(0, dtype=np.float32)

    def build_smart_turn_input(self) -> np.ndarray:
        """构造固定长度的 Smart Turn 模型输入窗口。"""

        # 目标样本数对应模型最长输入窗口，例如 16k 采样率下 8 秒就是 128000 个样本。
        target_samples = self.sample_rate * self.smart_turn_max_seconds

        # 当前 turn 超过模型窗口时，只保留最近音频，让判断聚焦最后 8 秒的说话上下文。
        if self._audio.shape[0] >= target_samples:
            return self._audio[-target_samples:].astype(np.float32, copy=False)

        # 当前 turn 还不够模型窗口时，把有效音频放在末尾，前面补零对齐模型期望输入长度。
        padding = np.zeros(target_samples - self._audio.shape[0], dtype=np.float32)
        return np.concatenate([padding, self._audio.astype(np.float32, copy=False)])

