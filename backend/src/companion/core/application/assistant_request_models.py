from __future__ import annotations

from dataclasses import dataclass
from typing import NotRequired, Protocol, TypedDict

from companion.activities.notice.models import NoticeSnapshot
from companion.character_cards.models import CharacterCardDefinition
from companion.environment.models import FrozenEnvironmentSnapshot
from companion.materials.foreground_images import FrozenVisualInput
from companion.materials.models import MaterialKind, MaterialMediaClass, MaterialNodeKind
from companion.runtime_memory.models import RuntimeMemorySnapshot
from companion.realtime.assistant_output_contracts import (
    ActivityProgressReference,
    AssistantHistoryPolicy,
)


class RecentMaterialContextItem(TypedDict):
    """描述 Foreground 当前可逐字选择的一项近期路径能力。

    Args:
        display_name: 给模型辨认用户指代使用的材料名称。
        material_kind: 材料进入系统时的采集来源类型。
        node_kind: 路径登记时的普通文件或目录类型。
        media_class: 材料入口确定的媒体大类。
        path: 只能逐字传给 ``material_paths`` 的本地路径。
    """

    display_name: str
    material_kind: MaterialKind
    node_kind: MaterialNodeKind
    media_class: MaterialMediaClass
    path: str


class ForegroundContext(TypedDict, total=False):
    """跨主进程与助手子进程传递的产品态势面板契约。"""

    active_activities: list[dict[str, object]]
    notice_batch: list[NoticeSnapshot]
    timed_reminders: list[dict[str, object]]
    timed_reminder_runtime_id: str
    timed_reminder_notice: NotRequired[dict[str, object]]
    environment_snapshot: NotRequired[dict[str, object] | None]
    active_turn_material_context: NotRequired[dict[str, object] | None]
    turn_material_summary: NotRequired[dict[str, object]]
    recent_materials: NotRequired[list[RecentMaterialContextItem]]
    material_workload_profile: NotRequired[dict[str, object]]
    has_base_material_inputs: NotRequired[bool]
    has_material_inputs: NotRequired[bool]
    foreground_text_available: NotRequired[bool]
    pending_work_interactions: NotRequired[list[dict[str, object]]]
    activity_progress_reports: NotRequired[list[dict[str, object]]]


@dataclass(frozen=True, slots=True)
class AssistantRequestContextSnapshot:
    """保存一次助手请求在主进程冻结的全部资料事实。

    Args:
        character_card: 当前请求使用的角色卡快照。
        user_call_name: 角色对用户的称呼。
        user_self_intro: 用户主动配置的自我简介。
        user_extra_notes: 用户主动配置的补充说明。
        turn_material_context_id: 当前轮明确绑定的材料上下文编号。
        environment_snapshot: 当前轮明确绑定的环境快照。
        frozen_visual_input: 请求准备阶段冻结的图片，恢复轮复用，不进入历史。
        foreground_context: 当前轮活动、材料、环境和提醒的前台上下文。
        history_policy: 本轮可见正文的主进程历史归档策略。
        activity_progress_references: 纯 Progress 请求关联的 Activity 冻结进度。
        derived_from_activity_ids: 本轮表达依赖的 Cognitive Result Activity 编号。
    """

    character_card: CharacterCardDefinition
    user_call_name: str
    user_self_intro: str
    user_extra_notes: str
    turn_material_context_id: str | None
    environment_snapshot: FrozenEnvironmentSnapshot | None
    foreground_context: ForegroundContext
    frozen_visual_input: FrozenVisualInput | None = None
    runtime_memory_snapshot: RuntimeMemorySnapshot = RuntimeMemorySnapshot()
    history_policy: AssistantHistoryPolicy = "stable_dialogue"
    activity_progress_references: tuple[ActivityProgressReference, ...] = ()
    derived_from_activity_ids: tuple[str, ...] = ()


@dataclass
class PreparedAssistantRequestContext:
    """保存冻结请求事实、请求事务和主进程内部的权限播放身份。"""

    snapshot: AssistantRequestContextSnapshot
    lease: AssistantRequestContextLease
    permission_interaction_request_ids: tuple[str, ...] = ()

    def commit_enqueued(self) -> bool:
        """确认请求已经成功进入 Bridge 或助手运行时队列。"""

        return self.lease.commit_enqueued()

    def rollback(self) -> bool:
        """回收请求构造或入队失败前预留的提醒。"""

        return self.lease.rollback()


class AssistantRequestContextLease(Protocol):
    """定义请求上下文内所有可提交资源共用的最小事务协议。"""

    def commit_enqueued(self) -> bool:
        """确认请求已经成功进入 Bridge。"""

        ...

    def rollback(self) -> bool:
        """回收请求入队前冻结的资源。"""

        ...


@dataclass(frozen=True, slots=True)
class CompositeAssistantRequestContextLease:
    """让调用方一次提交或回滚同请求内的多个领域租约。

    Args:
        leases: Activity Notice、Timed Reminder 等同请求事务。
    """

    leases: tuple[AssistantRequestContextLease, ...]

    def commit_enqueued(self) -> bool:
        """提交全部租约，并在任一异常时继续保留真实异常。

        Returns:
            所有租约都首次成功提交时返回真。
        """

        results = [lease.commit_enqueued() for lease in self.leases]
        return all(results)

    def rollback(self) -> bool:
        """回滚全部租约。

        Returns:
            所有租约都首次成功回滚时返回真。
        """

        results = [lease.rollback() for lease in self.leases]
        return all(results)


class AssistantRequestContextPreparerProtocol(Protocol):
    """定义实时输入和控制面共同使用的请求准备端口。"""

    def prepare(
        self,
        *,
        request_id: str,
        request_source: str,
        turn_material_context_id: str | None = None,
        environment_snapshot_id: str | None = None,
        source_turn_id: str | None = None,
    ) -> PreparedAssistantRequestContext:
        """冻结一次请求的资料并返回待提交事务。"""

        ...

    def prepare_final_asr_request_context(
        self,
        *,
        request_id: str,
        source_turn_id: str,
    ) -> PreparedAssistantRequestContext:
        """激活 Final ASR 材料后冻结请求资料。"""

        ...

    def prepare_system_output_snapshot(self) -> AssistantRequestContextSnapshot:
        """冻结不领取 Notice 的当前角色系统输出上下文。"""

        ...

    def prepare_timed_reminder_notice_snapshot(
        self,
        batch: object,
    ) -> AssistantRequestContextSnapshot:
        """冻结独立 Reminder Notice 的最小请求事实。"""

        ...
