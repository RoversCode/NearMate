from __future__ import annotations

import base64
from typing import Any

from companion.foreground_tools.activity_correction.contracts import (
    CorrectionEvidence,
    CorrectionReservation,
)
from companion.foreground_tools.activity_correction.execution import (
    ActivityCorrectionToolExecutor,
)
from companion.foreground_tools.activity_correction.policy import (
    CorrectableActivityContext,
)
from companion.foreground_tools.activity_correction.service import (
    ActivityCorrectionService,
)
from companion.llm.foreground_models import ForegroundToolCall
from companion.materials.foreground_images import (
    ForegroundImageReference,
    FrozenVisualInput,
    VisualEvidence,
    load_foreground_visual_input,
)


class _RecordingExecutor:
    """记录纠正服务交给目标执行器的参数。"""

    def __init__(
        self,
        *,
        activity_ids: set[str] | None = None,
        evidence: CorrectionEvidence | None = None,
    ) -> None:
        """初始化可选的活跃 Activity 集合。

        Args:
            activity_ids: 当前执行器仍持有的 Activity 编号。
            evidence: 父 Activity 可继承的安全证据。
        """

        self.activity_ids = set(activity_ids or set())
        self.prepared: list[dict[str, Any]] = []
        self.cancelled: list[str] = []
        self.evidence = evidence or CorrectionEvidence()

    def is_activity_correctable(self, activity_id: str) -> bool:
        """测试中把显式登记的 Activity 视为仍可纠正。"""

        return activity_id in self.activity_ids

    def cancel_for_correction(self, activity_id: str, *, timeout: float = 5.0) -> str:
        """记录测试中的纠正取消请求。"""

        del timeout
        if activity_id not in self.activity_ids:
            return False
        self.cancelled.append(activity_id)
        return "cleaned"

    def reserve_correction(self, **kwargs: Any) -> CorrectionReservation:
        """记录纠正预留参数并返回准备成功事件。"""

        self.prepared.append(kwargs)
        return CorrectionReservation(
            accepted=True,
            activity_id="new-id",
            events=({"type": "prepared", "payload": {"activity_id": "new-id"}},),
        )

    def commit_reserved_correction(self, activity_id: str) -> bool:
        """测试替身接受 Foreground committed 标记。"""

        return activity_id == "new-id"

    def start_reserved_correction(self, activity_id: str) -> bool:
        """测试替身接受启动。"""

        return activity_id == "new-id"

    def rollback_reserved_correction(self, activity_id: str) -> bool:
        """测试替身接受回滚。"""

        return activity_id == "new-id"

    def fail_reserved_correction(self, activity_id: str, *, reason: str) -> bool:
        """测试替身接受失败收口。"""

        return activity_id == "new-id" and bool(reason)

    def correction_evidence(self, activity_id: str) -> CorrectionEvidence:
        """返回测试预先冻结的父 Activity 证据。"""

        del activity_id
        return self.evidence


def _correctable_context(source: str) -> tuple[CorrectableActivityContext, ...]:
    """构造当前交互会话可纠正的冻结 Activity 现场。"""

    return (
        CorrectableActivityContext(
            activity_id="old-id",
            source=source,  # type: ignore[arg-type]
            interaction_session_id="session-a",
            state="running",
            phase="reasoning",
            goal="错误目标",
            cognitive_tool_types=(
                ("lookup_current_fact",)
                if source == "cognitive"
                else ()
            ),
        ),
    )


def _executor(
    cognitive: _RecordingExecutor,
    background: _RecordingExecutor,
) -> ActivityCorrectionToolExecutor:
    """构造使用测试执行器的纠正工具入口。"""

    return ActivityCorrectionToolExecutor(
        correction_service=ActivityCorrectionService(
            cognitive_executor=cognitive,
            background_executor=background,
        ),
    )


