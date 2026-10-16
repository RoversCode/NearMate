from __future__ import annotations

import time
import os
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from queue import Empty
from typing import Any, Deque

from companion.core.streamvox_runtime_config import read_prepared_streamvox_voice
from companion.core.runtime_paths import BACKEND_ROOT, DATA_ROOT
from companion.core.voice_setup_assets import sha256_file
from companion.activities.progress_scheduler import ActivityProgressScheduler
from companion.audio.providers.tts_provider import (
    StreamVoxProviderConfig,
    StreamVoxTtsProvider,
)
from companion.config.realtime_audio import (
    DebugAudioYamlConfig,
    LatencyYamlConfig,
    load_realtime_audio_config,
)
from companion.llm.client_factory import LlmClientFactory
from companion.llm.foreground_turn_processor import ForegroundTurnProcessor
from companion.llm.config import LlmSettings
from companion.llm.client_protocol import LlmClientProtocol
from companion.llm.model_registry import QWEN_COGNITIVE_REPAIR_INTEGRATION_ID
from companion.foreground_self_reflection.coordinator import ForegroundSelfReflectionCoordinator
from companion.foreground_tools.orchestration.commit_barrier import (
    BackgroundCommitDrainBarrier,
)
from companion.foreground_tools.catalog.service import ForegroundToolService
from companion.foreground_tools.cognitive.executor import CognitiveTaskExecutor
from companion.foreground_tools.cognitive.activity_executor import CognitiveActivityExecutor
from companion.foreground_tools.cognitive.result_repairer import (
    QwenFlashCognitiveResultRepairer,
)
from companion.foreground_tools.cognitive.result_resolver import CognitiveResultResolver
from companion.foreground_tools.background_agent.execution import BackgroundAgentToolExecutor
from companion.foreground_tools.catalog.waiting_policy import ToolWaitingPolicyResolver, ToolWaitingPolicyStore
from companion.foreground_tools.background_agent.submission_broker import (
    WorkInteractionSubmissionBroker,
)
from companion.foreground_tools.task_memory.broker import TaskMemoryLookupBroker
from companion.foreground_tools.activity_control.broker import (
    ActivityCancellationBroker,
)
from companion.foreground_tools.timed_reminders.broker import (
    TimedReminderCommandBroker,
)
from companion.foreground_self_reflection.tool_commit_broker import (
    SelfReflectionToolCommitBroker,
)
from companion.runtime_memory.compaction import RuntimeMemoryCompactionWorker
from companion.observability.logging_config import (
    configure_backend_logging,
    get_backend_logger,
    shutdown_backend_logging,
)
from companion.observability.realtime_latency import RealtimeLatencyRecorder
from companion.observability.prompt_audit import PromptAuditRecorder
from companion.records.debug import InteractionDebugRecorder, NoopInteractionDebugRecorder
from companion.prompt.builder import PromptBuilder
from companion.realtime.assistant_events import (
    AssistantControlCommand,
    ActivityCancellationResultCommand,
    AssistantProcessCommand,
    AssistantRequest,
    RuntimeEvent,
    CancelActivityCommand,
    BackgroundAgentInteractionAnswerCommand,
    BackgroundAgentInteractionSubmissionRejectedCommand,
    TaskMemoryLookupResultCommand,
    TimedReminderCommandResultCommand,
    RuntimeMemoryCompactionPlanCommand,
    CancelCurrentGenerationCommand,
    CancelSupersededSystemNoticeCommand,
    CancelTimedReminderNoticeCommand,
    StreamVoxManagementRequest,
    StreamVoxManagementResponse,
    WarmupForegroundCommand,
    SelfReflectionToolCommitDecisionCommand,
)
from companion.realtime.assistant_output_lanes import AssistantOutputLanes
from companion.realtime.assistant_child_runtime import AssistantChildRuntime
from companion.session.context import SessionContext
from companion.character_cards.runtime_config import CharacterExperienceRuntimeConfig
from companion.capabilities.weather.config import QWeatherConfig
from companion.foreground_tools.external.weather_tool import WeatherForegroundTool
from companion.foreground_tools.external.windows_application_tool import (
    WindowsApplicationForegroundTool,
)
from companion.background_agent.activity_executor import BackgroundAgentActivityExecutor
from companion.background_agent.config_provider import BackgroundAgentConfigProvider
from companion.background_agent.cua_integration import (
    CUA_SESSION_CLEANUP_TIMEOUT_SECONDS,
    CUA_SKILL_ID,
    CuaIntegration,
)
from companion.background_agent.cua_capability import CuaCapabilitySlot
from companion.background_agent.settings import BackgroundAgentSettings
from companion.background_agent.skill_registry import SkillRegistry
from companion.background_agent.workspace import WorkspaceManager
from companion.background_agent.interaction_bridge import AgentInteractionAnswer
from companion.records.bootstrap_secret_store import BootstrapSecretStore


logger = get_backend_logger(__name__)


ASSISTANT_CHILD_RUNTIME_SHUTDOWN_TIMEOUT_SECONDS = 1.0
# 父进程先覆盖子运行时同步预算和 Browser CLI 单步清理上限，再留一秒给 transport 与 Grant 收口。
ASSISTANT_PROCESS_NATURAL_SHUTDOWN_TIMEOUT_SECONDS = (
    ASSISTANT_CHILD_RUNTIME_SHUTDOWN_TIMEOUT_SECONDS
    + CUA_SESSION_CLEANUP_TIMEOUT_SECONDS
    + 1.0
)


