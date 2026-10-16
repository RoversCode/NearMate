from __future__ import annotations

from threading import Event

from companion.foreground_tools.background_agent.execution import (
    BackgroundAgentToolExecutor,
)
from companion.foreground_tools.background_agent.submission_service import (
    BackgroundWorkSubmissionService,
)
from companion.llm.foreground_models import ForegroundToolCall


def test_submission_availability_uses_canonical_executor_snapshot() -> None:
    """Background 可用性必须来自提交边界，不能把 executor 存在误报成可接单。"""

    class OccupiedExecutor:
        """暴露当前不可接单的只读状态。"""

        def background_available(self) -> bool:
            """返回当前槽位不可用。"""

            return False

    assert BackgroundWorkSubmissionService(None).background_available() is False
    assert (
        BackgroundWorkSubmissionService(OccupiedExecutor()).background_available()
        is False
    )


class RejectingActivityExecutor:
    """返回指定 prepare 拒绝事实的测试执行器。"""

    def __init__(self, reason: str, safe_message: str = "") -> None:
        self.reason = reason
        self.safe_message = safe_message
        self.prepare_calls: list[dict[str, object]] = []

    def prepare(self, **kwargs: object) -> tuple[bool, list[dict[str, object]]]:
        """记录调用并返回拒绝事件。"""

        self.prepare_calls.append(kwargs)
        payload: dict[str, object] = {"reason": self.reason}
        if self.safe_message:
            payload["safe_message"] = self.safe_message
        return False, [{"type": "background_agent.rejected", "payload": payload}]


class AcceptingSubmissionBroker:
    """记录工具执行器交给权威回执 Broker 的冻结事实。"""

    def __init__(self) -> None:
        self.submissions: list[dict[str, object]] = []

    def submit_and_wait(self, **kwargs: object) -> dict[str, object]:
        """保存提交参数并模拟后台执行器已经真实接收。"""

        self.submissions.append(dict(kwargs))
        return {
            "state": "accepted",
            "request_id": kwargs["request_id"],
            "activity_id": kwargs["activity_id"],
            "outcome": kwargs["outcome"],
            "answers": kwargs["answers"],
            "safe_message": "本次回应已被当前工作接收。",
        }


class AcceptingActivityExecutor:
    """记录派生任务输入并模拟后台槽位接受任务。"""

    def __init__(self) -> None:
        """初始化 prepare 调用记录。"""

        self.prepare_calls: list[dict[str, object]] = []

    def prepare(self, **kwargs: object) -> tuple[bool, list[dict[str, object]]]:
        """保存执行事实并返回唯一 Activity 编号。"""

        self.prepare_calls.append(dict(kwargs))
        return True, [
            {
                "type": "background_agent.prepared",
                "payload": {"activity_id": "background-derived"},
            }
        ]


class RecordingTaskMemoryBroker:
    """返回固定父任务详情并记录内部召回用途。"""

    def __init__(self, result: dict[str, object]) -> None:
        """保存待返回的权威任务详情。"""

        self.result = result
        self.lookups: list[dict[str, object]] = []

    def lookup_and_wait(self, **kwargs: object) -> dict[str, object]:
        """记录精确编号和用途后返回详情副本。"""

        self.lookups.append(dict(kwargs))
        return dict(self.result)


class CancellingTaskMemoryBroker(RecordingTaskMemoryBroker):
    """父任务查询返回前取消当前 generation。"""

    def lookup_and_wait(self, **kwargs: object) -> dict[str, object]:
        """设置调用方传入的取消事件，再返回原任务详情。"""

        cancellation_event = kwargs.get("cancellation_event")
        assert isinstance(cancellation_event, Event)
        cancellation_event.set()
        return super().lookup_and_wait(**kwargs)


