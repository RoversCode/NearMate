from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import replace
from threading import RLock

from companion.core.application.errors import StateConflict
from companion.core.broadcast import BroadcastHub
from companion.core.settings import Settings
from companion.observability.logging_config import get_backend_logger
from companion.realtime.assistant_process import AssistantProcessConfig
from companion.realtime.assistant_process_client import (
    ASSISTANT_PROCESS_STARTUP_TIMEOUT_SECONDS,
    AssistantProcessNotReadyError,
    AssistantProcessClient,
    MultiprocessingAssistantProcessClient,
)
from companion.realtime.assistant_process_handle import (
    AssistantProcessReplacement,
    SwappableAssistantProcessClient,
)
from companion.realtime.assistant_events import RuntimeEvent
from companion.realtime.runtime_identity import RuntimeIdentityFactory


logger = get_backend_logger(__name__)


class AssistantProcessSupervisor:
    """持有助手子进程客户端、稳定世代句柄和替换生命周期。

    Args:
        settings: 控制服务配置。
        hub: 控制面状态和角色体验配置中心。
        output_enabled: 是否允许启动助手输出侧资源。
        process_client: 外部注入的助手子进程客户端；为空时由应用按启动状态构造。
        shutdown_event_handler: 私下接收运行时退出阶段最后事件的端口。
        retired_event_handler: 把带旧世代身份的退出 backlog 交给主进程事件收口端口。
    """

    def __init__(
        self,
        *,
        settings: Settings,
        hub: BroadcastHub,
        output_enabled: bool,
        process_client: AssistantProcessClient | None,
        shutdown_event_handler: Callable[[str, dict[str, object]], None] | None = None,
        retired_event_handler: Callable[[RuntimeEvent], bool | None] | None = None,
        runtime_identity_factory: RuntimeIdentityFactory | None = None,
    ) -> None:
        self.settings = settings
        self.hub = hub
        self.output_enabled = output_enabled
        self.owns_process = process_client is None
        self.shutdown_event_handler = shutdown_event_handler
        self.retired_event_handler = retired_event_handler
        self.runtime_identity_factory = runtime_identity_factory or RuntimeIdentityFactory()
        self._target = process_client
        self.client = SwappableAssistantProcessClient(initial_runtime=process_client)
        self._target_epoch = self.client.current_epoch
        # 事务发布后会取走这份交接事实；即使上层包装器在交换后抛错，
        # 事务仍能识别旧进程和世代并完成只可前进的提交后清理。
        self._pending_published_replacement: AssistantProcessReplacement | None = None
        self._shutdown_lock = RLock()
        # 未发布候选也可能已启动进程；事务退出后仍由 Supervisor 持有失败的关闭对象。
        self._pending_shutdown_clients: dict[int, tuple[AssistantProcessClient, int | None]] = {}

        if not output_enabled:
            logger.info("只启动ASR及之前的链路")
            return

        if self.owns_process and self.hub.bootstrap.status_payload()["is_initialized"]:
            self._publish_target(self.build_owned_process_client())

    def build_owned_process_client(self) -> AssistantProcessClient:
        """按当前设置构造应用自有助手子进程客户端。

        Returns:
            尚未启动的多进程助手客户端。
        """

        # 子进程只接收可序列化配置，避免把完整设置对象或主进程资源放入进程参数。
        return MultiprocessingAssistantProcessClient(
            now=time.monotonic,
            runtime_identity_factory=self.runtime_identity_factory,
            config=AssistantProcessConfig.from_settings(
                self.settings,
                experience_config=self.hub.current_character_experience_runtime_config(),
            ),
        )

    def ensure_client_if_available(self) -> AssistantProcessClient | None:
        """在配置允许时确保稳定句柄已经绑定真实客户端。

        Returns:
            可供调用方长期持有的稳定客户端；尚未初始化或输出禁用时为空。
        """

        if not self.output_enabled:
            return None
        if not self.owns_process:
            return self.client
        if self._target is not None:
            return self.client
        if not self.hub.bootstrap.status_payload()["is_initialized"]:
            return None

        candidate = self.build_candidate()
        if candidate is None:
            return None
        self._publish_target(candidate)
        return self.client

    def require_for_output(self) -> AssistantProcessClient:
        """取得助手输出链路使用的稳定客户端。

        Returns:
            当前稳定助手子进程客户端。

        Raises:
            RuntimeError: 输出已禁用或首次初始化尚未完成。
        """

        if not self.output_enabled:
            raise RuntimeError("Assistant output 已被 realtime input-only 模式禁用")
        client = self.ensure_client_if_available()
        if client is None:
            raise RuntimeError("Assistant process 尚未初始化，必须先完成 bootstrap")
        if not self.is_ready:
            raise AssistantProcessNotReadyError(
                "assistant runtime is not ready for output"
            )
        return client

    @property
    def is_ready(self) -> bool:
        """判断当前助手世代是否已经完成装配并可以接收输出命令。"""

        target = self._target
        if target is None:
            return False
        target_ready = getattr(target, "is_ready", None)
        # 外部注入的测试/嵌入式运行时没有独立 readiness 协议，沿用其既有可用语义。
        return True if target_ready is None else bool(target_ready)

    def require_for_streamvox_management(self) -> AssistantProcessClient:
        """取得 StreamVox 管理动作使用的稳定客户端。

        Returns:
            当前稳定助手子进程客户端。

        Raises:
            StateConflict: 输出已禁用或助手子进程尚未初始化。
        """

        if not self.output_enabled:
            raise StateConflict("assistant output is disabled in realtime input-only mode")
        client = self.ensure_client_if_available()
        if client is None:
            raise StateConflict("assistant process is not initialized")
        return client

    def build_candidate(self) -> AssistantProcessClient | None:
        """按最新设置构造自有助手子进程候选客户端。

        Returns:
            尚未启动的候选客户端；输出禁用或外部进程不允许替换时为空。

        Raises:
            RuntimeError: 上一次关闭失败的资源仍未释放，不能继续启动新进程。
        """

        if not self.output_enabled or not self.owns_process:
            return None
        if not self.retry_pending_shutdown():
            raise RuntimeError("旧助手进程尚未完成关闭")
        # 候选构造不代表发布；首次 Bootstrap 需要先验证并准备全部资源，
        # 再持久化初始化事实，最后由替换事务启动和发布候选。
        return self.build_owned_process_client()

    def start_if_initialized(self) -> None:
        """在首次初始化完成后启动应用自有助手子进程。"""

        if not self.output_enabled or not self.owns_process:
            return
        if not self.hub.bootstrap.status_payload()["is_initialized"]:
            return
        client = self.ensure_client_if_available()
        if client is None:
            return
        start_client = getattr(client, "start", None)
        if callable(start_client):
            start_client()

    def start_candidate(self, candidate: AssistantProcessClient) -> None:
        """在发布前启动候选助手子进程客户端。

        Args:
            candidate: 已按最新设置构造的候选客户端。
        """

        try:
            start_client = getattr(candidate, "start", None)
            if callable(start_client):
                start_client()
        except Exception:
            self.shutdown_process(candidate)
            raise

    def wait_until_ready(
        self,
        client: AssistantProcessClient | None = None,
        *,
        timeout: float = ASSISTANT_PROCESS_STARTUP_TIMEOUT_SECONDS,
    ) -> None:
        """等待指定助手候选完成装配，再允许它接收管理请求。

        Args:
            client: 要等待的真实助手客户端；为空时等待当前已发布客户端。
            timeout: 最长等待秒数。

        Returns:
            无；没有实现就绪协议的测试替身视为无需等待。

        Raises:
            RuntimeError: 助手进程退出或在期限内没有完成装配。
        """

        target = client if client is not None else self._target
        if target is None:
            return
        wait_ready = getattr(target, "wait_until_ready", None)
        if callable(wait_ready):
            wait_ready(timeout=timeout)

    def publish_candidate(self, candidate: AssistantProcessClient) -> AssistantProcessReplacement:
        """把已启动的候选客户端原子发布到稳定句柄。

        Args:
            candidate: 已经成功启动的候选客户端。

        Returns:
            包含新旧世代和旧客户端的原子发布结果。
        """

        return self._publish_target(candidate)

    def _publish_target(self, client: AssistantProcessClient) -> AssistantProcessReplacement:
        """把真实助手客户端发布到稳定代理句柄。

        Args:
            client: 新的真实助手子进程客户端。

        Returns:
            稳定句柄完成原子发布后的世代结果。
        """

        replacement = self.client.replace(client)
        self._target = client
        self._target_epoch = replacement.current_epoch
        self._pending_published_replacement = replacement
        return replacement

    def is_current_target(self, client: AssistantProcessClient | None) -> bool:
        """判断指定客户端是否就是当前绑定在稳定句柄上的现役助手进程。

        Args:
            client: 待确认的助手客户端。

        Returns:
            该客户端已经完成发布且仍现役时为 True；为 None 或已退役时为 False。
        """

        return (
            client is not None
            and client is self._target
            and self.client.current_epoch == self._target_epoch
        )

    def take_published_replacement(
        self,
        client: AssistantProcessClient,
    ) -> AssistantProcessReplacement | None:
        """取走当前候选最近一次发布产生的交接事实。

        Args:
            client: 配置事务正在发布的助手候选。

        Returns:
            候选仍是现役目标时返回尚未领取的交接事实，否则返回空。
        """

        if not self.is_current_target(client):
            return None
        replacement = self._pending_published_replacement
        if replacement is None or replacement.current_epoch != self._target_epoch:
            return None
        self._pending_published_replacement = None
        return replacement

    def shutdown_process(
        self,
        client: AssistantProcessClient | None,
        *,
        runtime_epoch: int | None = None,
    ) -> bool:
        """关闭指定助手子进程客户端，并报告是否已经完成。

        Args:
            client: 等待关闭的真实客户端；为空时无需处理。
            runtime_epoch: 调用方已知的旧助手世代；原始客户端本身没有世代信息时必须传入。

        Returns:
            关闭成功时返回真；失败时保留给资源拥有者重试。
        """

        if client is None:
            return True
        with self._shutdown_lock:
            previous = self._pending_shutdown_clients.get(id(client))
            if runtime_epoch is None and previous is not None:
                runtime_epoch = previous[1]
            shutdown_client = getattr(client, "shutdown", None)
            if not callable(shutdown_client):
                self._pending_shutdown_clients.pop(id(client), None)
                return True
            try:
                shutdown_events = shutdown_client()
            except Exception:
                self._pending_shutdown_clients[id(client)] = (client, runtime_epoch)
                logger.warning("助手进程关闭失败，保留资源等待重试")
                return False
            self._pending_shutdown_clients.pop(id(client), None)
        if not isinstance(shutdown_events, (list, tuple)):
            return True
        for event in shutdown_events:
            if not isinstance(event, RuntimeEvent):
                continue
            event_epoch = runtime_epoch or event.runtime_epoch
            retired_event_dispatched = False
            if event_epoch is not None and self.retired_event_handler is not None:
                retired_event = replace(
                    event,
                    runtime_epoch=event_epoch,
                    retired_runtime_event=True,
                )
                try:
                    # 退出 backlog 只交给主进程唯一事件队列；Router 会在旧世代门禁之外
                    # 进入幂等清理分支，避免用量和材料被两条旁路各记一次。
                    result = self.retired_event_handler(retired_event)
                    retired_event_dispatched = result is not False
                except Exception:
                    logger.exception(
                        "Assistant retired event handler failed event_type=%s runtime_epoch=%s",
                        event.type,
                        event_epoch,
                    )
            if retired_event_dispatched or self.shutdown_event_handler is None:
                continue
            try:
                # 没有可用旧世代事件队列时仍保留去内容化用量旁路；候选进程尚未取得
                # Activity 身份，不能伪造一条退役生命周期事件。
                self.shutdown_event_handler(event.type, event.payload)
            except Exception:
                logger.exception(
                    "Assistant shutdown event handler failed event_type=%s",
                    event.type,
                )

        return True

    def retry_pending_shutdown(self) -> bool:
        """重试仍由 Supervisor 持有的关闭对象，成功后才释放引用。

        Returns:
            所有对象都已关闭时为真；失败对象保留到下一次构造或退出重试。
        """
        with self._shutdown_lock:
            # 逐项尝试，单个故障不能让其他旧进程永远等不到关闭机会。
            for client, runtime_epoch in tuple(self._pending_shutdown_clients.values()):
                self.shutdown_process(client, runtime_epoch=runtime_epoch)
            return not self._pending_shutdown_clients

    def detach_for_shutdown(self) -> AssistantProcessReplacement | None:
        """立即切断旧世代派发，关闭失败重试时不重复提升世代。"""
        if self._target is None or self.client.current_epoch != self._target_epoch:
            return None
        return self.client.replace(None)

    def shutdown(self) -> None:
        """关闭当前进程及失败候选，释放失败时保留 Owner 供下一次重试。

        Returns:
            无；所有已登记资源都关闭后才完成退出。

        Raises:
            RuntimeError: 某个助手进程仍未完成关闭。
        """
        with self._shutdown_lock:
            target = self._target
            if target is not None:
                self.detach_for_shutdown()
                self._pending_shutdown_clients[id(target)] = (target, self._target_epoch)
            all_closed = self.retry_pending_shutdown()
            if target is None or id(target) not in self._pending_shutdown_clients:
                self._target = None
                self._pending_published_replacement = None
            if not all_closed:
                raise RuntimeError("助手进程尚未完成关闭")