def test_terminal_cognitive_memory_cannot_be_corrected_to_background_work() -> None:
    """终态任务必须走普通派生，纠正入口不能召回任务记忆。"""

    cognitive = _RecordingExecutor()
    background = _RecordingExecutor()
    result = _executor(cognitive, background).execute_background_correction(
        tool_call=ForegroundToolCall(
            tool_call_id="call-a",
            name="correct_background_work",
            arguments={
                "activity_id": "old-id",
                "goal": "生成纠正后的报告",
                "deliverable": True,
            },
        ),
        interaction_session_id="session-a",
        foreground_context={},
        correctable_activities=(),
        environment_snapshot=None,
        cancellation_event=None,
    )

    assert result["state"] == "rejected"
    assert result["reason"] == "correction_activity_not_found"
    assert cognitive.cancelled == []
    assert background.prepared == []


def test_terminal_background_memory_cannot_be_corrected_to_cognitive_activity() -> None:
    """历史后台任务不能借纠正入口创建新的 Cognitive Activity。"""

    cognitive = _RecordingExecutor()
    background = _RecordingExecutor()
    result = _executor(cognitive, background).execute_cognitive_correction(
        tool_call=ForegroundToolCall(
            tool_call_id="call-b",
            name="correct_cognitive_activity",
            arguments={
                "activity_id": "old-id",
                "correction_goal": "重新分析正确的问题",
            },
        ),
        interaction_session_id="session-a",
        current_user_text="刚才说错了，请重新分析",
        foreground_context={},
        correctable_activities=(),
        frozen_visual_input=FrozenVisualInput(),
        cancellation_event=None,
    )

    assert result["state"] == "rejected"
    assert result["reason"] == "correction_activity_not_found"
    assert cognitive.prepared == []
    assert background.cancelled == []


def test_non_correctable_active_snapshot_fails_closed() -> None:
    """冻结快照已经 finalizing 时，纠正允许列表必须为空。"""

    result = _executor(_RecordingExecutor(), _RecordingExecutor()).execute_background_correction(
        tool_call=ForegroundToolCall(
            tool_call_id="call-c",
            name="correct_background_work",
            arguments={
                "activity_id": "old-id",
                "goal": "不应启动",
                "deliverable": False,
            },
        ),
        interaction_session_id="session-a",
        foreground_context={},
        correctable_activities=(),
        environment_snapshot=None,
        cancellation_event=None,
    )

    assert result["state"] == "rejected"
    assert result["reason"] == "correction_activity_not_found"


def test_background_correction_reads_full_turn_material_attachments() -> None:
    """纠正轮必须读取带路径和身份的正式材料，不得从展示摘要反推。"""

    cognitive = _RecordingExecutor()
    background = _RecordingExecutor(activity_ids={"old-id"})
    result = _executor(cognitive, background).execute_background_correction(
        tool_call=ForegroundToolCall(
            tool_call_id="call-material",
            name="correct_background_work",
            arguments={
                "activity_id": "old-id",
                "goal": "分析新上传的销售报告",
                "deliverable": True,
            },
        ),
        interaction_session_id="session-a",
        foreground_context={
            "active_turn_material_context": {
                "attachments": [
                    {
                        "material_id": "material-123",
                        "display_name": "2026销售报告.pdf",
                            "kind": "dragged_file",
                            "media_class": "mixed_or_document",
                            "path": r"G:\materials\2026销售报告.pdf",
                            "node_kind": "file",
                        }
                ]
            },
            "turn_material_summary": {
                "items": [
                    {
                        "display_name": "2026销售报告.pdf",
                        "material_kind": "文档",
                    }
                ]
            },
        },
        correctable_activities=_correctable_context("background_agent"),
        environment_snapshot=None,
        cancellation_event=None,
    )

    assert result["state"] == "accepted"
    assert result["based_on_relationship"] == "activity_correction"
    materials = background.prepared[0]["materials"]
    assert len(materials) == 1
    assert materials[0].material_id == "material-123"
    assert materials[0].local_path == r"G:\materials\2026销售报告.pdf"


