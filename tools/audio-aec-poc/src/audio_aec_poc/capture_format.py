"""统一设备采集格式与业务音频格式，避免 Console 和实时输入采用不同规则。"""

from __future__ import annotations

import sys
from typing import Any

import numpy as np


def soundcard_capture_channel_count(device: Any, output_channels: int) -> int:
    """为 soundcard 选择能保留正常波形的底层采集声道数。

    Args:
        device: 当前选中的 soundcard 麦克风或扬声器 loopback 设备。
        output_channels: 业务需要的输出声道数；它不代表 Windows 设备应如何打开。

    Returns:
        Windows 返回设备原生声道数，其他平台沿用业务声道数。

    Raises:
        ValueError: 声道数非法，或 Windows 设备只有 soundcard 无法可靠采集的单声道。
    """

    if output_channels < 1:
        raise ValueError("业务音频声道数必须大于零。")
    if sys.platform != "win32":
        return output_channels

    native_channels = int(getattr(device, "channels", output_channels))
    if native_channels < 1:
        raise ValueError("音频设备没有可采集的声道。")
    # Soundcard 的 Windows/WASAPI 单声道请求可能成功，却交付垃圾采样；不能用打开成功判断兼容。
    if native_channels == 1:
        raise ValueError("Windows soundcard 无法可靠采集单声道设备，麦克风需要改用 sounddevice。")
    return native_channels


def normalize_capture_audio(audio: Any, channels: int) -> np.ndarray:
    """把真实采集波形转换成业务需要的 float32 声道布局。

    Args:
        audio: 一维单声道或二维“样本帧 × 声道”的采集数组。
        channels: 业务需要的输出声道数。

    Returns:
        二维 float32 音频；单声道输出使用所有输入声道的平均值，其他输出保留前几个声道。

    Raises:
        ValueError: 声道数、数组形状、采样值非法，或输入声道不足以满足输出契约。
    """

    if channels < 1:
        raise ValueError("业务音频声道数必须大于零。")
    frame = np.asarray(audio, dtype=np.float32)
    if frame.ndim == 1:
        frame = frame.reshape(-1, 1)
    if frame.ndim != 2 or frame.shape[0] == 0 or frame.shape[1] == 0:
        raise ValueError("采集音频必须是非空的样本帧与声道数组。")
    if not np.isfinite(frame).all():
        raise ValueError("采集音频含有 NaN 或无穷大采样值。")
    if frame.shape[1] < channels:
        raise ValueError("采集音频声道数少于业务需要的声道数。")

    # 双声道麦克风的人声可能只在右侧；直接取第一列会误把正常人声丢掉。
    if channels == 1 and frame.shape[1] > 1:
        return np.mean(frame, axis=1, dtype=np.float32, keepdims=True)
    return frame[:, :channels]