@dataclass(frozen=True)
class AssistantProcessConfig:
    """助手子进程可序列化配置。

    保存真实 LLM、TTS 和会话所需的纯数据字段，避免把 Settings 或 SDK client 跨进程序列化。
    """

    llm_settings: dict[str, Any]
    qweather_api_host: str
    qweather_api_key: str
    qweather_timeout_s: float
    tool_waiting_policies_path: str
    # StreamVox 当前运行模型；角色缓存按模型隔离，因此必须随子进程配置固化。
    streamvox_model: str
    # StreamVox 运行设备，控制台只暴露 auto、cpu、gpu，多卡场景固定使用默认 0 卡。
    streamvox_device: str
    # StreamVox GGUF 模型的 llama.cpp 后端策略；None 表示不显式传参。
    streamvox_llama_backend: str | None
    # 当前角色卡已经过用户试听确认的音色名称。
    streamvox_role_name: str
    # StreamVox 推理随机种子；None 表示不显式传给 SDK。
    streamvox_seed: int | None
    # 当前模型族专属推理参数，例如 Qwen3 language 或 VoxCPM2 mode。
    streamvox_inference_options: dict[str, Any]
    # 参考音频路径保持字符串形态跨进程传递，子进程装配 provider 时再转换为 Path。
    streamvox_reference_audio_path: str | None
    # 角色卡参考文字仅用于核对已确认音色，启动过程不创建音色。
    streamvox_prompt_text: str | None = None
    # 子进程在加载模型前重读确认状态，不能复用已失效的父进程快照。
    streamvox_execution_config_path: str = ""
    streamvox_voice_setup_path: str = ""
    # debug_audio_* 字段让助手子进程记录 LLM/TTS 生成事实，避免子进程自己重新猜配置。
    debug_audio_enabled: bool = False
    debug_audio_output_dir: str = DebugAudioYamlConfig.output_dir
    debug_audio_max_session_files: int = DebugAudioYamlConfig.max_session_files
    # Prompt Audit 配置由父进程冻结后传入，子进程不会重新读取 YAML。
    prompt_audit_enabled: bool = False
    prompt_audit_output_dir: str = ""
    prompt_audit_max_runs: int = 20
    # Background 终端诊断由父进程冻结后传入，子进程不会重新读取 env.yaml。
    background_agent_terminal_diagnostics_enabled: bool = False
    # latency_* 字段让助手子进程与输入侧进程共写同一份 JSONL；父进程传入的路径已锚定为绝对路径，规避子进程 cwd 漂移导致 marker 落到别的文件。
    latency_enabled: bool = LatencyYamlConfig.enabled
    latency_output_path: str = LatencyYamlConfig.output_path
    # 后台智能体新任务直接读取现有用户设置文件和启动密钥库，保存后不必热替换运行中任务。
    background_agent_user_settings_path: str = ""
    background_agent_bootstrap_db_path: str = ""
    background_agent_defaults: dict[str, Any] = field(default_factory=dict)
    background_agent_runtime_dir: str = ""
    background_agent_workspace_root: str = ""
    background_agent_asset_root: str = ""

    @classmethod
    def from_settings(
        cls,
        settings: Any,
        *,
        experience_config: CharacterExperienceRuntimeConfig,
    ) -> AssistantProcessConfig:
        """从 Settings 和当前激活角色卡复制助手子进程需要的纯配置字段。

        Args:
            settings: 具备助手子进程所需属性的应用配置对象。
            experience_config: 当前激活角色卡派生的运行时体验配置。

        Returns:
            包含 LLM、TTS 和会话字段的不可变配置。

        Raises:
            AttributeError: settings 缺少必要配置字段。
        """
        # 读取backend\config\realtime_audio.yaml
        realtime_audio_config = load_realtime_audio_config()
        return cls(
            llm_settings=settings.llm.model_dump(mode="json"), # model_dump是Pydantic的方法
            qweather_api_host=settings.qweather_api_host,
            qweather_api_key=settings.qweather_api_key,
            qweather_timeout_s=settings.qweather_timeout_s,
            tool_waiting_policies_path=settings.tool_waiting_policies_path,
            streamvox_model=experience_config.streamvox_model,
            streamvox_device=settings.streamvox_device,
            streamvox_llama_backend=settings.streamvox_llama_backend,
            streamvox_role_name=experience_config.streamvox_role_name,
            streamvox_seed=experience_config.streamvox_seed,
            streamvox_inference_options=dict(experience_config.streamvox_inference_options),
            streamvox_reference_audio_path=(
                str(experience_config.streamvox_reference_audio_path)
                if experience_config.streamvox_reference_audio_path is not None
                else None
            ),
            streamvox_prompt_text=experience_config.streamvox_prompt_text,
            streamvox_execution_config_path=str(settings.execution_config_path),
            streamvox_voice_setup_path=str(settings.voice_setup_path),
            debug_audio_enabled=realtime_audio_config.debug_audio.enabled,
            debug_audio_output_dir=realtime_audio_config.debug_audio.output_dir,
            debug_audio_max_session_files=realtime_audio_config.debug_audio.max_session_files,
            prompt_audit_enabled=settings.prompt_audit.enabled,
            prompt_audit_output_dir=str(settings.prompt_audit.resolved_output_dir()),
            prompt_audit_max_runs=settings.prompt_audit.max_runs,
            background_agent_terminal_diagnostics_enabled=(
                settings.background_agent_terminal_diagnostics_enabled
            ),
            latency_enabled=realtime_audio_config.latency.enabled,
            latency_output_path=realtime_audio_config.latency.output_path,
            background_agent_user_settings_path=settings.user_settings_path,
            background_agent_bootstrap_db_path=settings.bootstrap_db_path,
            background_agent_defaults=settings.background_agent.model_dump(mode="json"),
            background_agent_runtime_dir=settings.background_agent_runtime_dir,
            background_agent_workspace_root=settings.background_agent_workspace_root,
            background_agent_asset_root=settings.background_agent_asset_root,
        )


