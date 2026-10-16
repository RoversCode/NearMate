from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends
from starlette.requests import HTTPConnection

from companion.config.realtime_audio import RealtimeAudioYamlConfig
from companion.activities.runtime.service import ActivityService
from companion.background_agent.interaction_registry import BackgroundAgentInteractionRegistry
from companion.background_agent.interaction_service import BackgroundAgentInteractionService
from companion.background_agent.usage_service import BackgroundAgentUsageService
from companion.core.assistant_process_supervisor import AssistantProcessSupervisor
from companion.core.application.container import ApplicationContainer
from companion.core.application.material_lifecycle import MaterialLifecycleService
from companion.core.application.status_broadcaster import ApplicationStatusBroadcaster
from companion.core.application.runtime_reconfiguration import RuntimeReconfigurationCoordinator
from companion.core.application.user_settings_reconfiguration import UserSettingsCoordinator
from companion.core.application.turn_admission import TurnAdmissionSequencer
from companion.core.broadcast import BroadcastHub
from companion.core.character_voice_service import CharacterVoiceService
from companion.core.tts_setup_service import TtsSetupService
from companion.core.realtime_input_runtime_owner import RealtimeInputRuntimeOwner
from companion.core.speaker_verification_service import SpeakerVerificationConsoleService
from companion.core.streamvox_example_console_service import StreamVoxExampleConsoleService
from companion.core.wake_response_console_service import WakeResponseConsoleService
from companion.realtime.task_result_delivery import TaskResultDeliveryCoordinator
from companion.realtime.assistant_output_health import AssistantOutputHealthLedger
from companion.timed_reminders.service import TimedReminderService


def get_application_container(connection: HTTPConnection) -> ApplicationContainer:
    """从当前 HTTP 或 WebSocket 连接读取应用容器。"""

    return connection.app.state.container

# 这个别名同时声明容器类型和 FastAPI 依赖来源，让各路由复用同一条取值规则。
ContainerDependency = Annotated[ApplicationContainer, Depends(get_application_container)]


@dataclass(frozen=True, slots=True)
class RealtimeApiDependencies:
    """只向实时交互路由提供收发输入和恢复控制快照需要的服务。

    Args:
        hub: 控制连接与业务事件广播中枢。
        application_status: 提供每次连接必须补发的初始化和 Bootstrap 状态。
        realtime_input_runtime_owner: 持有当前输入运行时及交互状态。
        assistant_process_supervisor: 管理助手进程与运行时请求。
        material_lifecycle: 管理本轮材料的准备与提交。
        turn_admission: 管理文本提交队列及首帧快照。
        livekit_session_service: 可选的实时媒体会话服务。
        activity_service: 提供活动任务的权威快照。
        timed_reminder_service: 提供当前运行期提醒快照。
        background_agent_interactions: 提供待答交互快照。
        task_result_delivery_coordinator: 提供已经交付的结果卡。
        livekit_media_session_controller: 管理实时媒体会话生命周期。
        assistant_output_health_ledger: 提供助手输出可用性快照。
    """

    hub: BroadcastHub
    application_status: ApplicationStatusBroadcaster
    realtime_input_runtime_owner: RealtimeInputRuntimeOwner
    assistant_process_supervisor: AssistantProcessSupervisor
    material_lifecycle: MaterialLifecycleService
    turn_admission: TurnAdmissionSequencer
    livekit_session_service: object | None
    activity_service: ActivityService
    timed_reminder_service: TimedReminderService
    background_agent_interactions: BackgroundAgentInteractionRegistry
    task_result_delivery_coordinator: TaskResultDeliveryCoordinator
    livekit_media_session_controller: object
    assistant_output_health_ledger: AssistantOutputHealthLedger


@dataclass(frozen=True, slots=True)
class MaterialsApiDependencies:
    """材料路由允许访问的应用依赖。"""

    hub: BroadcastHub
    material_lifecycle: MaterialLifecycleService


@dataclass(frozen=True, slots=True)
class SettingsApiDependencies:
    """通用设置路由允许访问的应用依赖。"""

    hub: BroadcastHub
    user_settings: UserSettingsCoordinator
    speaker_verification: SpeakerVerificationConsoleService


@dataclass(frozen=True, slots=True)
class BootstrapApiDependencies:
    """启动与 LLM 路由允许访问的应用依赖。"""

    hub: BroadcastHub
    runtime_reconfiguration: RuntimeReconfigurationCoordinator
    tts_setup: TtsSetupService
    application_status: ApplicationStatusBroadcaster


