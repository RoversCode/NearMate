from __future__ import annotations

from dataclasses import dataclass, field

from livekit import rtc


LIVEKIT_AUDIO_FRAME_DURATION_MS = 10


@dataclass
class AssistantAudioPublisher:
    """把助手 TTS 音频写入 LiveKit 本地音频源。

    Args:
        audio_source: LiveKit `rtc.AudioSource`，或测试中兼容 `capture_frame` 的替身。
    """

    # 发布器只依赖最小音频源协议，方便下行调度器在测试中注入替身。
    audio_source: object
    _queue_generation: int = field(default=0, init=False, repr=False)

    async def publish_pcm16(self, *, pcm16: bytes, sample_rate: int = 16000) -> None:
        """逐帧发布单声道音频；清队列后停止本段剩余帧。

        Args:
            pcm16: 小端序 16 位单声道 PCM 音频字节。
            sample_rate: 当前音频块的采样率，必须与底层 `AudioSource` 一致。

        Returns:
            None: 音频帧写入 `AudioSource`；被打断的旧段不再继续入队。

        Raises:
            ValueError: 当采样率不匹配，或 PCM16 字节长度不是 2 的倍数时抛出。
        """

        queue_generation = self._queue_generation
        # TTS flush 可能产生空块；空音频不是媒体事实，直接跳过即可。
        if not pcm16:
            return

        # 同一条 LiveKit 音频源只能保持一种采样率，混入其它格式会导致播放速度异常。
        expected_sample_rate = int(getattr(self.audio_source, "sample_rate", sample_rate))
        if sample_rate != expected_sample_rate:
            raise ValueError(f"assistant audio publisher expects {expected_sample_rate} Hz PCM16")

        # PCM16 必须按 16 位样本对齐，提前拒绝能把格式错误留在媒体边界内。
        if len(pcm16) % 2 != 0:
            raise ValueError("assistant audio publisher expects even-length PCM16 bytes")

        # WebRTC 音频链路按短帧稳定推进；大块 TTS 直接入源容易让接收端抖动缓冲在开头丢字。
        frame_samples = _samples_per_frame(sample_rate=sample_rate)
        frame_bytes = frame_samples * 2
        for offset in range(0, len(pcm16), frame_bytes):
            # capture_frame 可能等待底层队列腾位置。用户在等待期间打断后，
            # 只能让当前 10ms 帧收口，不能把同一旧句子的余下音频重新填回空队列。
            if queue_generation != self._queue_generation:
                return
            frame_pcm16 = pcm16[offset : offset + frame_bytes]
            samples_per_channel = len(frame_pcm16) // 2
            frame = rtc.AudioFrame(
                frame_pcm16,
                sample_rate,
                1,
                samples_per_channel,
            )
            await self.audio_source.capture_frame(frame)

    async def wait_for_playout(self) -> None:
        """等待已写入底层音频源的内容播放完毕。

        Returns:
            None: 底层音频源播放缓存耗尽后返回；测试替身不支持该能力时直接返回。
        """

        # 助手语音真正播完后再释放 idle，避免 UI 状态早于用户听感完成。
        wait_for_playout = getattr(self.audio_source, "wait_for_playout", None)
        if not callable(wait_for_playout):
            return

        await wait_for_playout()

    def clear_queue(self) -> None:
        """让在途旧段停止继续发帧，再清空底层播放缓存。

        Returns:
            None: 旧段失效，底层播放缓存被清空；之后的新段使用新的代际正常发布。
        """

        # 先失效正在 await capture_frame 的旧段，避免清完缓存后又被它填回旧语音。
        self._queue_generation += 1
        # 插话取消和断连需要立即丢弃旧语音，防止已经排队的音频继续播放。
        clear_queue = getattr(self.audio_source, "clear_queue", None)
        if callable(clear_queue):
            clear_queue()

    async def aclose(self) -> None:
        """关闭底层音频源资源。

        Returns:
            None: 底层音频源完成关闭；测试替身不支持该能力时直接返回。
        """

        # room 切换和应用退出时必须释放 SDK 资源，避免媒体句柄泄漏。
        close_source = getattr(self.audio_source, "aclose", None)
        if not callable(close_source):
            return

        await close_source()


def create_assistant_audio_publisher(
    *,
    track_name: str = "assistant-audio",
    sample_rate: int = 24000,
    queue_size_ms: int = 1000,
    loop: object | None = None,
) -> tuple[AssistantAudioPublisher, object]:
    """创建助手下行音频发布器和对应的 LiveKit 本地音频轨。

    Args:
        track_name: 发布到 room 后供 desktop 识别的本地音频轨名称。
        sample_rate: 助手 TTS 输出采样率。
        queue_size_ms: LiveKit 音频源内部播放队列长度。
        loop: `AudioSource` 绑定的事件循环。

    Returns:
        tuple[AssistantAudioPublisher, object]: 音频发布器和待发布到 room 的本地音频轨。
    """

    # AudioSource 直接使用 TTS 输出采样率，避免在进入 LiveKit 前增加额外重采样边界。
    audio_source = rtc.AudioSource(sample_rate, 1, queue_size_ms=queue_size_ms, loop=loop)
    # 轨道名是桌面端订阅助手语音的稳定协议字段。
    audio_track = rtc.LocalAudioTrack.create_audio_track(track_name, audio_source)
    return AssistantAudioPublisher(audio_source=audio_source), audio_track


def _samples_per_frame(*, sample_rate: int) -> int:
    """计算 LiveKit 下行发布使用的单帧样本数。

    Args:
        sample_rate: PCM16 音频采样率。

    Returns:
        单个 10ms 音频帧包含的每声道样本数。

    Raises:
        ValueError: 采样率无法整除 100 时抛出。
    """

    # 10ms 是 WebRTC 音频处理的稳定帧长，采样率必须能精确换算，避免时间轴逐帧漂移。
    if sample_rate % (1000 // LIVEKIT_AUDIO_FRAME_DURATION_MS) != 0:
        raise ValueError("assistant audio publisher expects sample rate divisible by 100")

    return sample_rate * LIVEKIT_AUDIO_FRAME_DURATION_MS // 1000


class AssistantVideoPublisher:
    """预留助手视频轨发布边界。"""

    async def publish_rgb24_frame(self, *, frame_bytes: bytes, width: int, height: int) -> None:
        """发布一帧 RGB24 视频画面。

        Args:
            frame_bytes: RGB24 原始帧字节。
            width: 视频帧宽度。
            height: 视频帧高度。

        Returns:
            None: 当前版本尚未实现视频发布。

        Raises:
            NotImplementedError: 当前版本始终抛出，避免调用方误认为视频链路已接通。
        """

        # 视频发布尚未接入真实链路，显式失败比静默丢帧更容易暴露误接线。
        raise NotImplementedError("assistant video publisher is reserved for a future task")