def run_assistant_process(
    stop_event: Any,
    bridge_work_event: Any,
    command_queue: Any,
    event_queue: Any,
    streamvox_management_response_queue: Any,
    runtime_ready_event: Any,
    config: AssistantProcessConfig,
) -> None:
    """运行真实助手子进程入口。

    在子进程内装配真实助手运行时，并持续消费请求、控制命令和 StreamVox 管理命令。
    command_queue 是统一的入口信箱（所有父→子的命令都走这里），但出口根据通信模式不同分开了：

    普通请求 / control → 异步事件流 → event_queue
    StreamVox 管理命令 → 同步 RPC 响应 → streamvox_management_response_queue

    Args:
        stop_event: 记录父进程已经发起关闭的跨进程事件；真实退出仍以
            command_queue 中的 FIFO shutdown 命令为准。
        bridge_work_event: 唤醒主进程 bridge 消费回流事件的跨进程事件。
        command_queue: 接收父进程输入和 runner tick 的统一命令队列。
        event_queue: 回流助手运行时事件的跨进程队列。
        streamvox_management_response_queue: 回传 StreamVox 管理响应的跨进程队列。
        runtime_ready_event: 子进程完成运行时装配后通知父进程的事件。
        config: 子进程装配真实依赖所需的纯数据配置。
    """

    child_runtime: AssistantChildRuntime | None = None
    stage = "bootstrap"
    try:
        _bootstrap_assistant_process_runtime()

        stage = "build_child_runtime"
        # assistant runtime 必须在子进程中构造，业务意图是让 SDK client、TTS engine 和线程队列都留在输出侧进程边界内。
        child_runtime = _build_assistant_child_runtime(
            config,
            event_notifier=lambda: command_queue.put(AssistantProcessCommand(kind="runner_event_tick")),
        )
        # 只有完整运行时和 StreamVox provider 都装配成功，父进程才允许发布这个世代。
        runtime_ready_event.set()
        pending_commands: Deque[AssistantProcessCommand] = deque[AssistantProcessCommand]()

        # shutdown 命令与普通控制命令共用父→子 FIFO。stop_event 只表示关闭已经发起，
        # 不能越过更早入队的工具批准、取消或其它权威控制命令。
        while True:
            stage = "process_command"
            try:
                command = pending_commands.popleft() if pending_commands else command_queue.get(timeout=0.25)
            except Empty:
                continue
            if not isinstance(command, AssistantProcessCommand):
                continue
            if command.kind == "shutdown":
                break
            _process_assistant_command(
                command,
                command_queue=command_queue,
                child_runtime=child_runtime,
                event_queue=event_queue,
                bridge_work_event=bridge_work_event,
                streamvox_management_response_queue=streamvox_management_response_queue,
                pending_commands=pending_commands,
            )
    except BaseException as exception:
        # 子进程日志保留完整 traceback，回传事件只允许主进程据此做精确收口，不能泄露异常正文。
        logger.exception(
            "assistant runtime crashed stage=%s exception_type=%s",
            stage,
            type(exception).__name__,
        )
        try:
            event_queue.put(
                RuntimeEvent(
                    type="runtime.internal_crash",
                    source="assistant_process",
                    payload={
                        "exception_type": type(exception).__name__,
                        "stage": stage,
                    },
                )
            )
            bridge_work_event.set()
        except Exception:
            logger.exception("无法回传 assistant runtime crash 诊断事件")
        raise
    finally:
        try:
            if child_runtime is not None:
                # 退出前先关闭常驻 worker，再尽量排空 shutdown 前已经完成的事件。
                child_runtime.shutdown(
                    timeout=ASSISTANT_CHILD_RUNTIME_SHUTDOWN_TIMEOUT_SECONDS
                )
                _drain_child_runtime_events(
                    child_runtime,
                    event_queue=event_queue,
                    bridge_work_event=bridge_work_event,
                )
        finally:
            shutdown_backend_logging()


