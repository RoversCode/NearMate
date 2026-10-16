from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal


# 描述材料进入系统时的顶层采集来源类型。
MaterialKind = Literal[
    "clipboard_text",  # 来自剪贴板的纯文本内容。
    "clipboard_image",  # 来自剪贴板的图片内容。
    "selected_text",  # 来自用户当前选中的文本片段。
    "selected_image",  # 来自用户当前选中的图片内容。
    "selected_web",  # 来自用户当前选中的网页链接或网页对象。
    "dragged_file",  # 来自拖拽进入的单个文件。
    "dragged_folder",  # 来自拖拽进入的整个文件夹。
]

# 描述本地材料在准入阶段被拒绝时的稳定原因码。
MaterialRejectionReason = Literal[
    "unsupported_extension",  # 文件扩展名不在系统允许范围内。
    "missing_path",  # 路径不存在，无法继续读取。
    "metadata_unreadable",  # 文件元数据读取失败，无法确认基本信息。
    "symlink_not_allowed",  # 命中了符号链接，出于安全或一致性原因不接收。
    "not_regular_file",  # 路径不是普通文件，例如目录或特殊设备文件。
    "empty_file",  # 文件内容为空，没有可提交的有效载荷。
]

# 描述 Desktop 聚合准入问题时使用的外层分类。
MaterialAdmissionIssueCategory = Literal[
    "unsupported",  # 类型或格式不支持，因此直接过滤。
    "unreadable",  # 内容不可读、缺失或损坏，因此无法接纳。
    "duplicate",  # 与当前 staged 集合中的已有材料重复。
]

# 描述材料路由阶段用于能力判定的媒体大类。
MaterialMediaClass = Literal[
    "text",  # 纯文本材料，可走轻量文本链路。
    "mixed_or_document",  # 含图片、文档或混合内容，需要更通用的处理链路。
    "audio",  # 音频文件，只冻结路径，具体读取和处理交给后台智能体。
    "video",  # 视频文件，只冻结路径，具体读取和处理交给后台智能体。
]

# 描述正式材料在文件系统中的安全节点形状；它与采集来源 kind 相互独立。
MaterialNodeKind = Literal["file", "directory"]

# 描述材料预检流程对当前上下文给出的阶段状态。
MaterialPreflightStatus = Literal[
    "pending",  # 预检尚未完成，结果还不能使用。
    "ready",  # 预检已完成，当前材料可按既定规则继续处理。
    "over_limit",  # 预检确认超出限制，需要拦截或降级。
    "indeterminate",  # 预检因超时、遍历过深或中途取消，未能可靠判断是否可用，按不可用处理。
    "closed",  # 预检生命周期已结束，不再接受后续更新。
]