def test_background_to_cognitive_correction_is_rejected() -> None:
    """Background Activity 不能通过 Cognitive 纠正入口替换。"""

    cognitive = _RecordingExecutor()
    background = _RecordingExecutor(activity_ids={"old-id"})
    result = _executor(cognitive, background).execute_cognitive_correction(
        tool_call=ForegroundToolCall(
            tool_call_id="call-image",
            name="correct_cognitive_activity",
            arguments={
                "activity_id": "old-id",
                "correction_goal": "分析旧任务图片中的内容",
                "additional_cognitive_types": ["lookup_current_fact"],
            },
        ),
        interaction_session_id="session-a",
        current_user_text="旧目标错了，改为分析图片",
        foreground_context={},
        correctable_activities=_correctable_context("background_agent"),
        frozen_visual_input=FrozenVisualInput(),
        cancellation_event=None,
    )

    assert result["state"] == "rejected"
    assert cognitive.prepared == []
    assert background.cancelled == []


def test_cognitive_visual_correction_accepts_parent_encoded_image() -> None:
    """纠正复用父图片像素，并保留来源引用供终态后重新校验。"""

    encoded_image = (
        "data:image/png;base64,"
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
    )
    parent_material = ForegroundImageReference("parent", "旧图.png", "G:/missing/parent.png")
    current_material = ForegroundImageReference("current", "新图.png", "G:/missing/current.png")
    cognitive = _RecordingExecutor(
        activity_ids={"old-id"},
        evidence=CorrectionEvidence(
            images=(VisualEvidence("material", "旧图.png", encoded_image),),
            image_materials=(parent_material,),
            cognitive_tool_types=("lookup_current_fact",),
        ),
    )
    result = _executor(cognitive, _RecordingExecutor()).execute_cognitive_correction(
        tool_call=ForegroundToolCall(
            tool_call_id="call-parent-image",
            name="correct_cognitive_activity",
            arguments={
                "activity_id": "old-id",
                "correction_goal": "查看父任务中的正确图片",
            },
        ),
        interaction_session_id="session-a",
        current_user_text="不是刚才的目标，请改为看这张图",
        foreground_context={},
        correctable_activities=_correctable_context("cognitive"),
        frozen_visual_input=FrozenVisualInput(image_materials=(current_material,)),
        cancellation_event=None,
    )

    assert result["state"] == "accepted"
    assert result["based_on_relationship"] == "activity_correction"
    plan = cognitive.prepared[0]["plan"]
    assert plan.encoded_images == (encoded_image,)
    assert plan.images[0].label == "旧图.png"
    assert plan.image_materials == (current_material, parent_material)


def test_cognitive_correction_inherits_types_and_recomputes_capabilities() -> None:
    """纠正后的完整目标不复制旧目标，但能力和网页证据按合并结果重算。"""

    cognitive = _RecordingExecutor(
        activity_ids={"old-id"},
        evidence=CorrectionEvidence(
            source_urls=("https://example.com/parent",),
            cognitive_tool_types=("lookup_current_fact",),
        ),
    )
    context = (
        CorrectableActivityContext(
            activity_id="old-id",
            source="cognitive",
            interaction_session_id="session-a",
            state="running",
            phase="reasoning",
            goal="旧目标不应进入 B",
            cognitive_tool_types=("lookup_current_fact",),
        ),
    )

    result = _executor(cognitive, _RecordingExecutor()).execute_cognitive_correction(
        tool_call=ForegroundToolCall(
            tool_call_id="call-capabilities",
            name="correct_cognitive_activity",
            arguments={
                "activity_id": "old-id",
                "correction_goal": "只执行纠正后的完整目标",
                "additional_cognitive_types": ["analyze_deeply"],
                "source_urls": ["https://example.com/new"],
            },
        ),
        interaction_session_id="session-a",
        current_user_text="不是旧目标，是新的综合目标",
        foreground_context={},
        correctable_activities=context,
        frozen_visual_input=FrozenVisualInput(),
        cancellation_event=None,
    )

    assert result["state"] == "accepted"
    plan = cognitive.prepared[0]["plan"]
    assert plan.goal_text == "只执行纠正后的完整目标"
    assert plan.parent_task_context is None
    assert plan.effective_cognitive_tool_types == (
        "lookup_current_fact",
        "analyze_deeply",
    )
    assert plan.reasoning == "high"
    assert plan.web_search is True
    assert plan.web_extract is True
    assert plan.source_urls == (
        "https://example.com/parent",
        "https://example.com/new",
    )