def _process_assistant_command(
    command: AssistantProcessCommand,
    *,
    command_queue: Any,
    child_runtime: AssistantChildRuntime,
    event_queue: Any,
    bridge_work_event: Any,
    streamvox_management_response_queue: Any,
    pending_commands: Deque[AssistantProcessCommand] | None = None,
) -> None:
    """处理一条助手子进程统一命令。

    Args:
        command: 父进程或 runner 投递给助手子进程的命令。
        command_queue: 同一条命令队列，用于吸收积压的连续生成请求。
        child_runtime: 子进程内唯一助手运行资源所有者。
        event_queue: 回流主进程的助手运行时事件队列。
        bridge_work_event: 唤醒主进程 bridge 消费事件的事件。
        streamvox_management_response_queue: StreamVox 管理命令的响应队列。
        pending_commands: 需要保持 FIFO 顺序、留给主循环稍后处理的非请求命令。
    """

    if command.kind == "assistant_control":
        _process_assistant_control_command(command.payload, child_runtime)
    elif command.kind == "assistant_request":
        _process_assistant_request_command(
            command.payload,
            command_queue=command_queue,
            child_runtime=child_runtime,
            pending_commands=pending_commands,
        )
    elif command.kind == "streamvox_management":
        _process_streamvox_management_command(
            command.payload,
            response_queue=streamvox_management_response_queue,
            child_runtime=child_runtime,
        )

    _drain_child_runtime_events(
        child_runtime,
        event_queue=event_queue,
        bridge_work_event=bridge_work_event,
    )


def _bootstrap_assistant_process_runtime() -> None:
    """初始化助手子进程的通用运行时环境。"""

    # 助手子进程在 Windows spawn 下不会继承主进程 logger handler，必须自行完成日志启动。
    configure_backend_logging(process_role="assistant-runtime")


def _drain_child_runtime_events(
    child_runtime: AssistantChildRuntime,
    *,
    event_queue: Any,
    bridge_work_event: Any,
) -> None:
    """把助手子进程内部事件转发到主进程。

    Args:
        child_runtime: 产生运行时事件的子进程资源所有者。
        event_queue: 跨进程发送事件的主队列。
        bridge_work_event: 通知主进程存在待处理桥接工作的事件。
    """

    for event in child_runtime.drain_events():
        if not child_runtime.forward_event_to_main(
            event,
            send_event=event_queue.put,
        ):
            continue
        try:
            bridge_work_event.set()
        except Exception:
            # 事件已经进入跨进程队列，主进程 Bridge 仍会按兜底周期主动排水。
            # 唤醒失败不能让 Broker 把已转交的 Background 提交倒退成本地取消。
            logger.exception("助手跨进程事件队列唤醒失败")


