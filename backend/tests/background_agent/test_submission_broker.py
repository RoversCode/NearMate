from __future__ import annotations

from threading import Event, Thread

from companion.background_agent.contracts import AgentMaterial
from companion.foreground_tools.background_agent.submission_broker import (
    WorkInteractionSubmissionBroker,
)
from companion.realtime.assistant_events import (
    BackgroundAgentInteractionAnswerCommand,
)


def test_submission_waits_until_background_executor_accepts_answer() -> None:
    """主进程命令到达后，只有后台执行器真实接收才允许返回 accepted。"""

    broker = WorkInteractionSubmissionBroker(
        submission_id_factory=lambda: "submission-a",
    )
    result_holder: list[dict[str, object]] = []
    worker = Thread(
        target=lambda: result_holder.append(
            broker.submit_and_wait(
                request_id="request-a",
                activity_id="activity-a",
                outcome="answer",
                answers={"输出格式？": "MP3"},
                user_text="用 MP3",
                materials=(
                    AgentMaterial(
                        material_id="material-a",
                        display_name="补充.wav",
                        material_kind="dragged_file",
                        media_class="audio",
                        local_path="C:/补充.wav",
                        node_kind="file",
                    ),
                ),
                turn_material_context_id="context-a",
                cancellation_event=Event(),
            )
        ),
    )

    worker.start()
    assert _wait_for_event(broker, "llm.foreground.work_interaction_submission_requested")
    assert broker.apply_answer_command(
        BackgroundAgentInteractionAnswerCommand(
            submission_id="submission-a",
            request_id="request-a",
            outcome="answer",
            answers={"输出格式？": "MP3"},
        ),
        submit_answer=lambda _command: True,
    )
    worker.join(timeout=1.0)

    assert result_holder == [
        {
            "state": "accepted",
            "request_id": "request-a",
            "activity_id": "activity-a",
            "outcome": "answer",
            "answers": {"输出格式？": "MP3"},
            "safe_message": "本次回应已被当前工作接收。",
        }
    ]
    completed = broker.drain_events()
    assert [event.type for event in completed] == [
        "llm.foreground.work_interaction_submission_completed"
    ]
    assert completed[0].payload["turn_material_context_id"] == "context-a"


def test_cancelled_submission_rejects_late_answer_command() -> None:
    """用户打断后 submission 立即失效，迟到命令不能再恢复后台工作。"""

    broker = WorkInteractionSubmissionBroker(
        submission_id_factory=lambda: "submission-a",
    )
    cancellation_event = Event()
    result_holder: list[dict[str, object]] = []
    worker = Thread(
        target=lambda: result_holder.append(
            broker.submit_and_wait(
                request_id="request-a",
                activity_id="activity-a",
                outcome="allow",
                answers={},
                user_text="允许执行",
                materials=(),
                turn_material_context_id=None,
                cancellation_event=cancellation_event,
            )
        ),
    )

    worker.start()
    assert _wait_for_event(broker, "llm.foreground.work_interaction_submission_requested")
    cancellation_event.set()
    worker.join(timeout=1.0)
    submitted_commands: list[BackgroundAgentInteractionAnswerCommand] = []

    accepted = broker.apply_answer_command(
        BackgroundAgentInteractionAnswerCommand(
            submission_id="submission-a",
            request_id="request-a",
            outcome="allow",
        ),
        submit_answer=lambda command: submitted_commands.append(command) or True,
    )

    assert accepted is False
    assert submitted_commands == []
    assert result_holder[0]["reason"] == "submission_timeout"
    assert [event.type for event in broker.drain_events()] == [
        "llm.foreground.work_interaction_submission_abandoned"
    ]


def test_background_rejection_returns_unavailable_receipt() -> None:
    """后台执行器拒收时，角色只能得到 background_unavailable 事实。"""

    broker = WorkInteractionSubmissionBroker(
        submission_id_factory=lambda: "submission-a",
    )
    result_holder: list[dict[str, object]] = []
    worker = Thread(
        target=lambda: result_holder.append(
            broker.submit_and_wait(
                request_id="request-a",
                activity_id="activity-a",
                outcome="answer",
                answers={"格式？": "MP3"},
                user_text="MP3",
                materials=(),
                turn_material_context_id=None,
                cancellation_event=Event(),
            )
        ),
    )

    worker.start()
    assert _wait_for_event(broker, "llm.foreground.work_interaction_submission_requested")
    assert not broker.apply_answer_command(
        BackgroundAgentInteractionAnswerCommand(
            submission_id="submission-a",
            request_id="request-a",
            outcome="answer",
            answers={"格式？": "MP3"},
        ),
        submit_answer=lambda _command: False,
    )
    worker.join(timeout=1.0)

    assert result_holder[0]["state"] == "rejected"
    assert result_holder[0]["reason"] == "background_unavailable"
    assert [event.type for event in broker.drain_events()] == [
        "llm.foreground.work_interaction_submission_failed"
    ]


def test_submission_times_out_without_main_process_receipt() -> None:
    """主进程没有在时限内回执时，Broker 必须明确返回未提交事实。"""

    broker = WorkInteractionSubmissionBroker(
        timeout_seconds=0.02,
        submission_id_factory=lambda: "submission-timeout",
    )

    result = broker.submit_and_wait(
        request_id="request-timeout",
        activity_id="activity-timeout",
        outcome="answer",
        answers={"格式？": "MP3"},
        user_text="用 MP3",
        materials=(),
        turn_material_context_id=None,
        cancellation_event=Event(),
    )

    assert result == {
        "state": "rejected",
        "request_id": "request-timeout",
        "activity_id": "activity-timeout",
        "reason": "submission_timeout",
        "safe_message": "这次回应没有在时限内确认，后台未收到本次回应。",
    }
    assert [event.type for event in broker.drain_events()] == [
        "llm.foreground.work_interaction_submission_requested",
        "llm.foreground.work_interaction_submission_abandoned",
    ]
    submitted_commands: list[BackgroundAgentInteractionAnswerCommand] = []
    assert broker.apply_answer_command(
        BackgroundAgentInteractionAnswerCommand(
            submission_id="submission-timeout",
            request_id="request-timeout",
            outcome="answer",
            answers={"格式？": "MP3"},
        ),
        submit_answer=lambda command: submitted_commands.append(command) or True,
    ) is False
    assert submitted_commands == []


def _wait_for_event(
    broker: WorkInteractionSubmissionBroker,
    event_type: str,
) -> bool:
    """等待 Broker 产出指定事件，并把其它事件视为测试失败。"""

    for _ in range(100):
        events = broker.drain_events()
        if not events:
            Event().wait(0.01)
            continue
        assert [event.type for event in events] == [event_type]
        return True
    return False