def test_cognitive_correction_without_additional_types_keeps_parent_capability() -> None:
    """没有新增能力时，B 直接继承 A 的能力，不要求 Foreground 重复声明。"""

    cognitive = _RecordingExecutor(
        activity_ids={"old-id"},
        evidence=CorrectionEvidence(
            cognitive_tool_types=("lookup_current_fact",),
        ),
    )
    context = (
        CorrectableActivityContext(
            activity_id="old-id",
            source="cognitive",
            interaction_session_id="session-a",
            state="running",
            phase="reasoning",
            goal="查询 Chrome 当前版本",
            cognitive_tool_types=("lookup_current_fact",),
        ),
    )

    result = _executor(cognitive, _RecordingExecutor()).execute_cognitive_correction(
        tool_call=ForegroundToolCall(
            tool_call_id="call-inherit-only",
            name="correct_cognitive_activity",
            arguments={
                "activity_id": "old-id",
                "correction_goal": "查询 Edge 当前版本",
            },
        ),
        interaction_session_id="session-a",
        current_user_text="不是 Chrome，是 Edge",
        foreground_context={},
        correctable_activities=context,
        frozen_visual_input=FrozenVisualInput(),
        cancellation_event=None,
    )

    assert result["state"] == "accepted"
    plan = cognitive.prepared[0]["plan"]
    assert plan.goal_text == "查询 Edge 当前版本"
    assert plan.effective_cognitive_tool_types == ("lookup_current_fact",)
    assert plan.reasoning == "minimal"
    assert plan.web_search is True


def test_cognitive_correction_rejects_unauthorized_new_image_even_with_parent_image() -> None:
    """新路径越权时，父图片不能把本轮无效输入伪装成成功。"""

    cognitive = _RecordingExecutor(
        activity_ids={"old-id"},
        evidence=CorrectionEvidence(
            images=(VisualEvidence("material", "父图.png", "data:image/png;base64,parent"),),
            cognitive_tool_types=("lookup_current_fact",),
        ),
    )
    result = _executor(cognitive, _RecordingExecutor()).execute_cognitive_correction(
        tool_call=ForegroundToolCall(
            tool_call_id="call-unauthorized-image",
            name="correct_cognitive_activity",
            arguments={
                "activity_id": "old-id",
                "correction_goal": "重新确认图片内容",
                "image_paths": [r"G:\outside\not-allowed.png"],
            },
        ),
        interaction_session_id="session-a",
        current_user_text="刚才说错了，改看这张图",
        foreground_context={"turn_material_summary": {"items": []}},
        correctable_activities=_correctable_context("cognitive"),
        frozen_visual_input=FrozenVisualInput(),
        cancellation_event=None,
    )

    assert result["state"] == "rejected"
    assert result["reason"] == "invalid_correction_arguments"
    assert cognitive.prepared == []


def test_cognitive_correction_rejects_missing_current_scene_even_with_parent_image() -> None:
    """明确要求本轮现场但截图缺失时，父图片不能兜底。"""

    cognitive = _RecordingExecutor(
        activity_ids={"old-id"},
        evidence=CorrectionEvidence(
            images=(VisualEvidence("material", "父图.png", "data:image/png;base64,parent"),),
            cognitive_tool_types=("lookup_current_fact",),
        ),
    )
    result = _executor(cognitive, _RecordingExecutor()).execute_cognitive_correction(
        tool_call=ForegroundToolCall(
            tool_call_id="call-missing-scene",
            name="correct_cognitive_activity",
            arguments={
                "activity_id": "old-id",
                "correction_goal": "结合当前屏幕重新确认",
                "include_current_scene": True,
            },
        ),
        interaction_session_id="session-a",
        current_user_text="请结合当前画面改做",
        foreground_context={},
        correctable_activities=_correctable_context("cognitive"),
        frozen_visual_input=FrozenVisualInput(),
        cancellation_event=None,
    )

    assert result["state"] == "rejected"
    assert result["reason"] == "invalid_correction_arguments"
    assert cognitive.prepared == []