def _build_assistant_child_runtime(
    config: AssistantProcessConfig,
    *,
    event_notifier: Any | None = None,
) -> AssistantChildRuntime:
    """按子进程配置构造助手运行资源。

    集中装配真实输出侧依赖，让子进程主循环只关注队列协议。

    Args:
        config: 父进程传入的纯数据配置。
        event_notifier: runner 内部产生事件时唤醒子进程主循环的可选回调。

    Returns:
        已启动常驻 worker 的 AssistantChildRuntime。
    """

    packaged_runtime = os.environ.get("NEARMATE_PACKAGED") == "1"
    prepared_voice = None
    if packaged_runtime:
        prepared_voice = read_prepared_streamvox_voice(
            execution_config_path=Path(config.streamvox_execution_config_path),
            voice_setup_path=Path(config.streamvox_voice_setup_path),
            model=config.streamvox_model, role_name=config.streamvox_role_name,
        )
    context = prepared_voice.context if prepared_voice is not None else None
    reference_audio_path = Path(config.streamvox_reference_audio_path).resolve() if config.streamvox_reference_audio_path else None
    if packaged_runtime:
        if (reference_audio_path is None or not reference_audio_path.is_file()
                or str(reference_audio_path) != str(Path(context.get("reference_audio", "")).resolve())
                or sha256_file(reference_audio_path) != context.get("reference_audio_sha256")
                or config.streamvox_prompt_text != context.get("prompt_text")
                or config.streamvox_seed != context.get("seed")
                or config.streamvox_inference_options != context.get("inference_options")):
            raise RuntimeError("当前角色的参考素材或推理参数已改变，请重新确认语音")
        # 安装版首次配置和正式助手必须使用同一目录，SDK 导入前就固定缓存归属。
        os.environ["STREAMVOX_HOME"] = str(prepared_voice.voice_directory)

    qweather_config = QWeatherConfig(
        api_host=config.qweather_api_host,
        api_key=config.qweather_api_key,
        timeout_s=config.qweather_timeout_s,
    )
    # 通 Python 数据，按 LlmSettings 这张结构说明书，校验并还原成一个 LlmSettings 实例。
    llm_settings = LlmSettings.model_validate(config.llm_settings)
    prompt_audit_recorder = PromptAuditRecorder(
        enabled=config.prompt_audit_enabled,
        output_dir=config.prompt_audit_output_dir,
        max_runs=config.prompt_audit_max_runs,
        process_role="assistant-runtime",
    )
    llm_factory = LlmClientFactory(
        llm_settings,
        prompt_audit_recorder=prompt_audit_recorder,
    )
    cognitive_client = llm_factory.client("cognitive")
    cognitive_repair_client = llm_factory.integration_client(
        QWEN_COGNITIVE_REPAIR_INTEGRATION_ID,
        purpose="cognitive_result_repair",
    )
    summary_client = llm_factory.client("summary")

    # 共享的进度调度器
    activity_progress_scheduler = ActivityProgressScheduler(monotonic_now=time.monotonic)

    # 认知任务
    cognitive_executor = CognitiveTaskExecutor(client=cognitive_client)
    cognitive_activity_executor = CognitiveActivityExecutor(
        task_executor=cognitive_executor,
        result_resolver=CognitiveResultResolver(
            repairer=QwenFlashCognitiveResultRepairer(
                client=cognitive_repair_client,
            )
        ),
        monotonic_now=time.monotonic,
        event_notifier=event_notifier,
        progress_scheduler=activity_progress_scheduler,
    )

    # 后台智能体与 Foreground 共用助手子进程，但 SDK client 自己运行在专属线程；
    # 产品私有 runtime 根为每项 Activity 派生独占 CLAUDE_CONFIG_DIR；
    # 共享业务工作区的默认位置仍由系统文档目录解析器决定。
    agent_runtime_dir = Path(
        config.background_agent_runtime_dir
        or (DATA_ROOT / "agent-runtime")
    )
    agent_workspace_root = (
        Path(config.background_agent_workspace_root)
        if config.background_agent_workspace_root
        else None
    )
    agent_asset_root = Path(
        config.background_agent_asset_root
        or (BACKEND_ROOT / "assets" / "agent-workspace")
    )
    skill_registry = SkillRegistry(asset_root=agent_asset_root)
    background_agent_config_provider = BackgroundAgentConfigProvider(
        user_settings_path=config.background_agent_user_settings_path,
        secret_store=BootstrapSecretStore(config.background_agent_bootstrap_db_path),
        config_dir=agent_runtime_dir,
        skill_registry=skill_registry,
        defaults=BackgroundAgentSettings.model_validate(
            config.background_agent_defaults
        ),
    )
    background_commit_barrier = BackgroundCommitDrainBarrier()
    cua_integration = CuaIntegration(
        skill_root=skill_registry.skills_root / CUA_SKILL_ID,
    )
    cua_capability = CuaCapabilitySlot()
    background_agent_executor = BackgroundAgentActivityExecutor(
        config_provider=background_agent_config_provider,
        runtime_dir=agent_runtime_dir,
        workspace_manager=WorkspaceManager(
            asset_root=agent_asset_root,
            workspace_root=agent_workspace_root,
            skill_registry=skill_registry,
        ),
        event_notifier=event_notifier,
        progress_scheduler=activity_progress_scheduler,
        commit_barrier=background_commit_barrier,
        # 后台执行体走 Claude Agent SDK，绕过 Chat Completions 客户端；同一份
        # 子进程审计记录器透传进去，后台 Prompt 才能落到同一个审计目录。
        prompt_audit_recorder=prompt_audit_recorder,
        cua_integration=cua_integration,
        cua_capability=cua_capability,
        terminal_diagnostics_enabled=(
            config.background_agent_terminal_diagnostics_enabled
        ),
    )
    work_interaction_submission_broker = WorkInteractionSubmissionBroker(
        event_notifier=event_notifier,
    )
    task_memory_lookup_broker = TaskMemoryLookupBroker(
        event_notifier=event_notifier,
    )
    activity_cancellation_broker = ActivityCancellationBroker(
        event_notifier=event_notifier,
    )
    timed_reminder_command_broker = TimedReminderCommandBroker(
        event_notifier=event_notifier,
    )
    runtime_memory_compaction_worker = RuntimeMemoryCompactionWorker(
        summary_client=summary_client,
        event_notifier=event_notifier,
    )
    # 所有助手子进程事实共用这一条 FIFO，保证 Recovery 工具事实不会被普通输出超车。
    runtime_event_queue: Any = __import__("queue").Queue()

    def emit_self_reflection_event(event: RuntimeEvent) -> None:
        """写入 Self-Reflection 内部事实，并立即唤醒跨进程转发循环。

        Args:
            event: Recovery 结果或同步工具提交请求。
        """

        runtime_event_queue.put(event)
        if event_notifier is not None:
            event_notifier()

    self_reflection_tool_commit_broker = SelfReflectionToolCommitBroker(
        emit_event=emit_self_reflection_event,
    )

    # 管理整个可申明的工具
    foreground_tools = ForegroundToolService(
        weather_tools=WeatherForegroundTool.from_config(qweather_config),
        windows_application_tools=WindowsApplicationForegroundTool.from_system(),
    )
    background_tool_executor = BackgroundAgentToolExecutor(
        background_agent_executor,
        work_interaction_submission_broker,
        task_memory_lookup_broker,
        commit_barrier=background_commit_barrier,
    )

    foreground_role_client = llm_factory.client("foreground")
    # 预热
    _warmup_foreground_role_client(foreground_role_client)

    self_reflection_coordinator = ForegroundSelfReflectionCoordinator(
        foreground_tools=foreground_tools,
        tool_commit_broker=self_reflection_tool_commit_broker,
        background_tool_executor=background_tool_executor,
        monotonic_now=time.monotonic,
        commit_barrier=background_commit_barrier,
    )
    foreground_turn_processor = ForegroundTurnProcessor(
        client=foreground_role_client,
        prompt_builder=PromptBuilder(),
        session_context=SessionContext(),
        foreground_tools=foreground_tools,
        cognitive_activity_executor=cognitive_activity_executor,
        background_agent_activity_executor=background_agent_executor,
        work_interaction_submission_broker=(
            work_interaction_submission_broker
        ),
        task_memory_lookup_broker=task_memory_lookup_broker,
        activity_cancellation_broker=activity_cancellation_broker,
        timed_reminder_command_broker=timed_reminder_command_broker,
        tool_waiting_policy_resolver=ToolWaitingPolicyResolver(
            store=ToolWaitingPolicyStore(config.tool_waiting_policies_path)
        ),
        runtime_memory_compaction_worker=runtime_memory_compaction_worker,
        background_agent_tool_executor=background_tool_executor,
        background_commit_barrier=background_commit_barrier,
        self_reflection_tool_executor=self_reflection_coordinator,
    )
    tts_provider = StreamVoxTtsProvider(
        config=StreamVoxProviderConfig(
            model=config.streamvox_model,
            device=("cpu" if prepared_voice.onnx_provider == "cpu" else "gpu") if prepared_voice is not None else config.streamvox_device,
            llama_backend=prepared_voice.llama_backend if prepared_voice is not None else config.streamvox_llama_backend,
            onnx_provider=prepared_voice.onnx_provider if prepared_voice is not None else None,
            model_directory=prepared_voice.model_directory if prepared_voice is not None else None,
            voice_directory=prepared_voice.voice_directory if prepared_voice is not None else None,
            execution_config_path=Path(config.streamvox_execution_config_path) if prepared_voice is not None else None,
            voice_setup_path=Path(config.streamvox_voice_setup_path) if prepared_voice is not None else None,
            reference_audio_path=reference_audio_path,
            role_name=config.streamvox_role_name,
            prompt_text=config.streamvox_prompt_text or "",
            streamvox_seed=config.streamvox_seed,
            inference_options=dict(config.streamvox_inference_options),
            # 源码开发态角色卡可能尚未走正式语音引导；有参考音频时允许按角色名创建
            # 本地 StreamVox prompt，安装包仍由正式配置和素材指纹门禁保护。
            allow_missing_requested_role_creation=(
                not packaged_runtime and reference_audio_path is not None and reference_audio_path.is_file()
            ),
            allow_development_fallback=not packaged_runtime,
        )
    )
    if config.debug_audio_enabled:
        debug_recorder = InteractionDebugRecorder(
            enabled=True,
            output_dir=config.debug_audio_output_dir,
            session_id_provider=lambda: "assistant-runtime",
            max_session_files=config.debug_audio_max_session_files,
        )
    else:
        debug_recorder = NoopInteractionDebugRecorder()

    # latency 记录器与输入侧 livekit_media_session 共写同一份 JSONL；父进程传入的路径已是绝对路径，规避子进程 cwd 漂移。关闭时不创建实例，runner 已按 None 判空。
    if config.latency_enabled:
        latency_recorder = RealtimeLatencyRecorder(jsonl_path=config.latency_output_path)
    else:
        latency_recorder = None

    runner = AssistantOutputLanes(
        agent=foreground_turn_processor,
        tts_provider=tts_provider,
        now=time.monotonic,
        event_queue=runtime_event_queue,
        event_notifier=event_notifier,
        debug_recorder=debug_recorder,
        latency_recorder=latency_recorder,
    )

    def shutdown_cua_after_background(timeout: float) -> None:
        """把官方 Host.stop 延后到 Background 提交和 Runner 都完成后。

        Args:
            timeout: 当前 ChildRuntime 分配给该关闭回调的剩余秒数。

        Returns:
            无返回值；实际 Host.stop 由 Background 关闭完成回调执行。
        """

        shutdown_deadline = time.monotonic() + max(0.0, timeout)

        def stop_cua_host() -> None:
            """在 Background 关闭完成回调中停止共享官方 Host。

            Returns:
                无返回值；Host 超时只记录诊断。
            """

            cua_integration.shutdown(
                timeout=max(0.0, shutdown_deadline - time.monotonic())
            )

        background_agent_executor.run_after_shutdown(stop_cua_host)

    child_runtime = AssistantChildRuntime(
        runner=runner,
        cancel_activity_handlers={
            "cognitive": lambda activity_id, reason: cognitive_activity_executor.cancel_running(
                activity_id,
                reason=reason,
            ),
            "background_agent": background_agent_executor.cancel_running,
        },
        answer_background_interaction=lambda command: background_agent_executor.respond_to_work_interaction(
            answer=AgentInteractionAnswer(
                request_id=command.request_id,
                outcome=command.outcome,
                answers=dict(command.answers),
                reason=command.reason,
                user_text=command.user_text,
                submission_id=command.submission_id,
            ),
            materials=command.materials,
        ),
        work_interaction_submission_broker=(
            work_interaction_submission_broker
        ),
        task_memory_lookup_broker=task_memory_lookup_broker,
        activity_cancellation_broker=activity_cancellation_broker,
        timed_reminder_command_broker=timed_reminder_command_broker,
        runtime_memory_compaction_worker=runtime_memory_compaction_worker,
        self_reflection_coordinator=self_reflection_coordinator,
        self_reflection_tool_commit_broker=(
            self_reflection_tool_commit_broker
        ),
        shutdown_callbacks=(
            foreground_turn_processor.shutdown,
            cognitive_activity_executor.shutdown,
            background_agent_executor.shutdown,
            shutdown_cua_after_background,
            activity_progress_scheduler.shutdown,
            runtime_memory_compaction_worker.shutdown,
            # SDK 的 close 不接受超时；预算耗尽后也要尝试释放各自的网络连接。
            lambda _timeout: foreground_role_client.close(),
            lambda _timeout: cognitive_client.close(),
            lambda _timeout: cognitive_repair_client.close(),
            lambda _timeout: summary_client.close(),
        ),
    )
    # 助手进程启动时显式启动常驻 worker，后续请求只投递工作项，不再每轮创建临时线程。
    child_runtime.start()
    return child_runtime


