from __future__ import annotations

import asyncio
from threading import Barrier, Thread

import pytest

from companion.background_agent.interaction_bridge import (
    AgentInteractionAnswer,
    InteractionBridge,
)


@pytest.mark.asyncio
async def test_interaction_bridge_waits_for_and_returns_runtime_answer() -> None:
    requests = []
    bridge = InteractionBridge(
        activity_id="activity-a",
        request_notifier=requests.append,
    )

    pending = asyncio.create_task(
        bridge.request(
            tool_name="Bash",
            input_data={"command": "ffmpeg -version"},
            action_summary="运行完成这项工作所需的命令",
        )
    )
    await asyncio.sleep(0)

    request = requests[0]
    assert request.action_summary == "运行完成这项工作所需的命令"
    accepted = bridge.answer_from_runtime_thread(
        AgentInteractionAnswer(
            request_id=request.request_id,
            outcome="allow",
            answers={},
        )
    )

    assert accepted is True
    assert (await pending).outcome == "allow"
    assert bridge.answer_from_runtime_thread(
        AgentInteractionAnswer(
            request_id=request.request_id,
            outcome="deny",
            answers={},
        )
    ) is False


@pytest.mark.asyncio
async def test_interaction_bridge_close_resolves_every_pending_request() -> None:
    requests = []
    bridge = InteractionBridge(activity_id="activity-a", request_notifier=requests.append)
    pending = asyncio.create_task(
        bridge.request(
            tool_name="AskUserQuestion",
            input_data={"questions": [{"question": "输出格式？"}]},
            questions=[{"question": "输出格式？"}],
        )
    )
    await asyncio.sleep(0)

    closed_request_ids = bridge.close("运行时关闭")
    assert bridge.answer_from_runtime_thread(
        AgentInteractionAnswer(
            request_id=requests[0].request_id,
            outcome="allow",
            answers={},
        )
    ) is False
    answer = await pending

    assert closed_request_ids == (requests[0].request_id,)
    assert answer.outcome == "cancel"
    assert answer.reason == "运行时关闭"


@pytest.mark.asyncio
async def test_answer_claim_prevents_close_from_cancelling_same_request() -> None:
    """回答先认领 Future 后，任务关闭不能再发布同一请求的取消事实。"""

    requests = []
    cancelled_request_ids: list[str] = []
    bridge = InteractionBridge(activity_id="activity-a", request_notifier=requests.append)
    pending = asyncio.create_task(
        bridge.request(
            tool_name="Bash",
            input_data={"command": "ffmpeg -version"},
            action_summary="运行命令",
        )
    )
    await asyncio.sleep(0)

    assert bridge.answer_from_runtime_thread(
        AgentInteractionAnswer(
            request_id=requests[0].request_id,
            outcome="allow",
            answers={},
        )
    ) is True
    assert bridge.close(
        before_resolve=lambda request_ids: cancelled_request_ids.extend(request_ids)
    ) == ()

    assert (await pending).outcome == "allow"
    assert cancelled_request_ids == []


@pytest.mark.asyncio
async def test_concurrent_answer_and_close_have_exactly_one_winner() -> None:
    """真实线程并发时，每条交互只能由回答或关闭其中一方收口。"""

    for _ in range(50):
        requests = []
        bridge = InteractionBridge(activity_id="activity-a", request_notifier=requests.append)
        pending = asyncio.create_task(
            bridge.request(
                tool_name="Bash",
                input_data={"command": "ffmpeg -version"},
                action_summary="运行命令",
            )
        )
        await asyncio.sleep(0)
        answer = AgentInteractionAnswer(
            request_id=requests[0].request_id,
            outcome="allow",
            answers={},
        )

        def run_race() -> tuple[bool, tuple[str, ...]]:
            """让回答和关闭在两个真实线程中同时争用同一条 Future。"""

            barrier = Barrier(3)
            answer_result: list[bool] = []
            close_result: list[tuple[str, ...]] = []

            def answer_request() -> None:
                barrier.wait()
                answer_result.append(bridge.answer_from_runtime_thread(answer))

            def close_bridge() -> None:
                barrier.wait()
                close_result.append(bridge.close())

            answer_thread = Thread(target=answer_request)
            close_thread = Thread(target=close_bridge)
            answer_thread.start()
            close_thread.start()
            barrier.wait()
            answer_thread.join()
            close_thread.join()
            return answer_result[0], close_result[0]

        answer_won, closed_request_ids = await asyncio.to_thread(run_race)
        resolved_answer = await pending

        if answer_won:
            assert closed_request_ids == ()
            assert resolved_answer.outcome == "allow"
        else:
            assert closed_request_ids == (answer.request_id,)
            assert resolved_answer.outcome == "cancel"


