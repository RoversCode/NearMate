from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import os
from threading import RLock

from companion.core.application.errors import StateConflict
from companion.core.broadcast import BroadcastHub
from companion.core.settings import Settings
from companion.core.streamvox_runtime_config import development_voice_fingerprint
from companion.observability.logging_config import get_backend_logger
from companion.realtime.interaction_state import InteractionState
from companion.realtime.realtime_input_runtime import RealtimeInputRuntime
from companion.realtime.runtime_identity import RuntimeIdentityFactory


logger = get_backend_logger(__name__)


@dataclass(frozen=True, slots=True)
class RealtimeInputRuntimeReplacement:
    """描述实时输入运行时发布后的新旧资源交接。

    Args:
        old_runtime: 发布前由 Owner 持有的运行时；首次初始化时为空。
        current_runtime: 已发布并成为当前状态源的新运行时。
        old_interaction_state: 发布前控制面使用的交互状态源。
    """

    old_runtime: RealtimeInputRuntime | None
    current_runtime: RealtimeInputRuntime
    old_interaction_state: InteractionState


class RealtimeInputRuntimeOwner:
    """持有并热替换主进程的实时输入运行时。

    Args:
        settings: 控制服务配置。
        hub: 控制面广播和持久化中心。
        runtime: 外部注入的实时输入运行时；为空时由应用按启动状态构造。
        voice_status_provider: 读取完整内部语音状态的函数；不会把该状态直接广播给控制端。
    """

    def __init__(
        self,
        *,
        settings: Settings,
        hub: BroadcastHub,
        runtime: RealtimeInputRuntime | None,
        runtime_identity_factory: RuntimeIdentityFactory | None = None,
        voice_status_provider: Callable[[], dict[str, object]] | None = None,
    ) -> None:
        self.settings = settings
        self.hub = hub
        # 唤醒缓存资格需要完整的内部语音状态；Bootstrap 的公开投影会主动移除 fingerprint。
        self._voice_status_provider = voice_status_provider
        self._runtime_identity_factory = runtime_identity_factory or RuntimeIdentityFactory()
        self._identity_lock = RLock()
        self._shutdown_lock = RLock()
        self._pending_shutdown_runtimes: dict[int, RealtimeInputRuntime] = {}
        self._next_input_runtime_epoch = 1
        self.owns_runtime = runtime is None
        self.runtime = runtime or self._build_initial_runtime()
        if self.runtime is not None:
            self._assign_runtime_identity(self.runtime)
        self._publish_interaction_state()

    def _build_initial_runtime(self) -> RealtimeInputRuntime | None:
        """按启动初始化状态构造首个自有实时输入运行时。

        Returns:
            已绑定唤醒回复依赖的运行时；尚未初始化时为空。
        """

        # 首次启动尚无外部密钥时必须先开放控制台，不能创建半初始化的语音运行时。
        if not self.hub.bootstrap.status_payload()["is_initialized"]:
            return None
        try:
            candidate = self.attach_wake_response_runtime(
                RealtimeInputRuntime.from_settings(
                    self.settings,
                    wakeword_keywords=self.active_wakeword_keywords(),
                )
            )
            return candidate
        except Exception as error:
            # 控制面必须先启动，语音输入资源失败时让 Console 展示配置错误并支持重试。
            logger.exception("实时输入运行时初始化失败，保留控制面以便修复：%s", error)
            return None

    def _assign_runtime_identity(self, runtime: RealtimeInputRuntime) -> None:
        """为新建候选分配一次性 UUID 与单调 epoch。

        Args:
            runtime: 尚未绑定进程级身份的输入 Runtime。

        Returns:
            None: 身份直接绑定到候选对象；回滚时 epoch 不回收。
        """

        if runtime.input_runtime_id and runtime.input_runtime_epoch > 0:
            return
        with self._identity_lock:
            input_runtime_epoch = self._next_input_runtime_epoch
            self._next_input_runtime_epoch += 1
        runtime.bind_runtime_identity(
            input_runtime_id=self._runtime_identity_factory.new("input-runtime"),
            input_runtime_epoch=input_runtime_epoch,
        )

    def current_runtime_identity_event(self) -> dict[str, object] | None:
        """构造当前已发布输入 Runtime 的公共身份快照。

        Returns:
            当前存在 Runtime 时返回 ``input.runtime_changed``；未初始化时返回空。
        """

        if self.runtime is None:
            return None
        return {
            "type": "input.runtime_changed",
            "payload": self.runtime.runtime_identity_payload(),
        }

    def current_runtime_identity(self) -> tuple[str, int] | None:
        """读取当前输入 Runtime 的比较身份。

        Returns:
            Runtime 存在时返回 ``(id, epoch)``，尚未初始化时返回空。
        """

        if self.runtime is None:
            return None
        return self.runtime.input_runtime_id, self.runtime.input_runtime_epoch

    def current_interaction_state_event(self) -> dict[str, object] | None:
        """读取当前输入 Runtime 的完整交互状态，供新连接直接恢复展示。

        Args:
            无。

        Returns:
            带版本的交互快照事件；Runtime 尚未初始化时返回空。
        """

        if self.runtime is None:
            return None
        return self.runtime.interaction_state_event()

    async def broadcast_current_runtime_identity(self) -> None:
        """在替换事务正式提交后广播当前输入 Runtime 身份及交互状态。

        Args:
            无。

        Returns:
            None: 未初始化 Runtime 时不产生事件。
        """

        event = self.current_runtime_identity_event()
        if event is None:
            return
        await self.hub.publisher("interaction").publish(event)
        state_event = self.current_interaction_state_event()
        if state_event is not None:
            await self.hub.publisher("interaction").publish(state_event)

    def attach_wake_response_runtime(self, runtime: RealtimeInputRuntime) -> RealtimeInputRuntime:
        """给实时输入运行时绑定当前角色的唤醒回复依赖。

        Args:
            runtime: 等待绑定唤醒回复依赖的运行时。

        Returns:
            已完成依赖绑定的同一运行时。
        """

        wake_response_service = self.hub.wake_response_service
        # 输入 Runtime 可能在角色切换前后持续存活；目录必须每次读取当前角色，
        # 不能依赖启动时捕获的固定路径，否则旧角色的音频会被继续播放。
        if hasattr(wake_response_service, "asset_dir_provider"):
            wake_response_service.asset_dir_provider = (
                lambda: self.hub.character_card_assets.wake_response_audio_dir(
                    self.hub.character_cards.load_active_card_id()
                )
            )
        if hasattr(wake_response_service, "voice_fingerprint_provider"):
            # 指纹必须从当前 Bootstrap 语音状态实时读取；不能把构造 Runtime 时的旧角色指纹
            # 固定下来，否则角色切换或重新确认音色后旧缓存会永久失效。
            wake_response_service.voice_fingerprint_provider = self._active_voice_fingerprint
        if hasattr(wake_response_service, "bundled_audio_validator"):
            wake_response_service.bundled_audio_validator = (
                lambda item: self.hub.character_card_assets.bundled_wake_responses.is_audio_current(
                    self.hub.character_cards.load_active_card_id(), item
                )
            )
        runtime.wake_response_service = wake_response_service
        runtime.wake_response_config_provider = lambda: self.hub.character_card_assets.load_wake_response(
            self.hub.character_cards.load_active_card_id()
        )
        return runtime

    def _active_voice_fingerprint(self) -> str | None:
        """读取当前角色已确认的语音指纹。

        Returns:
            当前已确认音色的 SHA-256 指纹；语音尚未就绪或状态不可用时返回空值。
        """

        # 生产组合根注入 TtsSetupService.status_payload；它是唯一能提供已确认
        # fingerprint 的内部来源。不能从公开 Bootstrap 快照反推，因为公开投影
        # 只保留界面门禁字段，故意不包含缓存指纹和文件路径。
        internal_status_provider = getattr(self, "_voice_status_provider", None)
        if callable(internal_status_provider):
            try:
                internal_status = internal_status_provider()
            except Exception:
                # 生产语音状态读取失败时必须闭合缓存资格，不能拿可能过期的
                # 公开快照继续播放旧音色；下一次状态刷新会重新评估资格。
                return None
            if not isinstance(internal_status, dict):
                return None
            fingerprint = internal_status.get("fingerprint")
            if isinstance(fingerprint, str) and fingerprint:
                return fingerprint
            # 内部状态未就绪时，安装版必须闭合缓存资格；源码开发态仍沿用
            # 原有稳定开发指纹，方便在尚未落盘 voice-setup.json 时运行调试。
            if os.environ.get("NEARMATE_PACKAGED") == "1":
                return None
        else:
            # 保留旧版装配和轻量测试替身的兼容路径：历史 Bootstrap 状态曾经直接
            # 暴露 fingerprint。新公开投影没有该字段时，这段读取自然会落到开发态
            # 指纹或 packaged 安全失败分支，不会把内部状态泄露到控制端。
            bootstrap = getattr(self.hub, "bootstrap", None)
            public_status_provider = getattr(bootstrap, "status_payload", None)
            if not callable(public_status_provider):
                return None
            try:
                public_status = public_status_provider()
                voice_status = public_status.get("voice")
            except Exception:
                return None
            if isinstance(voice_status, dict):
                fingerprint = voice_status.get("fingerprint")
                if isinstance(fingerprint, str) and fingerprint:
                    return fingerprint
            if os.environ.get("NEARMATE_PACKAGED") == "1":
                return None

        # 源码开发态没有 voice-setup.json 时，仍需让新生成的唤醒音频可播放；
        # 使用与 Console 合成端相同的稳定指纹，角色或参考音频变化会自动失效旧缓存。
        try:
            card = self.hub.character_cards.load_active_card()
            reference_audio = self.hub.character_card_assets.resolve_voice_reference_audio(card)
            return development_voice_fingerprint(card=card, reference_audio=reference_audio)
        except Exception:
            return None

    def active_wakeword_keywords(self) -> tuple[str, ...]:
        """读取当前角色卡的唤醒词。

        Returns:
            当前角色卡配置的唤醒词集合。
        """

        return self.hub.current_character_experience_runtime_config().wakeword_keywords

    def _publish_interaction_state(self) -> None:
        """把真实运行时的交互状态发布为控制面唯一状态源。"""

        runtime_interaction_state = getattr(self.runtime, "interaction_state", None)
        if runtime_interaction_state is not None:
            self.hub.interaction_state = runtime_interaction_state

    def current_interaction_state(self) -> InteractionState:
        """返回当前唯一交互状态源。

        Returns:
            输入运行时持有的状态；运行时缺失时返回控制面占位状态。
        """

        runtime_interaction_state = getattr(self.runtime, "interaction_state", None)
        if runtime_interaction_state is not None:
            return runtime_interaction_state
        return self.hub.interaction_state

    def require_for_livekit(self) -> RealtimeInputRuntime:
        """取得 LiveKit 媒体会话所需的实时输入运行时。

        Returns:
            当前实时输入运行时。

        Raises:
            RuntimeError: 首次初始化尚未完成。
        """

        if self.runtime is None:
            raise RuntimeError("RealtimeInputRuntime 尚未初始化，必须先完成 bootstrap")
        return self.runtime

    def require_for_turn_state(self) -> RealtimeInputRuntime:
        """取得轮次状态闭环所需的实时输入运行时。

        Returns:
            当前实时输入运行时。

        Raises:
            StateConflict: 首次初始化尚未完成。
        """

        if self.runtime is None:
            raise StateConflict("realtime input runtime is not initialized")
        return self.runtime

    def build_candidate(self) -> RealtimeInputRuntime | None:
        """按最新设置构造自有实时输入运行时候选实例。

        Returns:
            可供发布的候选运行时；外部运行时不允许被替换时为空。

        Raises:
            RuntimeError: 上一次关闭失败的输入资源仍未释放。
        """

        if not self.owns_runtime:
            return None
        if not self.retry_pending_shutdown():
            raise RuntimeError("旧实时输入运行时尚未完成关闭")
        candidate = self.attach_wake_response_runtime(
            RealtimeInputRuntime.from_settings(
                self.settings,
                wakeword_keywords=self.active_wakeword_keywords(),
            )
        )
        # 候选一旦构造成功就占用自己的 epoch。即使随后回滚，也不能把这个序号分给别的 Runtime。
        self._assign_runtime_identity(candidate)
        return candidate

    def publish_candidate(
        self,
        candidate: RealtimeInputRuntime | None,
    ) -> RealtimeInputRuntimeReplacement | None:
        """发布初始化成功的实时输入运行时候选实例。

        Args:
            candidate: 已完成构造的候选运行时；为空时无需替换。

        Returns:
            新旧运行时交接结果；候选为空时返回空。
        """

        if candidate is None:
            return None
        if not candidate.input_runtime_id or candidate.input_runtime_epoch <= 0:
            raise RuntimeError("输入 Runtime 候选发布前必须完成身份分配")
        old_runtime = self.runtime
        old_interaction_state = self.hub.interaction_state
        self.runtime = candidate
        self._publish_interaction_state()
        return RealtimeInputRuntimeReplacement(
            old_runtime=old_runtime,
            current_runtime=candidate,
            old_interaction_state=old_interaction_state,
        )

    def restore_replacement(
        self,
        replacement: RealtimeInputRuntimeReplacement,
    ) -> None:
        """把尚未提交的输入运行时交接恢复到发布前状态。

        Args:
            replacement: `publish_candidate` 返回的新旧资源交接结果。
        """

        if self.runtime is not replacement.current_runtime:
            raise RuntimeError("当前输入 Runtime 已变化，不能恢复过期交接")
        self.runtime = replacement.old_runtime
        self.hub.interaction_state = replacement.old_interaction_state

    def rebuild_speaker_verification_provider(self, settings: Settings | None = None) -> None:
        """在实时输入运行时存在时热替换说话人确认提供器。

        Args:
            settings: 构造新提供器使用的候选设置；为空时使用当前设置。
        """

        if self.runtime is None:
            return
        rebuild_provider = getattr(self.runtime, "rebuild_speaker_verification_provider", None)
        if callable(rebuild_provider):
            rebuild_provider(settings or self.hub.settings)

    def shutdown(self) -> None:
        """关闭当前输入资源和失败候选，失败实例保留给授权停机重试。

        Returns:
            无；所有已登记资源都关闭后才释放当前交互状态。

        Raises:
            RuntimeError: 某个输入运行时仍未完成关闭。
        """
        with self._shutdown_lock:
            runtime = self.runtime
            if runtime is not None:
                self._pending_shutdown_runtimes[id(runtime)] = runtime
            all_closed = self.retry_pending_shutdown()
            if runtime is None or id(runtime) not in self._pending_shutdown_runtimes:
                self.runtime = None
                self.hub.interaction_state = InteractionState()
            if not all_closed:
                raise RuntimeError("实时输入运行时尚未完成关闭")

    def shutdown_runtime(self, runtime: RealtimeInputRuntime | None) -> None:
        """关闭指定输入资源，失败时交回 Owner 持有而不是遗失模型引用。

        Args:
            runtime: 等待关闭的当前或候选运行时；为空时无需处理。

        Returns:
            无；关闭失败会登记到 Owner，后续构造或退出时继续重试。
        """

        if runtime is None:
            return
        with self._shutdown_lock:
            shutdown_runtime = getattr(runtime, "shutdown", None)
            if not callable(shutdown_runtime):
                self._pending_shutdown_runtimes.pop(id(runtime), None)
                return
            try:
                shutdown_runtime()
            except Exception:
                self._pending_shutdown_runtimes[id(runtime)] = runtime
                logger.warning("实时输入运行时关闭失败，保留资源等待重试")
                return
            self._pending_shutdown_runtimes.pop(id(runtime), None)

    def retry_pending_shutdown(self) -> bool:
        """逐项重试关闭失败的输入模型，成功后才交回资源引用。

        Returns:
            所有对象都已关闭时为真；失败对象继续由 Owner 持有。
        """
        with self._shutdown_lock:
            for runtime in tuple(self._pending_shutdown_runtimes.values()):
                self.shutdown_runtime(runtime)
            return not self._pending_shutdown_runtimes