def test_unconfigured_prepare_maps_to_exact_console_guidance() -> None:
    """未配置拒绝必须保留 Console 设置入口，供二轮 Foreground 自然表达。"""

    executor = BackgroundAgentToolExecutor(
        RejectingActivityExecutor("background_agent_unconfigured")
    )

    result = executor.execute_start_background_work(
        execution_lane="ordinary",
        tool_call=_start_call(),
        interaction_session_id="session-a",
        foreground_context={},
        environment_snapshot=None,
        protocol_transaction_id="tool-unconfigured",
    )

    assert result == {
        "state": "rejected",
        "reason": "background_agent_unconfigured",
        "safe_message": (
            "后台工作尚未配置。请先打开 Console 的“后台工作”设置，填写 "
            "ANTHROPIC_BASE_URL，并配置 ANTHROPIC_API_KEY 或 "
            "ANTHROPIC_AUTH_TOKEN。"
        ),
    }


def test_slot_occupied_rejection_preserves_lane_safe_fact() -> None:
    """同通道占用必须使用唯一原因，并保留通道给出的安全说明。"""

    result = BackgroundAgentToolExecutor(
        RejectingActivityExecutor(
            "background_slot_occupied",
            safe_message="已有普通后台任务正在处理，当前无法开启新任务。",
        )
    ).execute_start_background_work(
        execution_lane="ordinary",
        tool_call=_start_call(),
        interaction_session_id="session-a",
        foreground_context={},
        environment_snapshot=None,
        protocol_transaction_id="tool-slot-occupied",
    )

    assert result == {
        "state": "already_running",
        "reason": "background_slot_occupied",
        "safe_message": "已有普通后台任务正在处理，当前无法开启新任务。",
    }


def test_question_answer_freezes_text_and_new_materials() -> None:
    """资料回答把原话、材料和上下文交给权威回执 Broker。"""

    broker = AcceptingSubmissionBroker()
    result = BackgroundAgentToolExecutor(
        None,
        broker,  # type: ignore[arg-type]
    ).execute_work_interaction(
        tool_call=ForegroundToolCall(
            tool_call_id="answer-a",
            name="respond_to_work_interaction",
            arguments={
                "request_id": "question-a",
                "outcome": "answer",
                "answers": {"输出格式？": "Markdown"},
            },
        ),
        current_user_text="用 Markdown，参考我刚传的文件。",
        foreground_context={
            "pending_work_interactions": [
                {
                    "request_id": "question-a",
                    "activity_id": "activity-a",
                    "kind": "question",
                }
            ],
            "active_turn_material_context": {
                "context_id": "context-a",
                "attachments": [
                    {
                        "material_id": "material-a",
                        "display_name": "补充.md",
                        "kind": "dragged_file",
                        "path": r"C:\materials\补充.md",
                        "node_kind": "file",
                        "media_class": "text",
                    }
                ]
            },
        },
        cancellation_event=None,
    )

    assert result["state"] == "accepted"
    assert broker.submissions[0]["user_text"] == "用 Markdown，参考我刚传的文件。"
    assert broker.submissions[0]["turn_material_context_id"] == "context-a"
    materials = broker.submissions[0]["materials"]
    assert isinstance(materials, tuple)
    assert [material.to_payload() for material in materials] == [
        {
            "material_id": "material-a",
            "display_name": "补充.md",
            "material_kind": "dragged_file",
            "media_class": "text",
            "local_path": r"C:\materials\补充.md",
            "node_kind": "file",
        }
    ]


