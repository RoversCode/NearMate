"""定义后台执行内核使用的 NearMate 领域契约。

本模块故意不导入 Claude Agent SDK。SDK 的消息格式属于基础设施，不能直接成为
Activity、材料或 Foreground 的公共接口。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from companion.background_agent.settings import AgentPermissionMode
from companion.background_agent.skill_models import SkillExecutionSnapshot
from companion.materials.models import (
    MaterialKind,
    MaterialMediaClass,
    MaterialNodeKind,
)


AgentEventType = Literal[
    "started",
    "stage_changed",
    "turn_output",
    "work_plan_changed",
    "interaction_requested",
    "interaction_resolved",
    "permission_granted",
    "completed",
    "failed",
    "cancelled",
]
AgentInteractionKind = Literal["permission", "question"]
AgentTaskMode = Literal["new_task", "task_derivation", "activity_correction"]
BackgroundExecutionLane = Literal["ordinary", "recovery"]


@dataclass(frozen=True, slots=True)
class BackgroundLaneAvailability:
    """保存一条 Background 执行通道的无内容可用性。

    Args:
        can_accept_submission: 当前通道是否能取得新的 prepared reservation。
        busy: prepared、active、finalizing 或 handoff 是否占用该通道。
        runner_available: 通道 Runner 是否仍能接管任务。
        configured: 当前后台配置是否完整可读。
        shutdown: 通道是否已经停止接单。
    """

    can_accept_submission: bool
    busy: bool
    runner_available: bool
    configured: bool
    shutdown: bool


@dataclass(frozen=True, slots=True)
class BackgroundWorkAvailability:
    """保存普通与恢复两条固定通道的 canonical 可用性。

    Args:
        ordinary: 普通 Foreground 派单使用的单槽状态。
        recovery: Self-Reflection 恢复专用单槽状态。
        active_count: 当前已经运行或 finalizing 的 Background 数量。
        cua_available: 当前全局 CUA Capability Slot 是否空闲。
    """

    ordinary: BackgroundLaneAvailability
    recovery: BackgroundLaneAvailability
    active_count: int
    cua_available: bool = False

    def to_payload(self) -> dict[str, object]:
        """转换成 Foreground Prompt 可安全读取的无内容事实。

        Returns:
            普通准入、恢复占用、活动数量和无队列政策。
        """

        return {
            "ordinary_can_accept": self.ordinary.can_accept_submission,
            "ordinary_busy": self.ordinary.busy,
            "recovery_busy": self.recovery.busy,
            "active_count": self.active_count,
            "cua_available": self.cua_available,
            "queue_supported": False,
        }


AGENT_COMPLETION_OUTPUT_FORMAT: dict[str, object] = {
    "type": "json_schema",
    "schema": {
        "type": "object",
        "properties": {
            "display_content": {
                "type": "string",
                "minLength": 1,
                "description": "给用户查看的完整成果正文。",
            },
            "delivery_brief": {
                "type": "string",
                "minLength": 1,
                "description": "一至两句中立成果事实，不使用角色口吻。",
            },
        },
        "required": ["display_content", "delivery_brief"],
        "additionalProperties": False,
    },
}


@dataclass(frozen=True, slots=True)
class AgentCompletionPayload:
    """保存由后台智能体生成、经过严格校验的终态语义。

    Args:
        display_content: 结果卡展示的完整成果正文。
        delivery_brief: 供 Foreground 自然转述的一至两句中立成果事实。
    """

    display_content: str
    delivery_brief: str

    @classmethod
    def from_structured_output(cls, value: object) -> AgentCompletionPayload:
        """把 SDK 结构化终态转换成稳定领域对象。

        Args:
            value: `ResultMessage.structured_output` 返回的原始对象。

        Returns:
            去除字段首尾空白后的后台智能体终态语义。

        Raises:
            ValueError: 结构不是严格双字段对象，或任一内容为空。
        """

        if not isinstance(value, dict):
            raise ValueError("后台智能体结构化结果必须是对象")
        expected_fields = {"display_content", "delivery_brief"}
        if set(value) != expected_fields:
            raise ValueError("后台智能体结构化结果字段不完整")
        display_content = value.get("display_content")
        delivery_brief = value.get("delivery_brief")
        if not isinstance(display_content, str) or not display_content.strip():
            raise ValueError("后台智能体完整成果不能为空")
        if not isinstance(delivery_brief, str) or not delivery_brief.strip():
            raise ValueError("后台智能体交付摘要不能为空")
        return cls(
            display_content=display_content.strip(),
            delivery_brief=delivery_brief.strip(),
        )


@dataclass(frozen=True, slots=True)
class AgentMaterial:
    """描述一份已经由 NearMate 准备好的材料。

    Args:
        material_id: 材料在 NearMate 中的稳定编号。
        display_name: 给智能体看的材料名称。
        material_kind: 材料进入系统时的采集来源类型。
        media_class: 材料准入阶段冻结的路由媒体类别。
        local_path: 受管材料副本的绝对路径；没有本地文件时为空。
        node_kind: 材料授权时冻结的普通文件或目录类型。
    """

    material_id: str
    display_name: str
    material_kind: MaterialKind
    media_class: MaterialMediaClass
    local_path: str | None
    node_kind: MaterialNodeKind

    def to_payload(self) -> dict[str, object]:
        """转换为跨进程内部事件使用的材料白名单。"""

        return {
            "material_id": self.material_id,
            "display_name": self.display_name,
            "material_kind": self.material_kind,
            "media_class": self.media_class,
            "local_path": self.local_path,
            "node_kind": self.node_kind,
        }

    @classmethod
    def from_payload(cls, value: object) -> AgentMaterial:
        """从跨进程内部事件还原一份材料。

        Args:
            value: 尚未信任的内部事件字段。

        Returns:
            字段完整且已经裁剪的材料对象。

        Raises:
            ValueError: 身份、名称、类型或路径字段不合法。
        """

        if not isinstance(value, dict):
            raise ValueError("材料载荷必须是对象")
        material_id = value.get("material_id")
        display_name = value.get("display_name")
        material_kind = value.get("material_kind")
        media_class = value.get("media_class")
        local_path = value.get("local_path")
        node_kind = value.get("node_kind")
        if not all(
            isinstance(item, str) and item.strip()
            for item in (material_id, display_name)
        ):
            raise ValueError("材料载荷缺少必填文本")
        if material_kind not in {
            "clipboard_text",
            "clipboard_image",
            "selected_text",
            "selected_image",
            "selected_web",
            "dragged_file",
            "dragged_folder",
        }:
            raise ValueError("材料来源类型不合法")
        if media_class not in {"text", "mixed_or_document", "audio", "video"}:
            raise ValueError("材料媒体类别不合法")
        if local_path is not None and not isinstance(local_path, str):
            raise ValueError("材料路径必须是文字或空值")
        if node_kind not in {"file", "directory"}:
            raise ValueError("材料节点类型不合法")
        return cls(
            material_id=material_id.strip(),
            display_name=display_name.strip(),
            material_kind=material_kind,
            media_class=media_class,
            local_path=local_path.strip() if isinstance(local_path, str) and local_path.strip() else None,
            node_kind=node_kind,
        )


@dataclass(frozen=True, slots=True)
class AgentTask:
    """冻结一次后台工作的全部执行输入。

    Args:
        activity_id: NearMate Activity 编号。
        agent_session_id: 可复用的产品级 AgentSession 编号；首次任务为空。
        interaction_session_id: 触发任务的 Foreground 交互会话编号。
        goal: 用户希望完成的任务目标。
        deliverable: 是否需要登记并展示最终交付结果。
        requirements: Foreground 补充的成果要求。
        turn_materials: 本 Turn 新增、需要显式交给智能体的材料。
        material_catalog: 当前 Activity 可追溯和授权的完整材料目录。
        current_scene_path: 本轮冻结截图的绝对路径；任务不需要现场时为空。
        based_on_activity_id: 可选父任务编号。
        parent_task_context: 旧任务的目标、终态、结果、输入和安全交互事实。
        task_mode: 当前任务是新任务、终态派生还是活跃纠正。
        correction_instruction: 纠正任务必须遵守的文件清理边界。
        workspace_root: Agent 的工作目录绝对路径。
        agent_config_dir: 当前任务或纠正链独占的 Claude 私有配置目录。
        task_output_dir: 本任务专属的交付目录。
        permission_mode: 默认审批模式或显式全权限模式。
        cua_capability_granted: 启动前原子裁决的 CUA Grant；整个 Activity 生命周期内不变。
        skill_snapshot: 本任务实际可以加载的产品 Skill 不可变快照。
        expected_sdk_session_id: Background Correction 必须恢复的 Claude SDK 会话编号。
    """

    activity_id: str
    interaction_session_id: str
    goal: str
    deliverable: bool
    requirements: str
    turn_materials: tuple[AgentMaterial, ...]
    material_catalog: tuple[AgentMaterial, ...]
    workspace_root: str
    agent_config_dir: str
    task_output_dir: str
    permission_mode: AgentPermissionMode
    cua_capability_granted: bool = False
    agent_session_id: str | None = None
    expected_sdk_session_id: str | None = None
    current_scene_path: str | None = None
    based_on_activity_id: str | None = None
    parent_task_context: dict[str, object] | None = None
    task_mode: AgentTaskMode = "new_task"
    correction_instruction: str | None = None
    skill_snapshot: SkillExecutionSnapshot = field(default_factory=SkillExecutionSnapshot)

@dataclass(frozen=True, slots=True)
class AgentEvent:
    """表示后台智能体向 Activity 主进程发送的一条领域事件。

    Args:
        event_type: NearMate 后台智能体事件类型。
        activity_id: 事件所属 Activity 编号。
        interaction_session_id: 事件所属 Foreground 交互会话编号。
        payload: JSON 友好的事件载荷。
    """

    event_type: AgentEventType
    activity_id: str
    interaction_session_id: str
    payload: dict[str, object] = field(default_factory=dict)

    def to_runtime_event(self) -> dict[str, object]:
        """返回 RuntimeEvent 队列可直接使用的来源载荷。

        Returns:
            包含活动身份和事件字段的字典。
        """

        return {
            "activity_id": self.activity_id,
            "interaction_session_id": self.interaction_session_id,
            **self.payload,
        }
