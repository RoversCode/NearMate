from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from collections.abc import Callable
from threading import RLock

from companion.audio.bootstrap.qwen_asr_config import (
    QwenAsrConfig,
    QwenAsrDecodeProfile,
    QwenAsrEngineConfig,
    QwenAsrSessionConfig,
    build_qwen_asr_config_from_model_dir,
)
from companion.audio.providers.base import (
    AsrProvider,
    SmartTurnProvider,
    SpeakerVerificationProvider,
    TextEouProvider,
    VadProvider,
    WakewordProvider,
)
from companion.audio.providers.asr_provider import AsrProviderConfig, RealAsrProvider
from companion.audio.providers.smart_turn_provider import RealSmartTurnProvider, SmartTurnProviderConfig
from companion.audio.providers.text_eou_provider import RealTextEouProvider, TextEouProviderConfig
from companion.audio.providers.vad_provider import SileroVadProvider, SileroVadProviderConfig
from companion.audio.providers.wakeword_provider import RealWakewordProvider, WakewordProviderConfig
from companion.audio.providers.speaker_verification_provider import (
    RealSpeakerVerificationProvider,
    SpeakerVerificationProviderConfig,
)
from companion.audio.bootstrap.realtime_asr_runtime import (
    build_realtime_asr_runtime_config,
)
from companion.audio.bootstrap.smart_turn_runtime import build_smart_turn_runtime_config
from companion.audio.bootstrap.wakeword_runtime import build_wakeword_runtime_config
from companion.audio.bootstrap.speaker_verification_runtime import (
    build_speaker_verification_runtime_config,
    create_speaker_embedding_extractor,
    load_whitelist_embeddings,
)
from companion.core.settings import Settings
from companion.config.realtime_audio import RealtimeAudioYamlConfig, load_realtime_audio_config
from companion.observability.logging_config import get_backend_logger
from companion.realtime.runtime_session import InputTurnSession
from companion.realtime.interaction_state import InteractionState, TurnState
from companion.wake_response.service import WakeResponseService


logger = get_backend_logger(__name__)