@dataclass(frozen=True, slots=True)
class CharacterCardsApiDependencies:
    """角色卡路由允许访问的应用依赖。"""

    hub: BroadcastHub
    realtime_input_runtime_owner: RealtimeInputRuntimeOwner
    realtime_audio_config: RealtimeAudioYamlConfig
    user_settings: UserSettingsCoordinator
    character_voice: CharacterVoiceService
    streamvox_examples: StreamVoxExampleConsoleService


@dataclass(frozen=True, slots=True)
class SpeakerVerificationApiDependencies:
    """说话人确认路由允许访问的应用依赖。"""

    hub: BroadcastHub
    realtime_input_runtime_owner: RealtimeInputRuntimeOwner
    user_settings: UserSettingsCoordinator
    speaker_verification: SpeakerVerificationConsoleService


@dataclass(frozen=True, slots=True)
class StreamVoxApiDependencies:
    """StreamVox 路由允许访问的应用依赖。"""

    hub: BroadcastHub
    assistant_process_supervisor: AssistantProcessSupervisor
    user_settings: UserSettingsCoordinator


@dataclass(frozen=True, slots=True)
class WakeResponseApiDependencies:
    """唤醒回复路由允许访问的应用依赖。"""

    hub: BroadcastHub
    wake_response: WakeResponseConsoleService


@dataclass(frozen=True, slots=True)
class RuntimeLogsApiDependencies:
    """本次运行日志路由允许访问的应用依赖。"""

    hub: BroadcastHub


@dataclass(frozen=True, slots=True)
class ActivitiesApiDependencies:
    """Activity 查询与取消路由允许访问的应用依赖。"""

    activity_service: ActivityService


@dataclass(frozen=True, slots=True)
class TimedRemindersApiDependencies:
    """定时提醒查询与写接口允许访问的应用依赖。"""

    timed_reminder_service: TimedReminderService


@dataclass(frozen=True, slots=True)
class BackgroundAgentApiDependencies:
    """后台智能体交互与配置路由允许访问的应用依赖。"""

    assistant_process_supervisor: AssistantProcessSupervisor
    background_agent_interactions: BackgroundAgentInteractionRegistry
    background_agent_interaction_service: BackgroundAgentInteractionService
    hub: BroadcastHub
    user_settings: UserSettingsCoordinator
    background_agent_usage_service: BackgroundAgentUsageService
    livekit_media_session_controller: object


@dataclass(frozen=True, slots=True)
class SkillLibraryApiDependencies:
    """技能库路由允许访问的应用依赖。

    Args:
        user_settings: 提供 Skill 查询和设置事务的应用服务。
    """

    user_settings: UserSettingsCoordinator


def get_realtime_dependencies(container: ContainerDependency) -> RealtimeApiDependencies:
    """从应用容器构造实时路由所需的完整依赖切面。

    Args:
        container: 当前 Backend 应用组合根。

    Returns:
        包含输入服务和全部控制首帧快照来源的窄化依赖。
    """

    return RealtimeApiDependencies(
        hub=container.hub,
        application_status=container.application_status,
        realtime_input_runtime_owner=container.realtime_input_runtime_owner,
        assistant_process_supervisor=container.assistant_process_supervisor,
        material_lifecycle=container.material_lifecycle,
        turn_admission=container.turn_admission,
        livekit_session_service=container.livekit_session_service,
        activity_service=container.activity_service,
        timed_reminder_service=container.timed_reminder_service,
        background_agent_interactions=container.background_agent_interactions,
        task_result_delivery_coordinator=container.task_result_delivery_coordinator,
        livekit_media_session_controller=container.livekit_media_session_controller,
        assistant_output_health_ledger=container.assistant_output_health_ledger,
    )


def get_materials_dependencies(container: ContainerDependency) -> MaterialsApiDependencies:
    """构造材料路由依赖切面。"""

    return MaterialsApiDependencies(
        hub=container.hub,
        material_lifecycle=container.material_lifecycle,
    )


def get_settings_dependencies(container: ContainerDependency) -> SettingsApiDependencies:
    """构造通用设置路由依赖切面。"""

    return SettingsApiDependencies(
        hub=container.hub,
        user_settings=container.user_settings,
        speaker_verification=container.speaker_verification,
    )


def get_bootstrap_dependencies(container: ContainerDependency) -> BootstrapApiDependencies:
    """构造启动与 LLM 路由依赖切面。"""

    return BootstrapApiDependencies(
        hub=container.hub,
        runtime_reconfiguration=container.runtime_reconfiguration,
        tts_setup=container.tts_setup,
        application_status=container.application_status,
    )


def get_character_cards_dependencies(
    container: ContainerDependency,
) -> CharacterCardsApiDependencies:
    """构造角色卡路由依赖切面。"""

    return CharacterCardsApiDependencies(
        hub=container.hub,
        realtime_input_runtime_owner=container.realtime_input_runtime_owner,
        realtime_audio_config=container.realtime_audio_config,
        user_settings=container.user_settings,
        character_voice=container.character_voice,
        streamvox_examples=container.streamvox_examples,
    )


