from __future__ import annotations

import multiprocessing as mp
import time
from collections.abc import Callable
from queue import Empty
from threading import RLock
from typing import Any, Protocol

from companion.background_agent.contracts import AgentMaterial
from companion.realtime.assistant_events import (
    AssistantControlCommand,
    BackgroundAgentInteractionAnswerCommand,
    BackgroundAgentInteractionSubmissionRejectedCommand,
    CancelCurrentGenerationCommand,
    AssistantProcessCommand,
    AssistantRequest,
    RuntimeEvent,
    WarmupForegroundCommand,
    StreamVoxManagementRequest,
    StreamVoxManagementResponse,
)
from companion.realtime.runtime_identity import RuntimeIdentityFactory
from companion.realtime.assistant_process import (
    ASSISTANT_PROCESS_NATURAL_SHUTDOWN_TIMEOUT_SECONDS,
    AssistantProcessConfig,
    run_assistant_process,
    run_noop_assistant_process,
)


SHUTDOWN_EVENT_DRAIN_GRACE_SECONDS = 0.05
ASSISTANT_PROCESS_STARTUP_TIMEOUT_SECONDS = 60.0


class AssistantProcessNotReadyError(RuntimeError):
    """助手子进程已经启动，但还没有完成运行时装配。"""


class AssistantProcessClient(Protocol):
    """主进程使用的助手运行时客户端协议。

    隐藏同进程运行时与独立进程运行时的通信差异，让主进程只依赖统一调用边界。
    """

    def cancel_current_generation(
        self, *, reason: str, generation_id: str | None = None
    ) -> AssistantControlCommand:
        """取消当前助手 generation。

        Args:
            reason: 取消原因或触发来源。
            generation_id: 可选的目标 generation 标识。

        Returns:
            已提交给运行时边界的控制命令。
        """
        ...

    def submit_control_command(self, command: AssistantControlCommand) -> AssistantControlCommand:
        """提交既有助手控制命令。

        Args:
            command: 上游已经创建好的助手控制命令。

        Returns:
            已提交给运行时边界的同一条控制命令。
        """
        ...

    def warmup_foreground(self, *, reason: str) -> AssistantControlCommand:
        """提交前台模型 warmup 控制命令。

        Args:
            reason: 触发预热的业务原因。

        Returns:
            已提交给运行时边界的控制命令。
        """
        ...

    def answer_background_agent_interaction(
        self,
        *,
        request_id: str,
        outcome: str,
        answers: dict[str, str] | None = None,
        reason: str = "",
        materials: tuple[AgentMaterial, ...] = (),
        user_text: str = "",
        submission_id: str | None = None,
        runtime_epoch: int | None = None,
    ) -> AssistantControlCommand:
        """把权限或资料回答提交给助手子进程。

        Args:
            request_id: 后台交互请求编号。
            outcome: 允许、拒绝、回答或取消。
            answers: AskUserQuestion 的答案映射。
            reason: 用户填写的拒绝或取消理由。
            materials: 本轮新增、需要扩展到当前工作的正式材料。
            user_text: Foreground 收到的本轮原始用户话语。
            submission_id: 自然语言回答等待权威回执时使用的提交编号。
            runtime_epoch: 稳定代理用于防止热替换竞态的目标世代；真实客户端忽略。

        Returns:
            已进入控制队列的命令。
        """
        ...

    def reject_background_agent_interaction_submission(
        self,
        *,
        submission_id: str,
        request_id: str,
        reason: str,
        runtime_epoch: int | None = None,
    ) -> AssistantControlCommand:
        """把主进程 Registry 的拒绝事实送回等待中的子进程 Broker。

        Args:
            submission_id: 自然语言回答的提交编号。
            request_id: 对应的工作交互请求编号。
            reason: `already_resolved` 或 `background_unavailable`。
            runtime_epoch: 稳定代理用于校验的目标运行时世代。

        Returns:
            已进入控制队列的拒绝命令。
        """
        ...

    def submit_existing_request(self, request: AssistantRequest) -> AssistantRequest:
        """提交既有助手生成请求。

        Args:
            request: 上游已经创建好的助手生成请求。

        Returns:
            已提交给运行时边界的同一条请求。
        """
        ...

    def drain_events(self, *, limit: int = 100) -> list[RuntimeEvent]:
        """读取助手运行时已完成事件。

        Args:
            limit: 本次最多返回的事件数量。

        Returns:
            按生产顺序排列的运行时事件列表。
        """
        ...

    def notify_bridge_work_available(self) -> None:
        """通知主进程 bridge 已有可处理工作。"""
        ...

    def clear_bridge_work_available(self) -> None:
        """清除主进程 bridge 的工作可用信号。"""
        ...

    def wait_for_bridge_work(self, timeout: float | None = None) -> bool:
        """等待主进程 bridge 的工作可用信号。

        Args:
            timeout: 最长等待秒数，None 表示一直等待。

        Returns:
            等待期间观察到工作信号时返回 True，超时时返回 False。
        """
        ...

    def get_streamvox_runtime_state(self, *, timeout: float = 5.0) -> dict[str, Any]:
        """读取助手子进程内 StreamVox 当前运行状态。

        Args:
            timeout: 同步等待运行时响应的最长秒数。

        Returns:
            可 JSON 序列化的 StreamVox 状态字典。
        """
        ...

    def switch_streamvox_config(
        self,
        *,
        model: str,
        device: str,
        llama_backend: str | None,
        role_name: str,
        streamvox_seed: int | None,
        inference_options: dict[str, Any],
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        """切换助手子进程内 StreamVox 模型、设备和当前角色。

        Args:
            model: 目标模型名称。
            device: 目标推理设备。
            llama_backend: 目标 llama.cpp 后端策略；None 表示不显式指定。
            role_name: 目标角色名称。
            streamvox_seed: 目标推理随机种子。
            inference_options: 目标模型族推理参数。
            timeout: 同步等待运行时响应的最长秒数。

        Returns:
            更新后的 StreamVox 状态字典。
        """
        ...

    def delete_streamvox_role(self, *, role_name: str, timeout: float = 5.0) -> dict[str, Any]:
        """删除当前模型下的指定 StreamVox 角色。

        Args:
            role_name: 待删除的角色名称。
            timeout: 同步等待运行时响应的最长秒数。

        Returns:
            更新后的 StreamVox 状态字典。
        """
        ...

    def create_streamvox_role(
        self,
        *,
        role_name: str,
        audio_path: str,
        prompt_text: str,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        """创建当前模型下的 StreamVox 自定义角色。

        Args:
            role_name: 新角色名称。
            audio_path: 用于克隆音色的音频文件路径。
            prompt_text: 音频对应的提示文本。
            timeout: 同步等待运行时响应的最长秒数。

        Returns:
            更新后的 StreamVox 状态字典。
        """
        ...

    def preview_streamvox_role(self, *, role_name: str, text: str, timeout: float = 30.0) -> dict[str, Any]:
        """使用当前模型下的已创建角色生成试听音频。

        Args:
            role_name: 试听使用的角色名称。
            text: 试听文本。
            timeout: 同步等待运行时响应的最长秒数。

        Returns:
            包含试听音频和采样率的载荷。
        """
        ...

    def get_streamvox_prompt_data(self, *, role_name: str, timeout: float = 5.0) -> Any | None:
        """读取当前模型下指定角色的 StreamVox prompt 数据。

        Args:
            role_name: 待读取的角色名。
            timeout: 同步等待运行时响应的最长秒数。

        Returns:
            StreamVox SDK 的 PromptData；不存在时返回 None。
        """
        ...

    def shutdown(self) -> tuple[RuntimeEvent, ...]:
        """关闭助手运行时资源。

        Returns:
            运行时退出阶段最后产生、尚未被主进程 Bridge 取走的事件。
        """
        ...


class MultiprocessingAssistantProcessClient:
    """独立进程助手运行时客户端。

    在主进程中持有助手运行时 IPC 队列，并托管独立助手子进程的生命周期。
    """

    def __init__(
        self,
        *,
        now: Callable[[], float],
        process_target: Callable[[Any, Any, Any, Any, Any, Any, Any], None] = run_assistant_process,
        config: AssistantProcessConfig | None = None,
        runtime_identity_factory: RuntimeIdentityFactory | None = None,
    ) -> None:
        """初始化跨进程队列和生命周期状态。

        Args:
            now: 外部注入的时间函数。
            process_target: 可被 multiprocessing spawn 的子进程入口函数。
            config: 传给子进程入口的可序列化配置。
            runtime_identity_factory: Backend 主进程共享的运行期身份工厂。

        Returns:
            无。
        """

        self.now = now
        # process_target 保持为顶层可 pickle 函数，满足 Windows spawn 模式对子进程入口的要求。
        self._process_target = process_target
        # 配置
        self._config = config
        self._runtime_identity_factory = (
            runtime_identity_factory or RuntimeIdentityFactory()
        )
        # multiprocessing context 统一创建 Event、Queue 和 Process，避免混用上下文导致平台差异。
        self._context = mp.get_context()
        # _process 保存当前子进程句柄；None 表示从未启动或已经完成收口。
        self._process: mp.Process | None = None
        # 管理 RPC 共用一个响应队列，必须串行收发，避免并发调用互相取走对方响应。
        self._streamvox_management_lock = RLock()
        # IPC 对象可在 shutdown 后重建，避免复用已关闭队列造成后续 start/submit 失败。
        self._queues_closed = True
        self._create_ipc_objects()

    def _create_ipc_objects(self) -> None:
        """创建一组新的跨进程通信对象。

        集中创建 stop_event、请求队列、控制队列和事件队列，供构造和 shutdown 后重启复用。

        Returns:
            无。
        """

        # stop_event 是主进程请求子进程退出的唯一信号，shutdown 会先设置它再等待进程自然退出。
        self._stop_event = self._context.Event()
        # bridge_work_event 唤醒主进程 bridge 消费子进程回流事件或本地待转发请求。
        self.bridge_work_event = self._context.Event()
        # command_queue 是父进程到助手子进程的唯一输入入口，命令本身即唤醒信号。
        self.command_queue = self._context.Queue()
        # event_queue 承载助手进程回流事件。
        self.event_queue = self._context.Queue()

        # streamvox_management_response_queue 承载助手子进程回传的管理响应。
        self.streamvox_management_response_queue = self._context.Queue()
        # 子进程完成模型、TTS 和常驻 worker 装配后才置位；Process.start() 本身不代表可服务。
        self._runtime_ready_event = self._context.Event()
        # _queues_closed 为 False 表示当前队列可继续 put/get；shutdown 会关闭它们并置回 True。
        self._queues_closed = False

    def _ensure_ipc_objects_open(self) -> None:
        """确保当前客户端持有可用 IPC 对象。

        允许客户端在 shutdown 后再次 start 或提交请求时自动重建队列。

        Returns:
            无。
        """

        # shutdown 后队列已经 close，继续 put 会抛错；这里用新队列表达新的生命周期。
        if self._queues_closed:
            self._create_ipc_objects()

    @classmethod
    def for_test_noop(cls, *, now: Callable[[], float]) -> MultiprocessingAssistantProcessClient:
        """构造测试用空转助手进程客户端。

        Args:
            now: 外部注入的时间函数。

        Returns:
            使用 run_noop_assistant_process 的客户端实例。
        """

        return cls(now=now, process_target=run_noop_assistant_process, config=None)

    @property
    def is_running(self) -> bool:
        """判断助手子进程是否仍在运行。

        Returns:
            子进程存在且仍存活时返回 True，否则返回 False。
        """

        # _process 为 None 代表没有可观察的子进程，直接返回 False 可以避免调用方处理空句柄。
        if self._process is None:
            return False

        return self._process.is_alive()

    @property
    def is_ready(self) -> bool:
        """判断助手子进程是否已经完成运行时装配。

        Returns:
            进程仍存活且子进程已发出就绪信号时返回 True。
        """

        return self.is_running and self._runtime_ready_event.is_set()

    def wait_until_ready(
        self,
        *,
        timeout: float = ASSISTANT_PROCESS_STARTUP_TIMEOUT_SECONDS,
    ) -> None:
        """等待助手子进程完成运行时装配。

        Args:
            timeout: 最长等待秒数。

        Raises:
            AssistantProcessNotReadyError: 子进程退出、尚未启动或等待超时。
        """

        if self._process is None:
            raise AssistantProcessNotReadyError("assistant process has not been started")

        # readiness 等待是生命周期时限，必须使用独立单调时钟，不能受业务 now
        # 注入、系统时钟校准或测试固定时间影响。
        deadline = time.monotonic() + max(0.0, timeout)
        while not self._runtime_ready_event.is_set():
            self._raise_if_started_process_stopped()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssistantProcessNotReadyError(
                    "assistant process startup timed out"
                )
            self._runtime_ready_event.wait(timeout=min(0.1, remaining))

        self._raise_if_started_process_stopped()

    def _raise_if_started_process_stopped(self) -> None:
        """阻止向已停止的助手子进程继续投递工作。

        检测曾经启动过但当前已经不存活的子进程，并向调用方暴露清晰错误。

        Returns:
            无。

        Raises:
            RuntimeError: 助手子进程曾经启动但当前已经停止。
        """

        # 从未启动时仍允许 pre-start queueing，保持现有测试和调用方可以先入队再 start 的语义。
        if self._process is None:
            return

        # 存活进程可以继续消费队列，提交和 drain 都按正常路径执行。
        if self._process.is_alive():
            return

        # 进程曾经存在但已经停止时，继续 put 只会把未来工作丢进无人消费的队列；这里直接失败并暴露退出码。
        exitcode = self._process.exitcode
        exitcode_text = "unknown" if exitcode is None else str(exitcode)
        raise AssistantProcessNotReadyError(
            f"assistant process has stopped; exitcode={exitcode_text}"
        )

    def _ensure_runtime_ready_for_submission(self) -> None:
        """阻止把业务命令投递到尚未完成装配的助手进程。"""

        # 尚未 start 时保留旧的预排队语义；正式生命周期会在发布前等待 readiness。
        if self._process is None:
            return
        self._raise_if_started_process_stopped()
        if not self._runtime_ready_event.is_set():
            raise AssistantProcessNotReadyError(
                "assistant runtime is not ready for command submission"
            )

    def start(self) -> None:
        """启动独立助手子进程。

        以幂等方式启动助手子进程，并把停止事件、业务队列和管理队列传入入口函数。

        Returns:
            无。
        """

        # 已经运行时不重复创建子进程，避免同一组队列被多个助手运行时同时消费。
        if self.is_running:
            return

        # shutdown 后再次启动时需要先创建新的 stop_event 和队列，避免复用已关闭 IPC 对象。
        self._ensure_ipc_objects_open()
        # 旧 stop_event 可能来自前一次 shutdown；重新启动前清除信号，让新进程能够进入正常循环。
        self._stop_event.clear()
        # 旧进程若已自然退出，先 join 回收句柄，避免进程资源在主进程中长期悬挂；
        # 上次 start 失败留下的进程对象从未启动，对它 join 会直接抛 AssertionError。
        if self._process is not None and self._process._popen is not None:
            self._process.join(timeout=0)

        # StreamVox qwen3 decoder 会在助手运行时内再创建内部子进程；Python daemon 进程禁止派生子进程。
        # 因此这里必须保持非 daemon，并依靠 FastAPI shutdown 和客户端 shutdown 显式收口。
        self._process = self._context.Process(
            target=self._process_target,
            args=(
                self._stop_event,
                self.bridge_work_event,
                self.command_queue,
                self.event_queue,
                self.streamvox_management_response_queue,
                self._runtime_ready_event,
                self._config,
            ),
            name="companion-assistant-runtime",
            daemon=False,
        )
        self._runtime_ready_event.clear()
        self._process.start()

    def cancel_current_generation(
        self, *, reason: str, generation_id: str | None = None
    ) -> AssistantControlCommand:
        """提交取消当前 generation 的控制命令。

        Args:
            reason: 取消原因或触发来源。
            generation_id: 可选的目标 generation 标识。

        Returns:
            已提交到控制队列的控制命令。

        Raises:
            RuntimeError: 助手子进程曾经启动但当前已经停止。
        """
        # 阻止向已停止的助手子进程继续投递工作。
        self._ensure_runtime_ready_for_submission()

        # 新建命令只在该入口生成时间戳；submit_control_command 会保留上游既有时间事实。
        command = CancelCurrentGenerationCommand(
            generation_id=generation_id,
            reason=reason,
            issued_at=self.now(),
        )
        self._ensure_ipc_objects_open()
        self.command_queue.put(AssistantProcessCommand(kind="assistant_control", payload=command))
        return command

    def submit_control_command(self, command: AssistantControlCommand) -> AssistantControlCommand:
        """提交既有助手控制命令。

        Args:
            command: 已经带有 generation_id、reason 和 issued_at 的控制命令。

        Returns:
            同一条已提交命令。

        Raises:
            RuntimeError: 助手子进程曾经启动但当前已经停止。
        """

        # 既有命令已经携带业务事实，本客户端只负责跨进程投递。
        self._ensure_runtime_ready_for_submission()
        self._ensure_ipc_objects_open()
        self.command_queue.put(AssistantProcessCommand(kind="assistant_control", payload=command))
        return command

    def warmup_foreground(self, *, reason: str) -> AssistantControlCommand:
        """提交前台模型 warmup 控制命令。

        Args:
            reason: 触发预热的业务原因。

        Returns:
            已提交到控制队列的控制命令。

        Raises:
            RuntimeError: 助手子进程曾经启动但当前已经停止。
        """

        self._ensure_runtime_ready_for_submission()
        command = WarmupForegroundCommand(
            reason=reason,
            issued_at=self.now(),
        )
        self._ensure_ipc_objects_open()
        self.command_queue.put(AssistantProcessCommand(kind="assistant_control", payload=command))
        return command

    def answer_background_agent_interaction(
        self,
        *,
        request_id: str,
        outcome: str,
        answers: dict[str, str] | None = None,
        reason: str = "",
        materials: tuple[AgentMaterial, ...] = (),
        user_text: str = "",
        submission_id: str | None = None,
        runtime_epoch: int | None = None,
    ) -> AssistantControlCommand:
        """把后台智能体交互回答提交到统一控制队列。"""

        if outcome not in {"allow", "deny", "answer", "cancel"}:
            raise ValueError("后台智能体交互回答 outcome 无效")
        command = BackgroundAgentInteractionAnswerCommand(
            request_id=request_id.strip(),
            outcome=outcome,  # type: ignore[arg-type]
            answers=dict(answers or {}),
            reason=reason.strip(),
            materials=tuple(materials),
            user_text=user_text.strip(),
            submission_id=(
                submission_id.strip()
                if isinstance(submission_id, str) and submission_id.strip()
                else None
            ),
            issued_at=self.now(),
        )
        return self.submit_control_command(command)

    def reject_background_agent_interaction_submission(
        self,
        *,
        submission_id: str,
        request_id: str,
        reason: str,
        runtime_epoch: int | None = None,
    ) -> AssistantControlCommand:
        """把 Registry 拒绝事实提交到助手子进程控制队列。"""

        del runtime_epoch
        if reason not in {"already_resolved", "background_unavailable"}:
            raise ValueError("工作交互提交拒绝原因无效")
        command = BackgroundAgentInteractionSubmissionRejectedCommand(
            submission_id=submission_id.strip(),
            request_id=request_id.strip(),
            reason=reason,
            issued_at=self.now(),
        )
        return self.submit_control_command(command)

    def submit_existing_request(self, request: AssistantRequest) -> AssistantRequest:
        """提交既有助手生成请求。

        Args:
            request: 已经带有 request_id、source_turn_id 和 requested_at 的助手请求。

        Returns:
            同一条已提交请求。

        Raises:
            RuntimeError: 助手子进程曾经启动但当前已经停止。
        """

        # 既有请求的编号、来源 turn 和时间戳都属于上游事实，跨进程客户端不得改写。
        self._ensure_runtime_ready_for_submission()
        self._ensure_ipc_objects_open()
        self.command_queue.put(AssistantProcessCommand(kind="assistant_request", payload=request))
        return request

    def notify_bridge_work_available(self) -> None:
        """通知主进程 bridge 已有可处理工作。"""

        self.bridge_work_event.set()

    def clear_bridge_work_available(self) -> None:
        """清除主进程 bridge 的工作可用信号。"""

        self.bridge_work_event.clear()

    def wait_for_bridge_work(self, timeout: float | None = None) -> bool:
        """等待主进程 bridge 的工作可用信号。"""

        return bool(self.bridge_work_event.wait(timeout))

    def drain_events(self, *, limit: int = 100) -> list[RuntimeEvent]:
        """非阻塞读取助手进程回流事件。

        Args:
            limit: 本次最多取出的事件数量。

        Returns:
            按入队顺序排列的运行时事件列表；limit 小于等于 0 时返回空列表。

        Raises:
            RuntimeError: 没有可读取事件且助手子进程曾经启动但当前已经停止。
        """

        # 非正预算代表调用方本轮不希望消费事件，直接返回并保持队列内容不变。
        if limit <= 0:
            return []

        self._ensure_ipc_objects_open()
        events: list[RuntimeEvent] = []
        while len(events) < limit:
            try:
                events.append(self.event_queue.get_nowait())
            except Empty:
                if events:
                    return events
                if self._process is not None and not self._process.is_alive():
                    try:
                        # multiprocessing.Queue 的 feeder 线程可能让刚写入的最后事件短暂不可见；进程已停时给一次极短窗口保住诊断事件。
                        events.append(self.event_queue.get(timeout=0.05))
                        continue
                    except Empty:
                        pass
                self._raise_if_started_process_stopped()
                return []
        return events

    def get_streamvox_runtime_state(self, *, timeout: float = 5.0) -> dict[str, Any]:
        """通过独立管理队列读取 StreamVox runtime 状态。

        Args:
            timeout: 同步等待子进程响应的最长秒数。

        Returns:
            子进程返回的 StreamVox 状态字典。
        """

        return self._submit_streamvox_management_request(kind="runtime_state", payload={}, timeout=timeout)

    def switch_streamvox_config(
        self,
        *,
        model: str,
        device: str,
        llama_backend: str | None,
        role_name: str,
        streamvox_seed: int | None,
        inference_options: dict[str, Any],
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        """通过独立管理队列切换 StreamVox 模型、设备和当前角色。

        Args:
            model: 目标模型名称。
            device: 目标推理设备。
            llama_backend: 目标 llama.cpp 后端策略；None 表示不显式指定。
            role_name: 目标角色名称。
            streamvox_seed: 目标推理随机种子。
            inference_options: 目标模型族推理参数。
            timeout: 同步等待子进程响应的最长秒数。

        Returns:
            子进程返回的 StreamVox 状态字典。
        """

        return self._submit_streamvox_management_request(
            kind="switch_config",
            payload={
                "model": model,
                "device": device,
                "llama_backend": llama_backend,
                "role_name": role_name,
                "streamvox_seed": streamvox_seed,
                "inference_options": inference_options,
            },
            timeout=timeout,
        )

    def delete_streamvox_role(self, *, role_name: str, timeout: float = 5.0) -> dict[str, Any]:
        """通过独立管理队列删除当前模型下的指定角色。

        Args:
            role_name: 待删除的角色名称。
            timeout: 同步等待子进程响应的最长秒数。

        Returns:
            子进程返回的 StreamVox 状态字典。
        """

        return self._submit_streamvox_management_request(
            kind="delete_role",
            payload={"role_name": role_name},
            timeout=timeout,
        )

    def create_streamvox_role(
        self,
        *,
        role_name: str,
        audio_path: str,
        prompt_text: str,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        """通过独立管理队列创建当前模型下的自定义角色。

        Args:
            role_name: 新角色名称。
            audio_path: 用于克隆音色的音频文件路径。
            prompt_text: 音频对应的提示文本。
            timeout: 同步等待子进程响应的最长秒数。

        Returns:
            子进程返回的 StreamVox 状态字典。
        """

        return self._submit_streamvox_management_request(
            kind="create_role",
            payload={
                "role_name": role_name,
                "audio_path": audio_path,
                "prompt_text": prompt_text,
            },
            timeout=timeout,
        )

    def preview_streamvox_role(self, *, role_name: str, text: str, timeout: float = 30.0) -> dict[str, Any]:
        """通过独立管理队列生成已创建角色的试听音频。

        Args:
            role_name: 试听使用的角色名称。
            text: 试听文本。
            timeout: 同步等待子进程响应的最长秒数。

        Returns:
            子进程返回的试听音频载荷。
        """

        return self._submit_streamvox_management_request(
            kind="preview_role",
            payload={
                "role_name": role_name,
                "text": text,
            },
            timeout=timeout,
        )

    def get_streamvox_prompt_data(self, *, role_name: str, timeout: float = 5.0) -> Any | None:
        """通过独立管理队列读取当前模型下指定角色的 prompt 数据。"""

        payload = self._submit_streamvox_management_request(
            kind="get_prompt_data",
            payload={"role_name": role_name},
            timeout=timeout,
        )
        return payload.get("prompt_data")

    def _submit_streamvox_management_request(
        self,
        *,
        kind: str,
        payload: dict[str, Any],
        timeout: float,
    ) -> dict[str, Any]:
        """提交 StreamVox 管理请求并同步等待响应。

        Args:
            kind: 管理动作类型。
            payload: 可跨进程序列化的管理参数。
            timeout: 同步等待子进程响应的最长秒数。

        Returns:
            子进程响应 payload 的浅拷贝。

        Raises:
            RuntimeError: 助手子进程停止、响应超时或子进程返回失败。
        """

        with self._streamvox_management_lock:
            # 所有管理动作共用同一 readiness 门禁，不能只保护状态读取；
            # 否则切换、试听和角色操作仍会在模型装配窗口内排队并伪装成 500。
            if not self.is_ready:
                raise AssistantProcessNotReadyError(
                    "assistant runtime is not ready for StreamVox management"
                )
            self._raise_if_started_process_stopped()
            self._ensure_ipc_objects_open()
            request = StreamVoxManagementRequest(
                request_id=self._runtime_identity_factory.new("streamvox-management"),
                kind=kind,  # type: ignore[arg-type]
                payload=payload,
                issued_at=self.now(),
            )
            self.command_queue.put(AssistantProcessCommand(kind="streamvox_management", payload=request))
            return self._wait_for_streamvox_management_response(
                response_queue=self.streamvox_management_response_queue,
                request=request,
                timeout=timeout,
            )

    def _wait_for_streamvox_management_response(
        self,
        *,
        response_queue: Any,
        request: StreamVoxManagementRequest,
        timeout: float,
    ) -> dict[str, Any]:
        """等待并匹配 StreamVox 管理响应。

        Args:
            response_queue: 跨进程响应队列。
            request: 当前等待匹配的管理请求。
            timeout: 最长等待秒数。

        Returns:
            匹配响应的 payload 浅拷贝。

        Raises:
            RuntimeError: 等待超时或子进程返回失败。
        """

        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError(f"StreamVox management request timed out: {request.kind}")
            try:
                response = response_queue.get(timeout=min(remaining, 0.1))
            except Empty as exc:
                self._raise_if_started_process_stopped()
                if time.monotonic() >= deadline:
                    raise RuntimeError(f"StreamVox management request timed out: {request.kind}") from exc
                continue
            if response.request_id != request.request_id:
                continue
            if not response.ok:
                error_message = response.error or "unknown StreamVox management error"
                raise RuntimeError(error_message)
            return dict(response.payload)

    def shutdown(self) -> tuple[RuntimeEvent, ...]:
        """关闭独立助手子进程。

        幂等请求子进程停止，先等待自然退出，超时后终止进程并回收句柄。

        Returns:
            子进程退出阶段最后写入、尚未被主进程 Bridge 取走的事件。
        """

        # 无论当前是否有进程，先设置停止信号，保证稍后启动前可以明确清理旧生命周期状态。
        self._stop_event.set()
        if not self._queues_closed:
            self.command_queue.put(AssistantProcessCommand(kind="shutdown"))
        self.bridge_work_event.set()

        # 从未启动时没有进程句柄需要回收，shutdown 保持幂等空操作。
        if self._process is None:
            self._close_ipc_objects()
            return ()

        # start 失败会留下从未启动的进程对象；对它 join 会抛 AssertionError，
        # 从而跳过后面的 IPC 清理，必须只在真正启动过时才回收进程句柄。
        if self._process._popen is not None:
            # 子进程优先消费同一 FIFO 中更早的控制命令，再由 shutdown 命令自然退出；
            # stop_event 只记录父进程已经发起关闭，不能抢过已排队的工具提交批准。
            self._process.join(timeout=ASSISTANT_PROCESS_NATURAL_SHUTDOWN_TIMEOUT_SECONDS)
            if self._process.is_alive():
                # Background 的非守护清理监控会让子进程等到 Browser Session、MCP transport
                # 和 Grant 真正收口；超过总宽限才说明清理已经失去响应，可以强制回收进程。
                self._process.terminate()
                self._process.join()
        # 进程句柄完成收口后清空，保持 is_running 和注释契约一致。
        self._process = None
        # 子进程会在 finally 中关闭后台工作并写出最后终态。必须在关闭 IPC 前取走，
        # 否则 Bridge 已停或热替换后，这批包含私有用量的事件没有其它恢复入口。
        shutdown_events = _drain_shutdown_event_queue(self.event_queue)
        # Queue.put 会启动主进程 feeder thread；进程退出后必须显式关闭队列，避免线程和句柄泄漏。
        self._close_ipc_objects()
        return shutdown_events

    def _close_ipc_objects(self) -> None:
        """关闭当前跨进程队列资源。

        关闭 request、control、event 和管理队列，并等待主进程 feeder thread 退出。

        Returns:
            无。
        """

        # 已关闭队列不能重复 close/join；幂等返回让 shutdown 可以安全多次调用。
        if self._queues_closed:
            return

        for queue in (
            self.command_queue,
            self.event_queue,
            self.streamvox_management_response_queue,
        ):
            # close 通知 QueueFeederThread 不再接收新数据，join_thread 等它把缓冲收完并退出。
            queue.close()
            queue.join_thread()
        self._queues_closed = True


def _drain_shutdown_event_queue(event_queue: Any) -> tuple[RuntimeEvent, ...]:
    """在子进程退出后取尽跨进程队列中可见的最后事件。

    Args:
        event_queue: 已经没有生产者的助手事件跨进程队列。

    Returns:
        按子进程写入顺序排列的合法运行时事件。
    """

    events: list[RuntimeEvent] = []
    while True:
        try:
            # multiprocessing.Queue 的 feeder 可能在进程退出后仍有极短可见性延迟；
            # 每次读取留一个小窗口，首次真正为空即代表最后批次已经排完。
            event = event_queue.get(timeout=SHUTDOWN_EVENT_DRAIN_GRACE_SECONDS)
        except Empty:
            return tuple(events)
        if isinstance(event, RuntimeEvent):
            events.append(event)