def _warmup_foreground_role_client(client: LlmClientProtocol) -> None:
    """在助手进程启动阶段预热前台模型连接。

    Args:
        client: 支持可选预热能力的前台模型客户端。
    """

    try:
        client.warmup()
    except Exception:
        # 预热只是把首轮冷启动提前，不应阻断真正的助手运行时启动。
        logger.exception("Foreground LLM warmup failed during assistant process bootstrap")


def _process_streamvox_management_command(
    request: object,
    *,
    response_queue: Any,
    child_runtime: AssistantChildRuntime,
) -> None:
    """处理主进程发来的 StreamVox 语音模型管理命令。

    这些命令和普通的 assistant 请求不一样，它们是去管理 TTS 音色/配置的，
    比如查状态、创建角色、切换模型等。处理完后要把结果写回 response_queue。

    Args:
        request: 主进程提交的语音模型管理请求。
        response_queue: 返回管理结果的跨进程队列。
        child_runtime: 持有当前 TTS provider 的助手子进程运行时。
    """

    if not isinstance(request, StreamVoxManagementRequest):
        return

    try:
        provider = child_runtime.tts_provider
        if request.kind == "runtime_state":
            # 查询当前 StreamVox 运行状态。
            payload = provider.runtime_state()
        elif request.kind == "delete_role":
            # 删除指定角色，然后返回当前状态。
            role_name = str(request.payload.get("role_name", ""))
            provider.delete_role(role_name)
            payload = provider.runtime_state()
        elif request.kind == "create_role":
            # 用一段音频克隆一个新角色。
            payload = provider.create_role(
                role_name=str(request.payload.get("role_name", "")),
                audio_path=Path(str(request.payload.get("audio_path", ""))),
                prompt_text=str(request.payload.get("prompt_text", "")),
            )
        elif request.kind == "preview_role":
            # 用指定角色试听一段文本。
            payload = provider.preview_role(
                role_name=str(request.payload.get("role_name", "")),
                text=str(request.payload.get("text", "")),
            )
        elif request.kind == "get_prompt_data":
            # 读取指定角色的 prompt 数据。
            payload = {
                "prompt_data": provider.get_prompt_data(
                    role_name=str(request.payload.get("role_name", ""))
                )
            }
        elif request.kind == "switch_config":
            # 切换 StreamVox 的模型、设备、当前角色等配置。
            raw_inference_options = request.payload.get("inference_options", {})
            if not isinstance(raw_inference_options, dict):
                raise ValueError("StreamVox inference_options must be an object")
            payload = provider.switch_config(
                model=str(request.payload.get("model", "")),
                device=str(request.payload.get("device", "auto")),
                llama_backend=(
                    None
                    if request.payload.get("llama_backend") is None
                    else str(request.payload.get("llama_backend", ""))
                ),
                role_name=str(request.payload.get("role_name", "")),
                streamvox_seed=(
                    None
                    if request.payload.get("streamvox_seed") is None
                    else int(request.payload.get("streamvox_seed", 0))
                ),
                inference_options=dict(raw_inference_options),
                # provider 完成无副作用预检后再取消旧轮，避免无效管理请求打断正常回复。
                before_switch=lambda: child_runtime.runner.cancel_current_generation(
                    reason="streamvox_config_switch"
                ),
            )
        else:
            raise ValueError(f"unknown StreamVox management kind: {request.kind}")
        # 管理命令执行成功，把结果封装成响应。
        response = StreamVoxManagementResponse(
            request_id=request.request_id,
            ok=True,
            payload=payload,
            error=None,
            produced_at=time.monotonic(),
        )
    except Exception as exception:
        logger.exception(
            "StreamVox management command failed kind=%s payload=%s",
            request.kind,
            request.payload,
        )
        # 管理命令执行失败，把错误信息封装成响应。
        response = StreamVoxManagementResponse(
            request_id=request.request_id,
            ok=False,
            payload={},
            error=str(exception),
            produced_at=time.monotonic(),
        )

    response_queue.put(response)