def get_speaker_verification_dependencies(
    container: ContainerDependency,
) -> SpeakerVerificationApiDependencies:
    """构造说话人确认路由依赖切面。"""

    return SpeakerVerificationApiDependencies(
        hub=container.hub,
        realtime_input_runtime_owner=container.realtime_input_runtime_owner,
        user_settings=container.user_settings,
        speaker_verification=container.speaker_verification,
    )


def get_streamvox_dependencies(container: ContainerDependency) -> StreamVoxApiDependencies:
    """构造 StreamVox 路由依赖切面。"""

    return StreamVoxApiDependencies(
        hub=container.hub,
        assistant_process_supervisor=container.assistant_process_supervisor,
        user_settings=container.user_settings,
    )


def get_wake_response_dependencies(
    container: ContainerDependency,
) -> WakeResponseApiDependencies:
    """构造唤醒回复路由依赖切面。"""

    return WakeResponseApiDependencies(
        hub=container.hub,
        wake_response=container.wake_response,
    )


def get_runtime_logs_dependencies(
    container: ContainerDependency,
) -> RuntimeLogsApiDependencies:
    """构造本次运行日志路由依赖切面。"""

    return RuntimeLogsApiDependencies(hub=container.hub)


def get_activities_dependencies(
    container: ContainerDependency,
) -> ActivitiesApiDependencies:
    """构造 Activity 路由依赖切面。"""

    return ActivitiesApiDependencies(activity_service=container.activity_service)


def get_timed_reminders_dependencies(
    container: ContainerDependency,
) -> TimedRemindersApiDependencies:
    """构造定时提醒路由依赖切面。

    Args:
        container: 当前 Backend 应用组合根。

    Returns:
        只暴露定时提醒应用服务的依赖对象。
    """

    return TimedRemindersApiDependencies(
        timed_reminder_service=container.timed_reminder_service,
    )


def get_background_agent_dependencies(
    container: ContainerDependency,
) -> BackgroundAgentApiDependencies:
    """构造后台智能体交互路由依赖切面。"""

    return BackgroundAgentApiDependencies(
        assistant_process_supervisor=container.assistant_process_supervisor,
        background_agent_interactions=container.background_agent_interactions,
        background_agent_interaction_service=container.background_agent_interaction_service,
        hub=container.hub,
        user_settings=container.user_settings,
        background_agent_usage_service=container.background_agent_usage_service,
        livekit_media_session_controller=container.livekit_media_session_controller,
    )


def get_skill_library_dependencies(
    container: ContainerDependency,
) -> SkillLibraryApiDependencies:
    """构造技能库路由依赖切面。

    Args:
        container: 当前 Backend 应用组合根。

    Returns:
        只暴露技能库读取和设置事务所需协调器的窄化依赖。
    """

    return SkillLibraryApiDependencies(
        user_settings=container.user_settings,
    )


RealtimeDependency = Annotated[RealtimeApiDependencies, Depends(get_realtime_dependencies)]
MaterialsDependency = Annotated[MaterialsApiDependencies, Depends(get_materials_dependencies)]
SettingsDependency = Annotated[SettingsApiDependencies, Depends(get_settings_dependencies)]
BootstrapDependency = Annotated[BootstrapApiDependencies, Depends(get_bootstrap_dependencies)]
CharacterCardsDependency = Annotated[
    CharacterCardsApiDependencies,
    Depends(get_character_cards_dependencies),
]
SpeakerVerificationDependency = Annotated[
    SpeakerVerificationApiDependencies,
    Depends(get_speaker_verification_dependencies),
]
StreamVoxDependency = Annotated[StreamVoxApiDependencies, Depends(get_streamvox_dependencies)]
WakeResponseDependency = Annotated[
    WakeResponseApiDependencies,
    Depends(get_wake_response_dependencies),
]
RuntimeLogsDependency = Annotated[
    RuntimeLogsApiDependencies,
    Depends(get_runtime_logs_dependencies),
]
ActivitiesDependency = Annotated[
    ActivitiesApiDependencies,
    Depends(get_activities_dependencies),
]
TimedRemindersDependency = Annotated[
    TimedRemindersApiDependencies,
    Depends(get_timed_reminders_dependencies),
]
BackgroundAgentDependency = Annotated[
    BackgroundAgentApiDependencies,
    Depends(get_background_agent_dependencies),
]
SkillLibraryDependency = Annotated[
    SkillLibraryApiDependencies,
    Depends(get_skill_library_dependencies),
]
