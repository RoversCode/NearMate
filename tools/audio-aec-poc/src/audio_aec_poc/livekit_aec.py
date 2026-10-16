from __future__ import annotations

from livekit import rtc


class LiveKitAecProcessor:
    """封装 LiveKit APM 的 10ms PCM16 回声消除处理器。

    功能：接收扬声器参考帧和麦克风帧，将二者转换为 LiveKit AudioFrame，并按 AEC 要求先处理参考流再处理麦克风流。
    核心入参：sample_rate 是 PCM16 音频采样率，channels 是声道数，两者共同决定每个 10ms 帧的字节数。
    预期输出：process_frame 返回经过 LiveKit APM 处理后的麦克风 PCM16 字节。
    边界异常：采样率无法整除 100、参考帧或麦克风帧不是精确 10ms 时抛出 ValueError。
    """

    def __init__(self, sample_rate: int, channels: int) -> None:
        """初始化 LiveKit AEC 处理器及底层 APM。

        功能：校验 10ms 帧长是否能用整数样本表示，并创建启用回声消除和高通滤波的 LiveKit APM。
        核心入参：sample_rate 是每秒采样数，channels 是 PCM16 音频声道数。
        预期输出：生成可连续处理固定 10ms 双流音频帧的处理器实例。
        边界异常：sample_rate 不能被 100 整除时抛出 ValueError，避免后续产生非 10ms 帧。
        """
        # LiveKit APM 按 10ms 帧工作，采样率必须能被 100 整除才能得到整数样本数。
        if sample_rate % 100 != 0:
            raise ValueError("sample_rate must be divisible by 100 to form exact 10ms frames.")

        # 采样率和声道数在整个处理器生命周期内保持不变，用于构造每一帧 AudioFrame。
        self._sample_rate = sample_rate
        self._channels = channels
        # 每声道 10ms 样本数是 LiveKit AudioFrame 的 samples_per_channel 参数。
        self._samples_per_channel = sample_rate // 100
        # 每帧总字节数按 PCM16 计算，每个样本固定占 2 字节。
        self._frame_bytes = self._samples_per_channel * channels * 2
        # AEC PoC 只开启回声消除和高通滤波，关闭降噪与自动增益以减少对双讲人声的额外误伤变量。
        self._apm = rtc.AudioProcessingModule(
            echo_cancellation=True,
            noise_suppression=False,
            high_pass_filter=True,
            auto_gain_control=False,
        )

    @property
    def frame_bytes(self) -> int:
        """返回单个 10ms PCM16 帧应包含的字节数。

        功能：为采集端或测试端提供统一切帧依据，避免调用方重复计算帧长。
        核心入参：无显式入参，使用初始化阶段保存的采样率和声道数。
        预期输出：返回 samples_per_channel * channels * 2 的整数结果。
        边界异常：初始化已保证采样率可形成 10ms 帧，因此本属性不会抛出业务异常。
        """
        # 帧长是公开只读配置，调用方可用它验证或切分 PCM16 数据块。
        return self._frame_bytes

    def process_frame(self, reference_pcm16: bytes, microphone_pcm16: bytes) -> bytes:
        """处理一对精确 10ms 的参考帧和麦克风帧。

        功能：校验两路 PCM16 帧长，先将扬声器参考帧送入 reverse stream，再处理麦克风帧并返回结果。
        核心入参：reference_pcm16 是扬声器 loopback 参考音频，microphone_pcm16 是同步采集的麦克风音频。
        预期输出：返回 LiveKit APM 处理后的麦克风帧字节。
        边界异常：任一路输入长度不是 frame_bytes 时抛出包含 10ms 的 ValueError。
        """
        # 参考帧必须精确匹配 10ms，否则 APM 无法建立与麦克风帧对齐的远端播放上下文。
        if len(reference_pcm16) != self._frame_bytes:
            raise ValueError("reference_pcm16 must contain exactly one 10ms PCM16 frame.")

        # 麦克风帧同样必须精确匹配 10ms，避免 LiveKit 按错误帧长执行近端流处理。
        if len(microphone_pcm16) != self._frame_bytes:
            raise ValueError("microphone_pcm16 must contain exactly one 10ms PCM16 frame.")

        # 参考帧携带扬声器播放内容，先进入 reverse stream 供 AEC 建立回声参考。
        reference_frame = rtc.AudioFrame(
            data=reference_pcm16,
            sample_rate=self._sample_rate,
            num_channels=self._channels,
            samples_per_channel=self._samples_per_channel,
        )
        # 麦克风帧是待处理的近端输入，APM 可能会原地更新其 data 缓冲区。
        microphone_frame = rtc.AudioFrame(
            data=microphone_pcm16,
            sample_rate=self._sample_rate,
            num_channels=self._channels,
            samples_per_channel=self._samples_per_channel,
        )

        # AEC 的正确数据流顺序是先提供远端参考，再处理近端麦克风输入。
        self._apm.process_reverse_stream(reference_frame)
        # 在参考上下文已经进入 APM 后处理麦克风帧，得到消回声后的近端音频。
        self._apm.process_stream(microphone_frame)

        # 将 LiveKit 帧数据转回不可变 bytes，便于调用方继续写文件或送入后续管线。
        return bytes(microphone_frame.data)