def test_background_work_can_derive_from_cognitive_without_serializing_image_data() -> None:
    """跨来源派生保留旧结果，但不能把 Cognitive 图片编码塞进 Claude 文本 Prompt。"""

    activity_executor = AcceptingActivityExecutor()
    task_memory_broker = RecordingTaskMemoryBroker(
        {
            "state": "completed",
            "activity_id": "cognitive-parent",
            "source": "cognitive",
            "goal": "识别截图",
            "terminal_state": "completed",
            "outcome_brief": "截图中有一份表格。",
            "result_text": "表格里有三行数据。",
            "output_paths": [],
            "safe_interactions": [],
            "input_snapshot": {
                "source_user_text": "识别这张图",
                "encoded_images": ["data:image/png;base64,parent-image"],
            },
            "truncated": False,
        }
    )
    executor = BackgroundAgentToolExecutor(
        activity_executor,
        task_memory_lookup_broker=task_memory_broker,  # type: ignore[arg-type]
    )

    result = executor.execute_start_background_work(
        execution_lane="ordinary",
        tool_call=ForegroundToolCall(
            tool_call_id="work-derived",
            name="start_background_work",
            arguments={
                "goal": "把旧表格整理成 Markdown",
                "deliverable": True,
                "based_on_activity_id": "cognitive-parent",
            },
        ),
        interaction_session_id="session-a",
        foreground_context={},
        environment_snapshot=None,
        protocol_transaction_id="tool-derived",
    )

    assert result["state"] == "accepted"
    assert result["based_on_activity_id"] == "cognitive-parent"
    assert task_memory_broker.lookups[0]["purpose"] == "task_derivation"
    parent_context = activity_executor.prepare_calls[0]["parent_task_context"]
    assert isinstance(parent_context, dict)
    assert parent_context["input_snapshot"] == {
        "source_user_text": "识别这张图",
        "encoded_image_count": 1,
        "encoded_images_available": False,
    }
    assert "parent-image" not in str(parent_context)


def test_background_work_rejects_missing_parent_before_prepare() -> None:
    """父任务详情不可用时，不得占后台槽位或创建任务目录。"""

    activity_executor = AcceptingActivityExecutor()
    executor = BackgroundAgentToolExecutor(
        activity_executor,
        task_memory_lookup_broker=RecordingTaskMemoryBroker(
            {
                "state": "not_available",
                "reason": "parent_task_not_available",
                "safe_message": "旧任务详情已经不可用。",
            }
        ),  # type: ignore[arg-type]
    )

    result = executor.execute_start_background_work(
        execution_lane="ordinary",
        tool_call=ForegroundToolCall(
            tool_call_id="work-missing",
            name="start_background_work",
            arguments={
                "goal": "继续旧任务",
                "deliverable": True,
                "based_on_activity_id": "missing-parent",
            },
        ),
        interaction_session_id="session-a",
        foreground_context={},
        environment_snapshot=None,
        protocol_transaction_id="tool-missing-parent",
    )

    assert result == {
        "state": "rejected",
        "reason": "parent_task_not_available",
        "safe_message": "旧任务详情已经不可用。",
    }
    assert activity_executor.prepare_calls == []


def test_background_derivation_cancelled_after_lookup_never_prepares() -> None:
    """父任务查询期间 generation 被取消时，不得继续创建后台 Activity。"""

    activity_executor = AcceptingActivityExecutor()
    cancellation_event = Event()
    executor = BackgroundAgentToolExecutor(
        activity_executor,
        task_memory_lookup_broker=CancellingTaskMemoryBroker({
            "state": "completed",
            "activity_id": "old-task",
            "source": "cognitive",
            "goal": "旧任务",
            "terminal_state": "completed",
            "outcome_brief": "旧任务完成。",
            "result_text": "旧结果",
            "input_snapshot": {},
            "original_character_count": 3,
            "returned_character_count": 3,
            "truncated": False,
        }),  # type: ignore[arg-type]
    )

    result = executor.execute_start_background_work(
        execution_lane="ordinary",
        tool_call=ForegroundToolCall(
            tool_call_id="cancelled-derived",
            name="start_background_work",
            arguments={
                "goal": "继续旧任务",
                "deliverable": True,
                "based_on_activity_id": "old-task",
            },
        ),
        interaction_session_id="session-a",
        foreground_context={},
        environment_snapshot=None,
        protocol_transaction_id="tool-cancelled-derived",
        cancellation_event=cancellation_event,
    )

    assert result["state"] == "rejected"
    assert result["reason"] == "generation_cancelled"
    assert activity_executor.prepare_calls == []


def _start_call() -> ForegroundToolCall:
    """构造一条合同完整的后台工作调用。"""

    return ForegroundToolCall(
        tool_call_id="work-a",
        name="start_background_work",
        arguments={"goal": "整理录音", "deliverable": True},
    )