def _process_assistant_control_command(
    command: object,
    child_runtime: AssistantChildRuntime,
) -> None:
    """处理父进程发来的控制命令。

    控制命令不是普通请求，而是像“取消当前生成”“预热前台模型”这种高优先级操作。
    本方法根据命令类型，交给子进程运行时的对应控制端口执行。

    Args:
        command: 具体的控制命令，例如取消生成或预热。
        child_runtime: 子进程里真正跑助手逻辑的运行时。
    """

    if not isinstance(
        command,
        (
            CancelCurrentGenerationCommand,  # 取消当前正在生成的回复
            CancelSupersededSystemNoticeCommand,  # 取消被新事实取代的独立 System Notice
            CancelTimedReminderNoticeCommand,  # 取消用户已关闭或打断的 Reminder Notice
            WarmupForegroundCommand,  # 预热foregroud
            CancelActivityCommand, # 取消某个后台 Activity
            BackgroundAgentInteractionAnswerCommand,
            BackgroundAgentInteractionSubmissionRejectedCommand,
            TaskMemoryLookupResultCommand,
            ActivityCancellationResultCommand,
            TimedReminderCommandResultCommand,
            RuntimeMemoryCompactionPlanCommand,
            SelfReflectionToolCommitDecisionCommand,
        ),
    ):
        return

    # generation 与 Activity 取消都由常驻服务路由到对应的权威状态机。
    if isinstance(
        command,
        (
            CancelCurrentGenerationCommand,
            CancelSupersededSystemNoticeCommand,
            CancelTimedReminderNoticeCommand,
            CancelActivityCommand,
            BackgroundAgentInteractionAnswerCommand,
            BackgroundAgentInteractionSubmissionRejectedCommand,
            TaskMemoryLookupResultCommand,
            ActivityCancellationResultCommand,
            TimedReminderCommandResultCommand,
            RuntimeMemoryCompactionPlanCommand,
            SelfReflectionToolCommitDecisionCommand,
        ),
    ):
        child_runtime.handle_control_command(command)
        return

    try:
        child_runtime.warmup_foreground()  # 预热foreground
    except Exception:
        # warmup 是首响优化旁路动作，失败不能影响 ASR 或真实 LLM 请求。
        logger.exception("Foreground LLM stale warmup failed reason=%s", command.reason)