def test_cognitive_correction_rejects_any_invalid_image_url() -> None:
    """多个新图片 URL 只要有一个结构无效，整项纠正都拒绝。"""

    cognitive = _RecordingExecutor(
        activity_ids={"old-id"},
        evidence=CorrectionEvidence(cognitive_tool_types=("lookup_current_fact",)),
    )
    result = _executor(cognitive, _RecordingExecutor()).execute_cognitive_correction(
        tool_call=ForegroundToolCall(
            tool_call_id="call-invalid-url",
            name="correct_cognitive_activity",
            arguments={
                "activity_id": "old-id",
                "correction_goal": "检查两张图片",
                "image_urls": ["https://example.com/ok.png", "not-a-url"],
            },
        ),
        interaction_session_id="session-a",
        current_user_text="重新检查图片",
        foreground_context={},
        correctable_activities=_correctable_context("cognitive"),
        frozen_visual_input=FrozenVisualInput(),
        cancellation_event=None,
    )

    assert result["state"] == "rejected"
    assert result["reason"] == "invalid_correction_arguments"
    assert cognitive.prepared == []


def test_cognitive_correction_merges_parent_and_new_images(tmp_path: Any) -> None:
    """新图片全部可用时，B 同时携带父图片和本轮图片。"""

    new_path = tmp_path / "new.png"
    new_path.write_bytes(
        base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
        )
    )
    cognitive = _RecordingExecutor(
        activity_ids={"old-id"},
        evidence=CorrectionEvidence(
            images=(VisualEvidence("material", "父图.png", "data:image/png;base64,parent"),),
            cognitive_tool_types=("lookup_current_fact",),
        ),
    )
    result = _executor(cognitive, _RecordingExecutor()).execute_cognitive_correction(
        tool_call=ForegroundToolCall(
            tool_call_id="call-two-images",
            name="correct_cognitive_activity",
            arguments={
                "activity_id": "old-id",
                "correction_goal": "同时检查旧图和新图",
            },
        ),
        interaction_session_id="session-a",
        current_user_text="保留旧图并加入新图",
        foreground_context={
            "turn_material_summary": {"items": [{"image_path": str(new_path)}]},
        },
        correctable_activities=_correctable_context("cognitive"),
        frozen_visual_input=load_foreground_visual_input({"attachments": [{"kind": "dragged_file", "material_id": "new", "display_name": "new.png", "path": str(new_path)}]}, None),
        cancellation_event=None,
    )

    assert result["state"] == "accepted"
    plan = cognitive.prepared[0]["plan"]
    assert len(plan.images) == 2
    assert plan.images[0].label == "new.png"
    assert plan.images[1].label == "父图.png"
    assert plan.encoded_images[1] == "data:image/png;base64,parent"


def test_cognitive_to_background_correction_is_rejected() -> None:
    """Cognitive Activity 不能通过 Background 纠正入口替换。"""

    cognitive = _RecordingExecutor(activity_ids={"old-id"})
    background = _RecordingExecutor()
    result = _executor(cognitive, background).execute_background_correction(
        tool_call=ForegroundToolCall(
            tool_call_id="call-cross-source",
            name="correct_background_work",
            arguments={
                "activity_id": "old-id",
                "goal": "只核查当前事实",
                "deliverable": True,
            },
        ),
        interaction_session_id="session-a",
        foreground_context={},
        correctable_activities=_correctable_context("cognitive"),
        environment_snapshot=None,
        cancellation_event=None,
    )

    assert result["state"] == "rejected"
    assert background.prepared == []
    assert cognitive.cancelled == []