@pytest.mark.asyncio
async def test_before_resolve_failure_safely_cancels_claimed_request() -> None:
    """前置状态应用失败时仍要解决 Future，并用同一请求编号发布取消。"""

    requests = []
    applied_answers: list[AgentInteractionAnswer] = []
    bridge = InteractionBridge(
        activity_id="activity-a",
        request_notifier=requests.append,
        answer_applied_notifier=lambda _request, answer: applied_answers.append(answer),
    )
    pending = asyncio.create_task(
        bridge.request(
            tool_name="AskUserQuestion",
            input_data={"questions": [{"question": "输出格式？"}]},
            questions=[{"question": "输出格式？"}],
        )
    )
    await asyncio.sleep(0)

    def fail_before_resolve() -> None:
        """模拟材料扩展或取消事实冻结失败。"""

        raise RuntimeError("state application failed")

    assert bridge.answer_from_runtime_thread(
        AgentInteractionAnswer(
            request_id=requests[0].request_id,
            outcome="answer",
            answers={"输出格式？": "Opus"},
        ),
        before_resolve=fail_before_resolve,
    ) is True
    resolved_answer = await pending

    assert resolved_answer.request_id == requests[0].request_id
    assert resolved_answer.outcome == "cancel"
    assert resolved_answer.reason == "后台交互状态应用失败。"
    assert applied_answers == [resolved_answer]


@pytest.mark.asyncio
async def test_cancelled_future_after_answer_claim_still_closes_registry_once() -> None:
    """回答已认领请求后，即使等待方取消也必须发布唯一取消事实。"""

    class DeferredLoop:
        """暂存线程安全回调，允许测试先取消等待方再执行回调。"""

        def __init__(self) -> None:
            self.callbacks: list[object] = []

        def call_soon_threadsafe(self, callback: object) -> None:
            """保存回调，不让它立刻进入真实事件循环。"""

            self.callbacks.append(callback)

    requests = []
    applied_answers: list[AgentInteractionAnswer] = []
    bridge = InteractionBridge(
        activity_id="activity-a",
        request_notifier=requests.append,
        answer_applied_notifier=lambda _request, answer: applied_answers.append(answer),
    )
    pending = asyncio.create_task(
        bridge.request(
            tool_name="AskUserQuestion",
            input_data={"questions": [{"question": "还继续吗？"}]},
            questions=[{"question": "还继续吗？"}],
        )
    )
    await asyncio.sleep(0)
    deferred_loop = DeferredLoop()
    bridge._loop = deferred_loop  # type: ignore[assignment]

    assert bridge.answer_from_runtime_thread(
        AgentInteractionAnswer(
            request_id=requests[0].request_id,
            outcome="answer",
            answers={"还继续吗？": "继续"},
        )
    ) is True
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending

    callback = deferred_loop.callbacks.pop()
    assert callable(callback)
    callback()

    assert bridge.close() == ()
    assert len(applied_answers) == 1
    assert applied_answers[0].request_id == requests[0].request_id
    assert applied_answers[0].outcome == "cancel"


@pytest.mark.asyncio
async def test_answer_scheduling_failure_leaves_request_for_close() -> None:
    """事件循环拒绝调度时，回答不能偷走关闭方仍需收口的请求。"""

    class RejectingLoop:
        """模拟已经停止接收线程安全回调的事件循环。"""

        def call_soon_threadsafe(self, _callback: object) -> None:
            """用固定异常模拟事件循环关闭。"""

            raise RuntimeError("loop is closed")

    requests = []
    bridge = InteractionBridge(activity_id="activity-a", request_notifier=requests.append)
    pending = asyncio.create_task(
        bridge.request(
            tool_name="Bash",
            input_data={"command": "ffmpeg -version"},
            action_summary="运行命令",
        )
    )
    await asyncio.sleep(0)
    real_loop = asyncio.get_running_loop()
    bridge._loop = RejectingLoop()  # type: ignore[assignment]

    assert bridge.answer_from_runtime_thread(
        AgentInteractionAnswer(
            request_id=requests[0].request_id,
            outcome="allow",
            answers={},
        )
    ) is False

    bridge.bind_loop(real_loop)
    assert bridge.close() == (requests[0].request_id,)
    assert (await pending).outcome == "cancel"


@pytest.mark.asyncio
async def test_interaction_bridge_freezes_cancel_before_resuming_sdk_future() -> None:
    """取消事实必须先于 SDK Future 恢复，避免错误终态抢先读取旧状态。"""

    requests = []
    bridge = InteractionBridge(activity_id="activity-a", request_notifier=requests.append)
    pending = asyncio.create_task(
        bridge.request(
            tool_name="AskUserQuestion",
            input_data={"questions": [{"question": "还要继续吗？"}]},
            questions=[{"question": "还要继续吗？"}],
        )
    )
    await asyncio.sleep(0)
    order: list[str] = []

    assert bridge.answer_from_runtime_thread(
        AgentInteractionAnswer(
            request_id=requests[0].request_id,
            outcome="cancel",
            answers={},
        ),
        before_resolve=lambda: order.append("cancel_frozen"),
    ) is True
    await pending
    order.append("future_resumed")

    assert order == ["cancel_frozen", "future_resumed"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "questions",
    [
        [{"question": "问题" * 501}],
        [{"question": f"问题 {index}"} for index in range(11)],
        [{"question": "重复问题"}, {"question": "重复问题"}],
    ],
)
async def test_interaction_bridge_rejects_invalid_question_contracts(
    questions: list[dict[str, object]],
) -> None:
    """过长、过多或重复问题不能进入等待态，避免前后端看到不同的问题集合。"""

    requests = []
    bridge = InteractionBridge(activity_id="activity-a", request_notifier=requests.append)

    answer = await bridge.request(
        tool_name="AskUserQuestion",
        input_data={"questions": questions},
        questions=questions,
    )

    assert answer.outcome == "cancel"
    assert answer.reason == "后台资料问题格式无效。"
    assert requests == []