def _process_assistant_request_command(
    request: object,
    *,
    command_queue: Any,
    child_runtime: AssistantChildRuntime,
    pending_commands: Deque[AssistantProcessCommand] | None = None,
) -> None:
    """处理助手请求命令，并在两个输出域内分别保持 latest-wins。

    Args:
        request: 当前准备启动的助手请求。
        command_queue: 用于继续读取同批积压命令的跨进程队列。
        child_runtime: 启动请求并记录覆盖关系的助手子进程运行时。
        pending_commands: 已从队列读出但尚未处理的本地命令缓冲。
    """

    if not isinstance(request, AssistantRequest):
        return

    requests: list[AssistantRequest] = [request]
    latest_request_by_lane: dict[str, AssistantRequest] = {}
    superseded_requests: list[AssistantRequest] = []
    while True:
        try:
            next_command = command_queue.get_nowait()
        except Empty:
            break
        if not isinstance(next_command, AssistantProcessCommand):
            continue
        if next_command.kind != "assistant_request":
            if pending_commands is not None:
                pending_commands.append(next_command)
            break
        if not isinstance(next_command.payload, AssistantRequest):
            continue
        requests.append(next_command.payload)

    for candidate in requests:
        lane = (
            "auxiliary"
            if candidate.source == "self_reflection_interruption_reply"
            else "primary"
        )
        previous = latest_request_by_lane.get(lane)
        if previous is not None:
            superseded_requests.append(previous)
        latest_request_by_lane[lane] = candidate

    for request in superseded_requests:
        logger.info(
            "旧请求被新请求替代，标记为已过期 request_id=%s source_turn_id=%s",
            request.request_id,
            request.source_turn_id,
        )
        child_runtime.mark_request_superseded(request)
    # Recovery 先打开 episode 辅助准入，随后同批辅助请求才能进入自己的 lane。
    for lane in ("primary", "auxiliary"):
        latest_request = latest_request_by_lane.get(lane)
        if latest_request is None:
            continue
        logger.info(
            "开始处理输出域最新请求 lane=%s request_id=%s source_turn_id=%s text_length=%s",
            lane,
            latest_request.request_id,
            latest_request.source_turn_id,
            len(latest_request.user_text),
        )
        child_runtime.start_request(latest_request)


def run_noop_assistant_process(
    stop_event: Any,
    bridge_work_event: Any,
    command_queue: Any,
    event_queue: Any,
    streamvox_management_response_queue: Any | None = None,
    runtime_ready_event: Any | None = None,
    config: Any | None = None,
) -> None:
    """运行空转助手子进程。

    作为测试用进程入口，只消费并丢弃请求、控制和管理队列中的消息。

    Args:
        stop_event: 控制子进程退出的跨进程事件。
        bridge_work_event: 与真实入口保持一致的 bridge 唤醒事件。
        command_queue: 接收所有父进程命令的跨进程队列。
        event_queue: 与真实入口保持一致的事件回流队列。
        streamvox_management_response_queue: 与真实入口保持一致的 StreamVox 管理响应队列。
        runtime_ready_event: 与真实入口保持一致的运行时就绪事件。
        config: 与真实入口保持一致的可选配置对象。
    """

    _bootstrap_assistant_process_runtime()

    # event_queue 是 Task 11 真实助手运行时的事件回流边界；当前 noop 进程只保留签名，不写入假事件。
    _ = event_queue
    _ = bridge_work_event
    # 管理队列在 noop 进程中只用于保持真实入口签名一致，不执行 StreamVox 管理动作。
    _ = streamvox_management_response_queue
    if runtime_ready_event is not None:
        runtime_ready_event.set()
    # config 只用于让测试入口和真实入口共享同一 Process args 协议，noop 不读取任何生产密钥。
    _ = config

    try:
        while not stop_event.is_set():
            command = command_queue.get()
            if isinstance(command, AssistantProcessCommand) and command.kind == "shutdown":
                break
    finally:
        shutdown_backend_logging()