@dataclass(frozen=True)
class MaterialAdmissionIssue:
    """描述一项没有进入 staged 的提交材料。

    Args:
        submitted_index: 材料在本次提交数组中的零基序号。
        display_name: 面向用户的材料名称。
        category: Desktop 用于聚合反馈的稳定分类。
        reason: Backend 判定的稳定事实原因码。
    """

    submitted_index: int
    display_name: str
    category: MaterialAdmissionIssueCategory
    reason: str

    def to_payload(self) -> dict[str, object]:
        """转换为 Desktop 可校验的问题载荷。"""

        return {
            "submitted_index": self.submitted_index,
            "display_name": self.display_name,
            "category": self.category,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class StagedMaterialAdmission:
    """描述一批 staged 材料的真实接纳结果。

    Args:
        submitted_count: 调用方本次提交的顶层材料项数量。
        added: 去重和可读性检查后实际进入 staged 的材料。
        unsupported_count: 因格式不受支持而被过滤的顶层材料项数量。
        unreadable_count: 因缺失、损坏或内容为空而未接纳的数量。
        duplicate_count: 已存在于当前 staged 集合中的重复项数量。
        issues: 未进入 staged 的有序问题明细。
    """

    submitted_count: int
    added: list["MaterialAttachment"]
    unsupported_count: int
    unreadable_count: int
    duplicate_count: int
    issues: list[MaterialAdmissionIssue]

    def to_payload(self) -> dict[str, object]:
        """转换为 Desktop 可校验的批次统计载荷。"""

        return {
            "submitted_count": self.submitted_count,
            "added_count": len(self.added),
            "unsupported_count": self.unsupported_count,
            "unreadable_count": self.unreadable_count,
            "duplicate_count": self.duplicate_count,
            "issues": [item.to_payload() for item in self.issues],
        }


@dataclass(frozen=True)
class StagedMaterialRemoval:
    """描述一次成功的 staged 单项删除。

    Args:
        removed: Backend 已移除的权威材料引用。
        released_managed_asset_ids: Desktop 可以释放的受管资产编号。
    """

    removed: "MaterialAttachment"
    released_managed_asset_ids: list[str]


@dataclass(frozen=True)
class MaterialWorkloadProfile:
    """描述一组材料能否由后台工作读取。

    Args:
        task_result_material_eligible: 当前材料是否允许由后台工作读取。
        local_file_count: 聚合后的本地可读文件数。
        visual_unit_count: 聚合后的视觉单元数。
        text_byte_count: 文本材料的 UTF-8 或原文件字节数。
        local_total_bytes: 聚合后的原始本地文件字节数。
        contains_mixed_material: 是否包含图片、文档、文件夹或 URL。
        ignored_file_count: 聚合后的被忽略文件数。
        blocking_reasons: 阻止后台工作读取材料的稳定原因。
    """

    task_result_material_eligible: bool
    local_file_count: int
    visual_unit_count: int
    text_byte_count: int
    local_total_bytes: int
    contains_mixed_material: bool
    ignored_file_count: int
    blocking_reasons: tuple[str, ...]

    def to_payload(self) -> dict[str, object]:
        """转换为请求上下文可使用的公开载荷。"""

        return {
            "task_result_material_eligible": self.task_result_material_eligible,
            "local_file_count": self.local_file_count,
            "visual_unit_count": self.visual_unit_count,
            "text_byte_count": self.text_byte_count,
            "local_total_bytes": self.local_total_bytes,
            "contains_mixed_material": self.contains_mixed_material,
            "ignored_file_count": self.ignored_file_count,
            "blocking_reasons": list(self.blocking_reasons),
        }


def normalize_material_path(path: object) -> str:
    """生成跨进程一致的材料路径归一化键。

    主进程登记近期材料与子进程校验模型抄回的路径必须使用完全相同的归一化规则，
    否则同一文件会被判成两个不同条目。非法输入一律归一化为空串，由调用方按未命中处理。

    Args:
        path: 待归一化的路径对象，通常是字符串。

    Returns:
        消除分隔符与大小写差异后的路径键；非字符串或纯空白输入返回空串。
    """

    if not isinstance(path, str) or not path.strip():
        return ""
    return os.path.normcase(os.path.normpath(path.strip()))


@dataclass(frozen=True)
class MaterialAttachment:
    """描述用户显式提交的一项材料。

    Args:
        material_id: 材料稳定编号。
        turn_id: 材料最初进入的用户 turn 编号。
        kind: 材料类型。
        source: 材料采集来源。
        display_name: 面向用户的展示名称。
        path: 必填本地读取路径；它不表达删除权限。
        created_at: 材料创建时间。
        node_kind: 材料进入系统时确认的普通文件或目录类型。
        media_class: 仅根据材料入口确定的路由媒体类别。
        managed_asset_id: Desktop 创建项目副本后登记的受管资产编号。
    """

    material_id: str
    turn_id: str
    kind: MaterialKind
    source: str
    display_name: str
    path: str
    created_at: str
    node_kind: MaterialNodeKind
    media_class: MaterialMediaClass
    managed_asset_id: str | None = None

    def to_payload(self) -> dict[str, object]:
        """转换为 API 可使用的公开载荷。"""

        return {
            "material_id": self.material_id,
            "turn_id": self.turn_id,
            "kind": self.kind,
            "source": self.source,
            "display_name": self.display_name,
            "path": self.path,
            "created_at": self.created_at,
            "node_kind": self.node_kind,
            "media_class": self.media_class,
            "managed_asset_id": self.managed_asset_id,
        }


@dataclass(frozen=True)
class TurnMaterialContext:
    """描述一次用户 turn 冻结的材料上下文。

    Args:
        context_id: 材料上下文稳定编号，用于隔离并发 turn。
        turn_id: 用户输入轮次编号。
        created_at: 材料上下文创建时间。
        attachments: 本轮冻结的材料附件。
    """

    context_id: str
    turn_id: str
    created_at: str
    attachments: list[MaterialAttachment]
    preflight_status: MaterialPreflightStatus = "pending"
    preflight_profile: MaterialWorkloadProfile | None = None
    preflight_generation: int = 0
    preflight_failures: tuple[dict[str, str], ...] = ()

    def to_payload(self) -> dict[str, object]:
        """转换为 API 可使用的公开载荷。"""

        return {
            "context_id": self.context_id,
            "turn_id": self.turn_id,
            "created_at": self.created_at,
            "attachments": [item.to_payload() for item in self.attachments],
        }
