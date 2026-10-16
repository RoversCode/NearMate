from __future__ import annotations

from typing import Any

from livekit import rtc


class CleanMicPublisher:
    """将 clean 麦克风 PCM16 发布到 LiveKit AudioSource。

    功能：接收已经由 AEC 管线处理后的 clean PCM16 字节，封装为 LiveKit AudioFrame，并异步提交给 AudioSource。
    核心入参：audio_source 是具备 capture_frame 协程方法的 LiveKit 音频源，sample_rate 和 channels 描述 clean PCM16 格式。
    预期输出：publish_pcm16 将非空 PCM16 发布为一帧 AudioFrame，空字节块被跳过。
    边界异常：sample_rate 非正、channels 不是 1 或 PCM16 字节数为奇数时抛出 ValueError。
    """

    def __init__(self, audio_source: Any, sample_rate: int = 16000, channels: int = 1) -> None:
        """初始化 clean 麦克风发布器。

        功能：保存 LiveKit AudioSource 和音频格式参数，供后续发布每个 PCM16 数据块时复用。
        核心入参：audio_source 负责接收 AudioFrame，sample_rate 是 clean 输出采样率，channels 是声道数。
        预期输出：生成可异步发布 PCM16 的 CleanMicPublisher 实例。
        边界异常：sample_rate 不是正整数或 channels 不是 1 时抛出 ValueError；发布失败由 capture_frame 自身透传。
        """
        # 采样率必须是普通正整数，否则 AudioFrame 无法表达 clean 麦克风数据的真实时间轴。
        if not isinstance(sample_rate, int) or isinstance(sample_rate, bool) or sample_rate <= 0:
            raise ValueError("sample_rate must be greater than 0.")

        # 当前侧车只支持单声道 clean 麦克风，构造阶段拒绝其它声道数可以避免发布静默错误格式。
        if not isinstance(channels, int) or isinstance(channels, bool) or channels != 1:
            raise ValueError("channels must be 1.")

        # AudioSource 由真实 runtime 创建，本基础单元只负责把帧交给它。
        self._audio_source = audio_source
        # 采样率用于构造 LiveKit AudioFrame，默认与 SidecarConfig.clean_output_sample_rate 对齐。
        self._sample_rate = sample_rate
        # 声道数用于计算 samples_per_channel，当前配置层会把侧车限制为单声道。
        self._channels = channels

    async def publish_pcm16(self, pcm16: bytes) -> None:
        """发布一块 clean PCM16 音频数据。

        功能：跳过空数据，拒绝奇数字节，计算每声道样本数后构造 AudioFrame 并等待 AudioSource 捕获。
        核心入参：pcm16 是 clean 麦克风 PCM16 字节，长度应为完整 16 位样本的整数倍。
        预期输出：非空合法数据会被 capture_frame 异步接收；方法自身不返回业务值。
        边界异常：pcm16 长度为奇数时抛出 ValueError；capture_frame 的运行时异常会自然透传给调用方。
        """
        # 上游暂时没有 clean 音频时可能传入空块，跳过可避免向 LiveKit 发布无意义的零样本帧。
        if not pcm16:
            return

        # PCM16 每个样本占 2 字节，奇数字节说明缓冲区被截断，必须在构造 AudioFrame 前拒绝。
        if len(pcm16) % 2 != 0:
            raise ValueError("PCM16 data length must be an even number of bytes.")

        # 每声道样本数是 LiveKit AudioFrame 的关键元数据，单声道时等于总样本数。
        samples_per_channel = len(pcm16) // (self._channels * 2)
        # AudioFrame 保存 clean 麦克风数据和格式信息，后续由 LiveKit AudioSource 接入实时房间。
        frame = rtc.AudioFrame(
            data=pcm16,
            sample_rate=self._sample_rate,
            num_channels=self._channels,
            samples_per_channel=samples_per_channel,
        )

        # capture_frame 是异步入口，必须 await，确保调用方能按 LiveKit 背压节奏推进发布循环。
        await self._audio_source.capture_frame(frame)