class RealtimeInputRuntime:
    """运行时音频控制消息管理器。"""

    def __init__(
        self,
        *,
        wakeword_provider: WakewordProvider | None = None,
        vad_provider: VadProvider | None = None,
        asr_provider: AsrProvider | None = None,
        smart_turn_provider: SmartTurnProvider | None = None,
        text_eou_provider: TextEouProvider | None = None,
        speaker_verification_provider: SpeakerVerificationProvider | None = None,
        wake_response_service: WakeResponseService | None = None,
        wake_response_config_provider: Callable[[], dict[str, object]] | None = None,
        realtime_audio_config: RealtimeAudioYamlConfig | None = None,
        followup_timeout_s: float = 5.0,
    ) -> None:
        """初始化实时输入运行时，并拒绝缺少生产必需依赖的半成品实例。

        Args:
            wakeword_provider: 从音频帧识别唤醒词的提供器。
            vad_provider: 判断用户是否开始或停止说话的语音活动检测提供器。
            asr_provider: 把用户语音转换成文字的识别提供器。
            smart_turn_provider: 判断一段用户表达是否已经说完整的提供器。
            text_eou_provider: 辅助判断文字输入是否表达完整的可选提供器。
            speaker_verification_provider: 校验说话人身份的可选提供器。
            wake_response_service: 生成并播放唤醒回应的可选服务。
            wake_response_config_provider: 返回最新唤醒回应配置的可选函数。
            realtime_audio_config: 当前实时音频行为的完整配置快照。
            followup_timeout_s: 一次唤醒后继续接纳用户追问的秒数。

        Raises:
            RuntimeError: 缺少必需提供器或实时音频配置。
            ValueError: 语音活动检测窗口不是正数。
        """

        # 实时输入四件套已经进入真实链路；缺少任一项都不能继续构造，避免 RealtimeInputRuntime 偷偷降级成假运行时。
        required_providers = {
            "wakeword_provider": wakeword_provider,
            "vad_provider": vad_provider,
            "asr_provider": asr_provider,
            "smart_turn_provider": smart_turn_provider,
        }
        for provider_name, provider in required_providers.items():
            # 逐项报错可以让调用方快速定位缺失依赖，而不是在 LiveKit 媒体会话启动后才遇到 NoneType。
            if provider is None:
                raise RuntimeError(f"{provider_name} 必须显式注入，生产路径请使用 RealtimeInputRuntime.from_settings")

        if realtime_audio_config is None:
            raise RuntimeError("realtime_audio_config 必须显式注入")
        # VAD 开口确认窗口必须为正数，否则无法判断用户何时正式开始讲话。
        if realtime_audio_config.vad.speech_start_window_count <= 0:
            raise ValueError("vad.speech_start_window_count 必须为正数")

        # 唤醒词、VAD、ASR 和 Smart Turn 都来自显式依赖，RealtimeInputRuntime 不再创建任何输入侧 fake/default provider。
        self.wakeword_provider = wakeword_provider
        self.vad_provider = vad_provider
        self.asr_provider = asr_provider
        self.smart_turn_provider = smart_turn_provider
        self.text_eou_provider = text_eou_provider
        self.realtime_audio_config = realtime_audio_config
        # 说话人确认 provider 可选，未启用时为 None。
        self.speaker_verification_provider = speaker_verification_provider
        # 唤醒回复服务只在生产组合根注入；测试或输入验证模式未注入时保持纯文字旧行为。
        self.wake_response_service = wake_response_service
        self.wake_response_config_provider = wake_response_config_provider
        self._bound_realtime_input_workers: list[object] = []
        # 唤醒词 provider 支持 console 热替换；检测、reset 和替换必须共享一把锁，避免音频线程读到半切换状态。
        self._wakeword_provider_lock = RLock()
        # 说话人确认 provider 支持 console 热替换；更新 RealtimeInputRuntime 和当前 worker 引用必须在同一把锁内完成。
        self._speaker_verification_provider_lock = RLock()

        # InputTurnSession 只管理用户输入 turn 的状态迁移，避免把状态规则散落在音频字节处理分支里。
        self.input_turn_session = InputTurnSession.empty()
        # 管理产品语义上的运行期会话和追问热区；assistant 子进程只通过 interaction_session_id 对齐历史生命周期。
        self.interaction_state = InteractionState(followup_timeout_s=followup_timeout_s)
        # 当前音频会话标识由真实媒体边界显式绑定；未绑定前保持空值，避免事件误落到假会话。
        self.current_audio_session_id = ""
        # Runtime 身份由进程级 Owner 在发布前一次性绑定；候选对象不能自行编号。
        self.input_runtime_id = ""
        self.input_runtime_epoch = 0

    def bind_runtime_identity(
        self,
        *,
        input_runtime_id: str,
        input_runtime_epoch: int,
    ) -> None:
        """在 Runtime 发布前绑定不可改写的进程级身份。

        Args:
            input_runtime_id: 带 ``input-runtime-`` 前缀的 UUID 身份。
            input_runtime_epoch: 当前 Backend 进程内单调递增的正整数。

        Returns:
            无返回值；绑定身份并建立交互快照的初始版本。

        Raises:
            ValueError: 身份格式或 epoch 非法时抛出。
            RuntimeError: 已绑定身份被尝试改写时抛出。
        """

        normalized_runtime_id = input_runtime_id.strip()
        if not normalized_runtime_id.startswith("input-runtime-"):
            raise ValueError("input_runtime_id 必须使用 input-runtime- 前缀")
        if input_runtime_epoch <= 0:
            raise ValueError("input_runtime_epoch 必须是正整数")
        if self.input_runtime_id or self.input_runtime_epoch:
            if (
                self.input_runtime_id == normalized_runtime_id
                and self.input_runtime_epoch == input_runtime_epoch
            ):
                return
            raise RuntimeError("输入 Runtime 身份一旦绑定就不能改写")
        self.input_runtime_id = normalized_runtime_id
        self.input_runtime_epoch = input_runtime_epoch
        # 正式发布前建立初始版本，随后第一次真实状态变化就会推进版本。
        self.interaction_state.public_snapshot()

    def runtime_identity_payload(self) -> dict[str, object]:
        """返回所有语音公共事件共用的 Runtime 身份字段。

        Returns:
            包含 Runtime UUID 身份和单调 epoch 的字典。

        Raises:
            RuntimeError: Owner 尚未绑定身份时抛出。
        """

        if not self.input_runtime_id or self.input_runtime_epoch <= 0:
            raise RuntimeError("输入 Runtime 尚未绑定进程级身份")
        return {
            "input_runtime_id": self.input_runtime_id,
            "input_runtime_epoch": self.input_runtime_epoch,
        }

    def current_turn_identity_payload(self) -> dict[str, object]:
        """返回当前语音 Turn 的完整关联身份。

        Returns:
            Runtime 身份、可读 Turn 编号和数值 Turn 顺序。

        Raises:
            RuntimeError: 当前没有可归属的 Turn 时抛出。
        """

        turn_id = self.input_turn_session.current_turn_id
        turn_sequence = self.input_turn_session.current_turn_sequence
        if not turn_id or turn_sequence is None or turn_sequence <= 0:
            raise RuntimeError("当前没有可归属的输入 Turn")
        return {
            **self.runtime_identity_payload(),
            "turn_id": turn_id,
            "turn_sequence": turn_sequence,
        }

    def interaction_state_event(self, *, source: str | None = None) -> dict[str, object]:
        """构造带版本的完整交互快照，供事件、HTTP 回执和重连共用。

        Args:
            source: 手动切换时为 ``manual_toggle``；普通更新和重连时省略。

        Returns:
            具有当前输入 Runtime 身份和权威交互状态的公共事件。

        Raises:
            ValueError: 来源不是约定的手动切换标记。
        """

        if source not in {None, "manual_toggle"}:
            raise ValueError("交互状态来源非法")
        payload = {**self.runtime_identity_payload(), **self.interaction_state.public_snapshot()}
        if source is not None:
            payload["source"] = source
        return {"type": "interaction.state_changed", "payload": payload}

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        wakeword_keywords: tuple[str, ...],
        vad_session_factory: Callable[[object], object] | None = None,
        wakeword_spotter: object | None = None,
    ) -> "RealtimeInputRuntime":
        """从生产配置装配实时语音运行时。"""

        # Qwen ASR 和 VAD相关配置
        realtime_audio_config = load_realtime_audio_config(execution_config_path=Path(settings.execution_config_path))

        realtime_asr_config = build_realtime_asr_runtime_config()
        realtime_asr_config = replace(
            realtime_asr_config,
            vad=replace(realtime_asr_config.vad, provider=realtime_audio_config.vad.provider),
        )
        qwen_asr_model_dir = Path(settings.backend_ckpt_dir) / "qwen_asr"

        # 实时语音识别配置一次性收敛成完整业务配置，避免调用方再对 base config 打补丁。
        qwen_asr_config = cls.build_qwen_asr_config(
            model_dir=qwen_asr_model_dir,
            realtime_audio_config=realtime_audio_config,
        )
        # 真实 VAD 是实时输入的必要组件，创建 Runtime 时即初始化；其他重型 Provider 仍按自身生命周期处理。
        # 说话人确认可选，按开关与白名单条件加载。
        speaker_verification_provider = cls.build_speaker_verification_provider(
            settings,
            realtime_audio_config,
        )
        return cls(
            wakeword_provider=cls.build_wakeword_provider(
                settings=settings,
                realtime_audio_config=realtime_audio_config,
                keywords=wakeword_keywords,
                wakeword_spotter=wakeword_spotter,
            ),
            vad_provider=SileroVadProvider(
                config=SileroVadProviderConfig(
                    runtime_config=realtime_asr_config.vad,
                    session_factory=vad_session_factory,
                )
            ),
            asr_provider=RealAsrProvider(
                config=AsrProviderConfig(model_dir=qwen_asr_config.assets.model_dir, runtime_config=qwen_asr_config),
            ),
            smart_turn_provider=RealSmartTurnProvider(
                config=SmartTurnProviderConfig(
                    runtime_config=build_smart_turn_runtime_config(
                        settings,
                        provider=realtime_audio_config.smart_turn.provider,
                    )
                )
            ),
            text_eou_provider=cls.build_text_eou_provider(realtime_audio_config),
            speaker_verification_provider=speaker_verification_provider,
            realtime_audio_config=realtime_audio_config,
            followup_timeout_s=settings.followup_timeout_s,
        )

    @classmethod
    def build_text_eou_provider(
        cls,
        realtime_audio_config: RealtimeAudioYamlConfig,
    ) -> RealTextEouProvider | None:
        """按 YAML 配置构造文本 EOU provider。"""

        if not realtime_audio_config.text_eou.enabled:
            return None

        return RealTextEouProvider(
            config=TextEouProviderConfig(
                model_dir=realtime_audio_config.text_eou.model_dir,
                lang=realtime_audio_config.text_eou.lang,
                providers=(realtime_audio_config.text_eou.provider,),
            )
        )

    @classmethod
    def build_speaker_verification_provider(
        cls,
        settings: Settings,
        realtime_audio_config: RealtimeAudioYamlConfig,
    ) -> RealSpeakerVerificationProvider | None:
        """按 settings 条件构造说话人确认 provider。"""

        # 开关关闭或白名单为空时不加载模型，避免无用开销。
        if not settings.speaker_verification_enabled:
            return None
        if not settings.speaker_whitelist_audio_paths:
            return None

        runtime_config = build_speaker_verification_runtime_config(
            settings,
            realtime_audio_config=realtime_audio_config,
        )
        extractor = create_speaker_embedding_extractor(runtime_config)
        whitelist = load_whitelist_embeddings(extractor, runtime_config)
        return RealSpeakerVerificationProvider(
            config=SpeakerVerificationProviderConfig(
                whitelist=whitelist,
                extractor=extractor,
                sample_rate=runtime_config.sample_rate,
            )
        )

    @classmethod
    def build_qwen_asr_config(
        cls,
        *,
        model_dir: Path | str,
        realtime_audio_config: RealtimeAudioYamlConfig,
    ) -> QwenAsrConfig:
        """按当前实时音频配置构造 Qwen ASR 业务配置。"""

        # 资产路径与默认语言上下文来自仓库内 builder，YAML 只覆盖运行设备与 decode 策略。
        base_config = build_qwen_asr_config_from_model_dir(model_dir)
        partial_profile = QwenAsrDecodeProfile(
            chunk_size_sec=realtime_audio_config.asr.session.partial.chunk_size_sec,
            memory_chunks=realtime_audio_config.asr.session.partial.memory_chunks,
            apply_hotwords=False,
            window_sec=realtime_audio_config.asr.session.partial.window_sec,
        )
        final_profile = QwenAsrDecodeProfile(
            chunk_size_sec=realtime_audio_config.asr.session.final.chunk_size_sec,
            memory_chunks=realtime_audio_config.asr.session.final.memory_chunks,
            apply_hotwords=True,
            window_sec=None,
        )
        return QwenAsrConfig(
            assets=base_config.assets,
            engine=QwenAsrEngineConfig(
                onnx_provider=realtime_audio_config.asr.engine.onnx_provider,
                llm_backend=realtime_audio_config.asr.engine.llm_backend,
                verbose=realtime_audio_config.asr.engine.verbose,
                encoder_intra_threads=base_config.engine.encoder_intra_threads,
                encoder_inter_threads=base_config.engine.encoder_inter_threads,
            ),
            session=QwenAsrSessionConfig(
                language=base_config.session.language,
                context=base_config.session.context,
                hotwords=realtime_audio_config.asr.session.hotwords,
                partial=partial_profile,
                final=final_profile,
            ),
        )

    @classmethod
    def build_wakeword_provider(
        cls,
        *,
        settings: Settings,
        realtime_audio_config: RealtimeAudioYamlConfig,
        keywords: tuple[str, ...],
        wakeword_spotter: object | None = None,
    ) -> WakewordProvider:
        """按当前设置构造唤醒词 provider。

        Args:
            settings: 当前运行设置，提供模型目录和用户配置的唤醒词。
            realtime_audio_config: 实时音频 YAML 配置，提供 sherpa 执行参数。
            keywords: 本次要生效的唤醒词列表。
            wakeword_spotter: 可选测试替身。

        Returns:
            已经完成初始化的唤醒词 provider。
        """

        # 唤醒词文本属于用户设置；provider/device/num_threads 仍属于底层音频运行参数。
        wakeword_runtime_config = build_wakeword_runtime_config(
            settings,
            keywords=keywords,
            provider=realtime_audio_config.wakeword.provider,
            device=realtime_audio_config.wakeword.device,
            num_threads=realtime_audio_config.wakeword.num_threads,
        )
        return RealWakewordProvider(
            config=WakewordProviderConfig(
                model_dir=settings.wakeword_model_dir,
                runtime_config=wakeword_runtime_config,
                keywords=keywords,
                provider=realtime_audio_config.wakeword.provider,
                device=realtime_audio_config.wakeword.device,
                num_threads=realtime_audio_config.wakeword.num_threads,
            ),
            spotter=wakeword_spotter,
        )

    def replace_wakeword_keywords(
        self,
        *,
        settings: Settings,
        realtime_audio_config: RealtimeAudioYamlConfig,
        keywords: tuple[str, ...],
    ) -> None:
        """使用新的唤醒词文本重建并替换 provider。

        Args:
            settings: 当前运行设置。
            realtime_audio_config: 实时音频 YAML 配置。
            keywords: 本次要生效的唤醒词列表。

        Returns:
            None: 新 provider 构造成功后才会替换旧 provider。
        """

        masked_keywords = _mask_wakeword_keywords(keywords)
        logger.info(
            "开始重建唤醒词 provider keyword_count=%s keywords=%s provider=%s device=%s num_threads=%s",
            len(keywords),
            masked_keywords,
            realtime_audio_config.wakeword.provider,
            realtime_audio_config.wakeword.device,
            realtime_audio_config.wakeword.num_threads,
        )
        try:
            new_provider = self.build_wakeword_provider(
                settings=settings,
                realtime_audio_config=realtime_audio_config,
                keywords=keywords,
            )
        except Exception:
            logger.exception(
                "唤醒词 provider 替换失败 keyword_count=%s keywords=%s provider=%s device=%s num_threads=%s",
                len(keywords),
                masked_keywords,
                realtime_audio_config.wakeword.provider,
                realtime_audio_config.wakeword.device,
                realtime_audio_config.wakeword.num_threads,
            )
            raise

        self.replace_wakeword_provider(new_provider)
        logger.info(
            "唤醒词 provider 替换成功 keyword_count=%s keywords=%s provider=%s device=%s num_threads=%s",
            len(keywords),
            masked_keywords,
            realtime_audio_config.wakeword.provider,
            realtime_audio_config.wakeword.device,
            realtime_audio_config.wakeword.num_threads,
        )

    def bind_realtime_input_worker(self, worker: object) -> None:
        """绑定当前正在消费音频的 worker。

        参数：
            worker: 持有 speaker_verification_provider 字段的音频 worker。

        返回：
            None: 后续说话人 provider 热替换会同步更新该 worker。
        """

        with self._speaker_verification_provider_lock:
            if worker in self._bound_realtime_input_workers:
                return
            self._bound_realtime_input_workers.append(worker)
            setattr(worker, "speaker_verification_provider", self.speaker_verification_provider)

    def replace_speaker_verification_provider(self, provider: SpeakerVerificationProvider | None) -> None:
        """替换说话人确认 provider 并同步当前 worker。

        参数：
            provider: 新的说话人确认 provider；None 表示关闭说话人确认。

        返回：
            None: 替换完成后新一轮检测会读取最新 provider。
        """

        with self._speaker_verification_provider_lock:
            self.speaker_verification_provider = provider
            for worker in list(self._bound_realtime_input_workers):
                setattr(worker, "speaker_verification_provider", provider)

    def rebuild_speaker_verification_provider(self, settings: Settings) -> None:
        """按最新 Settings 重建并发布说话人确认 provider。

        参数：
            settings: 已写入最新开关和白名单路径的运行设置。

        返回：
            None: 新 provider 构造成功后才发布；构造失败时保留旧 provider。
        """

        new_provider = self.build_speaker_verification_provider(
            settings,
            self.realtime_audio_config,
        )
        self.replace_speaker_verification_provider(new_provider)

    def handle_audio_control(self, message: dict) -> dict:
        """处理运行时内部音频控制消息并返回确认响应。"""

        # 将原始 payload 视为内部可选上下文；真实 LiveKit 链路会在媒体会话启动时绑定音频来源。
        payload = message.get("payload", {})

        # 兼容内部控制入口：流开始事件必须提供真实来源会话，不能退回假会话。
        if message["type"] == "audio.stream_started":
            self.bind_audio_session(payload.get("session_id", ""))

        # 回传同一事件类型并追加 accepted 标记，保持 runtime 内部调用方可以确认上下文已经接收。
        return {
            "type": message["type"],
            "payload": {
                **payload,
                "accepted": True,
            },
        }

    def bind_audio_session(self, source_session_id: str) -> None:
        """绑定实时音频媒体会话标识。

        Args:
            source_session_id: 真实媒体边界标识，当前 LiveKit 链路使用 room_name。

        Returns:
            None: 成功后更新 `current_audio_session_id`。
        """

        # 来源会话是运行时事件、调试录音和助手请求的归属边界，空白值必须尽早暴露。
        normalized_source_session_id = source_session_id.strip()
        if not normalized_source_session_id:
            raise ValueError("source_session_id 必须是非空字符串")

        self.current_audio_session_id = normalized_source_session_id

    def shutdown(self) -> None:
        """关闭 RealtimeInputRuntime 持有的可关闭 provider 资源。"""

        # 这些 provider 是 RealtimeInputRuntime 直接持有的输入侧资源边界；输出侧资源由独立助手运行时负责收口。
        providers = (
            self.wakeword_provider,
            self.vad_provider,
            self.asr_provider,
            self.smart_turn_provider,
            self.speaker_verification_provider,
        )
        seen_provider_ids: set[int] = set()
        shutdown_errors: list[Exception] = []

        for provider in providers:
            # 同一个 provider 可能被测试或未来装配复用于多个字段，按对象身份去重可以避免二次释放。
            provider_id = id(provider)
            if provider_id in seen_provider_ids:
                continue
            seen_provider_ids.add(provider_id)

            # 不是所有 provider 都持有需要显式释放的资源；没有 shutdown 方法时安全跳过。
            shutdown_provider = getattr(provider, "shutdown", None)
            if not callable(shutdown_provider):
                continue

            try:
                shutdown_provider()
            except Exception as exception:
                # 退出阶段不能让单个 provider 阻断其它 provider 释放；先收集，循环结束后统一抛出。
                shutdown_errors.append(exception)

        if shutdown_errors:
            raise ExceptionGroup("RealtimeInputRuntime provider shutdown failed", shutdown_errors)

    def detect_wakeword(self, pcm16: bytes) -> bool:
        """使用当前唤醒词 provider 检测一帧音频。

        Args:
            pcm16: 当前音频帧的 PCM16 字节。

        Returns:
            当前 provider 返回的唤醒命中结果。
        """

        # provider 可由控制台热替换，检测必须在锁内读取和调用同一个实例。
        with self._wakeword_provider_lock:
            return self.wakeword_provider.detect(pcm16)

    def reset_wakeword_provider(self) -> None:
        """重置当前唤醒词 provider 的流式状态。"""

        # track 切换或输入重置时只作用于当前 provider，避免旧 provider 被替换后仍被 reset。
        with self._wakeword_provider_lock:
            reset = getattr(self.wakeword_provider, "reset", None)
            if callable(reset):
                reset()

    def replace_wakeword_provider(self, wakeword_provider: WakewordProvider) -> None:
        """替换当前唤醒词 provider。

        Args:
            wakeword_provider: 已经构造成功的新唤醒词 provider。

        Returns:
            None: 替换成功后旧 provider 会在锁外尝试释放。
        """

        # 新 provider 在进入本方法前已经构造成功；这里仅做极短临界区指针切换。
        with self._wakeword_provider_lock:
            old_provider = self.wakeword_provider
            self.wakeword_provider = wakeword_provider

        logger.info(
            "唤醒词 provider 指针替换完成 old_provider=%s new_provider=%s",
            type(old_provider).__name__,
            type(wakeword_provider).__name__,
        )

        if old_provider is wakeword_provider:
            return

        shutdown = getattr(old_provider, "shutdown", None)
        if callable(shutdown):
            try:
                shutdown()
            except Exception:
                logger.warning(
                    "旧唤醒词 provider 释放失败，但新 provider 已经生效 old_provider=%s",
                    type(old_provider).__name__,
                    exc_info=True,
                )

    def apply_wake_detected(self, *, now: float) -> list[dict]:
        """应用已确认的唤醒，并附带与取消回执可比较的发生时间。

        Args:
            now: Worker 确认唤醒的单调时间。

        Returns:
            唤醒、可选短答音频、兼容跟进事件和完整交互快照。
        """

        # worker 已经完成唤醒判断，这里只同步产品交互窗口，避免 RealtimeInputRuntime 再保留一套音频态。
        self.interaction_state.on_wake_word(now=now)

        # 唤醒事件先发出，再追加会话窗口状态，方便前端按固定顺序打开交互 UI。
        wake_payload: dict[str, object] = {
            **self.runtime_identity_payload(),
            "session_id": self.current_audio_session_id,
            "detected_at": now,
        }
        audio_events: list[dict] = []
        if self.wake_response_service is not None and self.wake_response_config_provider is not None:
            selected = self.wake_response_service.select_for_time(
                self.wake_response_service.normalize_config(self.wake_response_config_provider())
            )
            if selected is not None:
                wake_payload["text"] = selected.text
                selected_audio = self.wake_response_service.read_audio(selected)
                if selected_audio is not None:
                    playback_delay_s = self.realtime_audio_config.wake_response.playback_delay_ms / 1000.0
                    audio_events = [
                        {
                            "type": "wake_response.audio_chunk",
                            "payload": {
                                **self.runtime_identity_payload(),
                                "session_id": self.current_audio_session_id,
                                "pcm16_b64": __import__("base64").b64encode(selected_audio.pcm16).decode("ascii"),
                                "sample_rate": selected_audio.sample_rate,
                                "playback_delay_s": playback_delay_s,
                            },
                        },
                        {
                            "type": "wake_response.audio_finished",
                            "payload": {
                                **self.runtime_identity_payload(),
                                "session_id": self.current_audio_session_id,
                            },
                        },
                    ]

        return [
            {
                "type": "wake.detected",
                "payload": wake_payload,
            },
            *audio_events,
            {
                "type": "followup.state_changed",
                "payload": {
                    **self.runtime_identity_payload(),
                    "followup_state": self.interaction_state.followup_state,
                },
            },
            self.interaction_state_event(),
        ]

    def apply_timeouts(self, *, now: float) -> list[dict]:
        """惰性应用交互窗口和追问热区的时间衰减。

        Args:
            now: Worker 当前观察到的单调时间。

        Returns:
            发生冷却时返回兼容跟进事件与完整快照；没有变化时为空列表。
        """

        result = self.interaction_state.apply_timeouts(now=now)
        events: list[dict] = []
        if result.followup_changed:
            events.append(
                {
                    "type": "followup.state_changed",
                    "payload": {
                        **self.runtime_identity_payload(),
                        "followup_state": self.interaction_state.followup_state,
                    },
                }
            )
            events.append(self.interaction_state_event())
        return events

    def apply_followup_toggle(self, *, now: float) -> tuple[bool, str, list[dict], str | None]:
        """应用用户手动切换追问热区的控制意图。

        Args:
            now: 控制意图到达后端的单调时间。

        Returns:
            tuple[bool, str, list[dict], str | None]: 是否接受、新追问状态、待广播事件和拒绝原因。
        """

        result = self.interaction_state.toggle_followup_state(now=now)
        if not result.accepted:
            return False, result.followup_state, [], result.rejected_reason

        return (
            True,
            result.followup_state,
            [
                {
                    "type": "followup.state_changed",
                    "payload": {
                        **self.runtime_identity_payload(),
                        "followup_state": result.followup_state,
                        "source": "manual_toggle",
                    },
                },
                self.interaction_state_event(source="manual_toggle"),
            ],
            None,
        )

    def apply_turn_state_changed(
        self,
        turn_state: TurnState,
        *,
        now: float,
        reason: str | None = None,
        turn_id: str | None = None,
        generation_id: str | None = None,
    ) -> list[dict]:
        """统一更新产品层 turn 状态并产出控制面事件。

        Args:
            turn_state: 目标产品层 turn 状态，只允许 idle/listening/processing/speaking。
            now: 状态事实发生时间，用于刷新会话活动窗口。
            reason: 可选状态变化原因，便于前端和日志区分来源。
            turn_id: 可选用户语音 turn 标识。
            generation_id: 可选助手输出 generation 标识。

        Returns:
            list[dict]: 兼容轮次事件，以及带版本的完整交互状态快照。
        """

        # 输入和助手输出各自拥有生命周期；这里只读取派生结果，不能用调用方的目标值覆盖真实状态。
        derived_turn_state = self.interaction_state.turn_state
        payload = {
            **self.runtime_identity_payload(),
            "session_id": self.current_audio_session_id,
            "turn_state": derived_turn_state,
        }
        if turn_id is not None:
            current_turn_identity = self.current_turn_identity_payload()
            if current_turn_identity["turn_id"] != turn_id:
                raise ValueError("turn.state_changed 不能绑定非当前 Turn")
            payload.update(current_turn_identity)
        if reason is not None:
            payload["reason"] = reason
        if generation_id is not None:
            payload["generation_id"] = generation_id

        return [
            {
                "type": "turn.state_changed",
                "payload": payload,
            },
            self.interaction_state_event(),
        ]

    def apply_user_turn_started(self, *, now: float) -> list[dict]:
        """应用 worker 已确认的用户 turn 开始事实。"""

        # 检测是否是新turn
        new_turn_started = (
            self.input_turn_session.current_turn_id is None
            or self.input_turn_session.turn_audio_state == "finalized"
        )
        self.input_turn_session.begin_turn()

        # 同一 turn不做操作
        if not new_turn_started:
            return []
        logger.info(f"{now}-新turn，开始")

        # 新 turn 开始后刷新产品活动时间，让交互窗口保持打开并进入监听态。
        self.interaction_state.on_user_speech_started(now=now)
        turn_id = self.input_turn_session.current_turn_id
        return [
            {
                "type": "environment.snapshot_requested",
                "payload": {
                    **self.current_turn_identity_payload(),
                    "request_id": f"material-snapshot-{uuid.uuid4().hex}",
                    "reason": "voice_turn_started",
                    "turn_id": turn_id,
                    "session_id": self.current_audio_session_id,
                    "created_at": datetime.now().astimezone().isoformat(),
                },
            },
            *self.apply_turn_state_changed("listening", now=now, turn_id=turn_id),
        ]

    def apply_partial_transcript(self, text: str) -> list[dict]:
        """应用 Worker 生成的实时转写片段。

        Args:
            text: ASR 返回的实时转写文本。

        Returns:
            list[dict]: 非空文本对应的公共事件；空文本返回空列表。
        """

        # 空 partial 只代表当前窗口没有可见文字，不能让 Desktop 创建空气泡或清掉助手字幕。
        normalized_text = text.strip()
        if not normalized_text:
            return []

        return [
            {
                "type": "user.partial_transcript",
                "payload": {
                    **self.current_turn_identity_payload(),
                    "session_id": self.current_audio_session_id,
                    "text": normalized_text,
                },
            }
        ]

    def apply_final_transcript(
        self,
        text: str,
        *,
        now: float,
        assistant_request_id: str | None,
        should_request_assistant: bool = True,
        history_disposition: str = "ordinary_turn",
        episode_id: str | None = None,
    ) -> list[dict]:
        """应用 worker 已生成的 final transcript。

        Args:
            text: worker 已生成的最终转写文本。
            now: final transcript 被应用的事实时间。
            assistant_request_id: 本轮将要进入助手输出侧的请求编号；不请求助手时为空。
            should_request_assistant: 该文本是否应该进入助手输出侧请求队列。
            history_disposition: 该转写立即进入稳定聊天还是只做 episode 临时显示。
            episode_id: 临时显示归属的 Self-Reflection episode 编号。

        Returns:
            list[dict]: `user.final_transcript` 和后续产品层 turn 状态事件。
        """

        if history_disposition not in {"ordinary_turn", "unattended_episode"}:
            raise ValueError("history_disposition 不是允许的历史归属")
        if history_disposition == "unattended_episode" and not (
            isinstance(episode_id, str) and episode_id.strip()
        ):
            raise ValueError("unattended_episode 必须携带 episode_id")
        if history_disposition == "ordinary_turn" and episode_id is not None:
            raise ValueError("ordinary_turn 不能携带 episode_id")

        # Final ASR 是输入侧的权威终点。先归档输入 turn，再由 InteractionState 在原子边界内
        # 关闭 InputState 并可选接管输出，避免 Notice 线程观察到中间的假 idle。
        self.input_turn_session.mark_smart_turn_result(is_turn_finished=True)
        normalized_text = text.strip()
        should_handoff_to_assistant = bool(normalized_text and should_request_assistant)
        handoff_request_id = (
            assistant_request_id
            if should_handoff_to_assistant and assistant_request_id
            else None
        )
        self.interaction_state.complete_final_asr_handoff(
            now,
            assistant_request_id=handoff_request_id,
        )
        final_transcript_event = {
            "type": "user.final_transcript",
            "payload": {
                **self.current_turn_identity_payload(),
                "session_id": self.current_audio_session_id,
                "text": normalized_text,
                "history_disposition": history_disposition,
            },
        }
        if episode_id is not None:
            final_transcript_event["payload"]["episode_id"] = episode_id
        if should_handoff_to_assistant:
            if not assistant_request_id:
                raise ValueError("有效 final transcript 必须绑定助手请求编号")
            final_transcript_event["payload"]["request_id"] = assistant_request_id
            # 产品状态进入 processing，表示后续助手 runner 可以接管，但这里不直接启动 LLM/TTS。
            return [final_transcript_event] + self.apply_turn_state_changed(
                "processing",
                now=now,
                turn_id=self.input_turn_session.current_turn_id,
            )

        # 空 final 或被过滤掉的短 final 没有输出侧接管，直接发布已经收口完成的 idle 状态。
        if not normalized_text:
            return self.apply_turn_state_changed(
                "idle",
                now=now,
                reason="empty_final_transcript",
                turn_id=self.input_turn_session.current_turn_id,
            )

        return [final_transcript_event] + self.apply_turn_state_changed(
            "idle",
            now=now,
            reason="ignored_final_transcript",
            turn_id=self.input_turn_session.current_turn_id,
        )

    def apply_input_only_final_transcript(self, text: str, *, now: float) -> list[dict]:
        """应用输入链路验证模式下 worker 已生成的 final transcript。

        Args:
            text: worker 已生成的最终转写文本。
            now: final transcript 被应用的事实时间。

        Returns:
            list[dict]: `user.final_transcript` 与 `turn.state_changed=idle` 事件。
        """

        # input-only 模式没有助手输出侧接管，因此 final ASR 完成后只归档输入 turn 并立即释放产品 turn 状态。
        self.input_turn_session.mark_smart_turn_result(is_turn_finished=True)
        self.interaction_state.on_user_input_finished(now=now)
        normalized_text = text.strip()
        final_events: list[dict] = []
        if normalized_text:
            final_events.append(
                {
                    "type": "user.final_transcript",
                    "payload": {
                        **self.current_turn_identity_payload(),
                        "session_id": self.current_audio_session_id,
                        "text": normalized_text,
                        "history_disposition": "ordinary_turn",
                    },
                }
            )
        return final_events + self.apply_input_only_final_transcript_completed(now=now)

    def close_uncommitted_final_transcript(
        self,
        *,
        now: float,
        reason: str,
    ) -> list[dict]:
        """结束没有成功取得历史归属的 Final ASR，且不公开正文。

        Args:
            now: 输入归属失败被确认的事实时间。
            reason: 供产品状态和诊断使用的稳定失败原因。

        Returns:
            只包含 idle 状态变化的事件，不包含 `user.final_transcript`。
        """

        normalized_reason = reason.strip()
        if not normalized_reason:
            raise ValueError("reason 不能为空")
        self.input_turn_session.mark_smart_turn_result(is_turn_finished=True)
        self.interaction_state.complete_final_asr_handoff(
            now,
            assistant_request_id=None,
        )
        return self.apply_turn_state_changed(
            "idle",
            now=now,
            reason=normalized_reason,
            turn_id=self.input_turn_session.current_turn_id,
        )

    def apply_text_input_started(
        self,
        *,
        now: float,
        request_id: str,
    ) -> list[dict]:
        """应用控制台文本输入已提交事实。

        Args:
            now: 文本输入真实入队的事实时间。
            request_id: 已经显示给用户的文本请求编号。
        Returns:
            list[dict]: `processing` 状态事件。
        """

        # 文本输入没有语音 turn_id，但同样代表助手后半段已经接管，应进入 processing。
        self.interaction_state.register_assistant_request(now, request_id=request_id)
        return self.apply_turn_state_changed("processing", now=now, reason="text_input")

    def register_assistant_request(self, request_id: str, *, now: float) -> list[dict]:
        """登记 Bridge 已接收的任意来源助手请求。

        Args:
            request_id: Bridge 已接收的请求编号。
            now: 请求进入输出侧的单调时间。

        Returns:
            list[dict]: 派生出的产品 Turn 状态事件。
        """

        self.interaction_state.register_assistant_request(now, request_id=request_id)
        return self.apply_turn_state_changed("processing", now=now, reason="assistant_request_queued")

    def apply_assistant_audio_publishing_started(
        self,
        *,
        now: float,
        generation_id: str | None,
        request_id: str,
    ) -> list[dict]:
        """应用助手音频已进入真实播放链路的事实。

        Args:
            now: 音频进入真实播放链路的事实时间。
            generation_id: 当前助手输出 generation 标识，缺失时仍允许状态推进。
            request_id: 当前助手输出所属请求编号。

        Returns:
            list[dict]: 首次进入 speaking 时返回状态事件；已在 speaking 时返回空列表。
        """

        # 真实播放开始才算助手下行活动；旧 request 或旧 generation 的事件只能被丢弃。
        output_state = self.interaction_state.assistant_output_state
        if output_state.request_id != request_id:
            logger.info(
                "忽略非当前请求的助手播放开始 request_id=%s active_request_id=%s",
                request_id,
                output_state.request_id,
            )
            return []
        already_speaking = output_state.playout_active
        accepted = self.interaction_state.on_assistant_audio_publishing_started(
            now=now,
            request_id=request_id,
            generation_id=generation_id,
        )
        if not accepted:
            return []
        if already_speaking:
            return []

        return self.apply_turn_state_changed(
            "speaking",
            now=now,
            reason="assistant_audio_publishing_started",
            generation_id=generation_id,
        )

    def apply_assistant_audio_publishing_cancelled(
        self,
        *,
        now: float,
        generation_id: str | None,
        request_id: str,
    ) -> list[dict]:
        """应用助手音频真实播放被取消的事实。

        Args:
            now: 播放取消的事实时间。
            generation_id: 被取消的 generation 标识，缺失时仍允许清理播放活跃态。
            request_id: 被取消播放所属请求编号。

        Returns:
            list[dict]: 取消后释放到 idle 的状态事件。
        """

        output_state = self.interaction_state.assistant_output_state
        if output_state.request_id != request_id:
            logger.info(
                "忽略非当前请求的助手播放取消 request_id=%s active_request_id=%s",
                request_id,
                output_state.request_id,
            )
            return []
        previous_turn_state = self.interaction_state.turn_state
        accepted = self.interaction_state.on_assistant_audio_publishing_cancelled(
            now=now,
            request_id=request_id,
            generation_id=generation_id,
        )
        current_turn_state = self.interaction_state.turn_state
        # 音频还在排队时也可能已完成生成；取消须公开 processing 的结束。
        # 用户正在说话时派生状态仍是 listening，不另发一条假的 idle。
        if not accepted or previous_turn_state == current_turn_state:
            logger.info(
                "忽略迟到的助手播放取消事件 generation_id=%s previous_turn_state=%s current_turn_state=%s",
                generation_id,
                previous_turn_state,
                current_turn_state,
            )
            return []

        return self.apply_turn_state_changed(
            current_turn_state,
            now=now,
            reason="assistant_audio_publishing_cancelled",
            generation_id=generation_id,
        )

    def apply_assistant_output_finished(
        self,
        *,
        reason: str,
        now: float,
        request_id: str,
        generation_id: str | None = None,
    ) -> list[dict]:
        """应用助手输出侧终态事实。

        Args:
            reason: 输出收尾原因，例如 assistant_audio_finished 或 assistant_failed。
            now: Router 观察到终态事件的事实时间。
            request_id: 产生终态的助手请求编号。

        Returns:
            list[dict]: `idle` 状态事件。
        """

        output_state = self.interaction_state.assistant_output_state
        if output_state.request_id != request_id:
            logger.info(
                "忽略非当前请求的助手输出终态 request_id=%s active_request_id=%s reason=%s",
                request_id,
                output_state.request_id,
                reason,
            )
            return []
        if not self.interaction_state.on_assistant_output_finished(
            now,
            request_id=request_id,
            generation_id=generation_id,
        ):
            return []
        return self.apply_turn_state_changed("idle", now=now, reason=reason)

    def apply_input_only_final_transcript_completed(self, *, now: float) -> list[dict]:
        """应用输入链路验证模式下 final ASR 已完成事实。

        Args:
            now: worker 应用 final transcript 的事实时间。

        Returns:
            list[dict]: `idle` 状态事件。
        """

        # input-only 模式没有助手输出侧接管，final transcript 广播后应立即释放产品 turn 状态。
        return self.apply_turn_state_changed("idle", now=now, reason="input_only_final_transcript")

    def apply_barge_in_detected(self, generation_id: str | None, *, now: float) -> list[dict]:
        """应用 worker 已确认的插话事实。

        Args:
            generation_id: 被插话的生成编号；未知时为空。
            now: Worker 确认插话时的单调时间。

        Returns:
            插话事件和刷新跟进窗口后的完整交互快照。
        """

        # 插话本身也是用户活动，刷新窗口可防止用户打断后会话立即被 idle timeout 关闭。
        self.interaction_state.on_barge_in(now=now)
        # generation_id 属于输出侧运行时上下文；输入侧拿不到具体目标时，事件只保留本次插话已经发生的产品事实。
        payload = {
            **self.runtime_identity_payload(),
            "session_id": self.current_audio_session_id,
        }
        if generation_id is not None:
            payload["generation_id"] = generation_id
        return [
            {
                "type": "barge_in.detected",
                "payload": payload,
            },
            self.interaction_state_event(),
        ]

    def apply_input_reset(self, reason: str, *, now: float) -> list[dict]:
        """应用输入侧重置事实。 livekit track发生重连等其他事件，重置asr这边的链路状态"""

        self.input_turn_session.reset_active_turn()
        self.interaction_state.on_user_input_finished(now=now)

        # VAD provider 可能持有流式上下文，输入侧重置后必须同步清空，避免旧轨道历史影响新 turn。
        reset_vad_provider = getattr(getattr(self, "vad_provider", None), "reset", None)
        if callable(reset_vad_provider):
            reset_vad_provider()

        return self.apply_turn_state_changed("idle", now=now, reason=reason)


def _mask_wakeword_keywords(keywords: tuple[str, ...]) -> list[str]:
    """脱敏唤醒词列表，供终端日志观测热替换事实。

    Args:
        keywords: 已归一化的唤醒词元组。

    Returns:
        保留首尾字符的脱敏唤醒词列表。
    """

    masked_keywords: list[str] = []
    for keyword in keywords:
        if len(keyword) <= 1:
            masked_keywords.append("*")
            continue
        if len(keyword) == 2:
            masked_keywords.append(f"{keyword[0]}*")
            continue
        masked_keywords.append(f"{keyword[0]}***{keyword[-1]}")
    return masked_keywords
