from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Iterator
from datetime import date
from pathlib import Path
from types import MethodType
from threading import Event
from typing import Any

import pytest

from companion.activities.foreground_snapshot import ForegroundActivitySnapshot
from companion.background_agent.execution_slot import ExecutionSlotSubmissionOutcome
from companion.character_cards.store import CharacterCardStore
from companion.foreground_tools.catalog.service import ForegroundToolService
from companion.foreground_tools.background_agent.execution import (
    BackgroundAgentToolExecutor,
)
from companion.foreground_tools.cognitive.schemas import cognitive_tool_schemas
from companion.llm.foreground_turn_processor import ForegroundTurnProcessor
from companion.llm.call_trace import ForegroundRequestTrace
from companion.llm.foreground_models import (
    ForegroundExecutionContext,
    ForegroundModelStreamChunk,
    ForegroundToolCall,
)
from companion.llm.output_policies import LlmProtocolLeakError
from companion.llm.request_intent import LlmInvocationIntent
from companion.prompt.builder import PromptBuilder
from companion.prompt.tool_followup_policy import VisibleTransitionKind
from companion.session.context import SessionContext
from backend.tests.foreground_processor_factory import (
    build_foreground_processor_for_test,
)


CARDS_DIR = Path(__file__).parents[2] / "data" / "character-cards"
CHARACTER_CARD = CharacterCardStore(CARDS_DIR).load_card("xiaolian")


def _execution_context() -> ForegroundExecutionContext:
    """构造工具续写测试使用的空执行期上下文。"""

    return ForegroundExecutionContext(
        available_tools=(),
        prompt_environment={},
    )


class RecordingClient:
    """记录工具结果后的 Foreground 续写请求。"""

    def __init__(self, chunks: list[ForegroundModelStreamChunk]) -> None:
        """初始化测试客户端。

        Args:
            chunks: 每次续写请求返回的固定流片段。
        """

        self.chunks = chunks
        self.calls: list[dict[str, object]] = []

    def stream_message(self, **kwargs: object) -> list[ForegroundModelStreamChunk]:
        """记录请求并返回固定流。

        Args:
            **kwargs: Foreground 续写请求参数。

        Returns:
            初始化时配置的固定流片段。
        """

        self.calls.append(dict(kwargs))
        return list(self.chunks)


def test_background_accepted_with_visible_text_skips_second_foreground_llm() -> None:
    """后台任务已接收且等待话已可见时不得再次生成同义等待话。"""

    client = RecordingClient([
        ForegroundModelStreamChunk(kind="text_delta", text="又说了一句等待话。"),
    ])
    agent = _build_agent(client)
    _install_accepting_background_executor(agent)
    agent._execute_cognitive_tool_calls = MethodType(  # type: ignore[method-assign]
        lambda _self, **_kwargs: ({
            "state": "accepted",
            "activity_id": "cognitive-a",
            "kind": "lookup_current_fact",
        },),
        agent,
    )

    events = list(_handle_calls(
        agent,
        tool_calls=[_tool_call("lookup-a", "lookup_current_fact")],
        visible_parts=["我去查一下。"],
    ))
    _accept_final(events)

    assert client.calls == []
    assert _final_text(events) == "我去查一下。"
    final_event = next(
        event
        for event in events
        if getattr(event, "type", "") == "assistant.text_final"
    )
    memory_commit = final_event.payload["memory_commit"]
    assert isinstance(memory_commit, dict)
    assert [message["role"] for message in memory_commit["messages"]] == [
        "assistant",
        "tool",
        "assistant",
    ]
    assert [message.role for message in agent._session_context.recent_messages()] == [
        "assistant",
        "tool",
        "assistant",
    ]
    assert agent._session_context.recent_messages()[-1].content == "我去查一下。"


def test_public_transition_with_strong_notice_uses_notice_only_followup() -> None:
    """等待句只覆盖接单事实，附着强 Notice 仍必须进入不含工具事务的二轮。"""

    client = RecordingClient([
        ForegroundModelStreamChunk(
            kind="text_delta",
            text="另外，会议纪要已经整理完成。",
        ),
    ])
    agent = _build_agent(client)
    agent._execute_cognitive_tool_calls = MethodType(  # type: ignore[method-assign]
        lambda _self, **_kwargs: ({
            "state": "accepted",
            "activity_id": "cognitive-a",
            "brief": "查询天气",
        },),
        agent,
    )

    events = list(_handle_calls(
        agent,
        tool_calls=[_tool_call("lookup-a", "lookup_current_fact")],
        visible_parts=["好，我查一下。"],
        prompt_environment={
            "notice_batch": [
                {
                    "intent": "work_result",
                    "goal": "整理会议纪要",
                    "result_context": "会议纪要已经整理完成。",
                }
            ]
        },
    ))
    _accept_final(events)

    assert len(client.calls) == 1
    followup_messages = client.calls[0]["messages"]
    assert isinstance(followup_messages, list)
    followup_payload = json.dumps(followup_messages, ensure_ascii=False)
    assert "把 notice 里的事说给他听" in followup_payload
    assert "整理会议纪要" in followup_payload
    assert "会议纪要已经整理完成" in followup_payload
    assert "查询天气已经开始" not in followup_payload
    assert followup_payload.count("测试请求") == 1
    assert not any(message.get("tool_calls") for message in followup_messages)
    assert _final_text(events) == "好，我查一下。另外，会议纪要已经整理完成。"


def test_start_failure_survives_public_transition() -> None:
    """提交后的启动失败必须纠正等待句形成的预期。"""

    class FailingActivityCommitExecutor(ActivityCommitExecutor):
        """模拟 prepare 成功但 after_commit 启动失败的执行器。"""

        def start(self, _activity_id: str) -> bool:
            """拒绝真正启动预留 Activity。

            Args:
                _activity_id: prepare 阶段分配的 Activity 编号。

            Returns:
                固定返回假，触发最终启动失败事实。
            """

            return False

    client = RecordingClient([
        ForegroundModelStreamChunk(kind="text_delta", text="这项查询没有启动。"),
    ])
    agent = _build_agent(client)
    agent._cognitive_activity_executor = FailingActivityCommitExecutor()
    agent._execute_cognitive_tool_calls = MethodType(  # type: ignore[method-assign]
        lambda _self, **_kwargs: ({
            "state": "accepted",
            "activity_id": "cognitive-a",
            "brief": "查询天气",
        },),
        agent,
    )

    events = list(_handle_calls(
        agent,
        tool_calls=[_tool_call("lookup-a", "lookup_current_fact")],
        visible_parts=["好，我查一下。"],
    ))
    _accept_final(events)

    followup_payload = json.dumps(client.calls[0]["messages"], ensure_ascii=False)
    assert "关于“查询天气”的处理没有开始" in followup_payload
    assert "启动结果无法确认" not in followup_payload
    assert _final_text(events) == "好，我查一下。这项查询没有启动。"


def test_background_accepted_without_visible_text_requests_waiting_reply() -> None:
    """后台任务已接收但尚未开口时仍由第二轮生成唯一等待回复。"""

    client = RecordingClient([
        ForegroundModelStreamChunk(kind="text_delta", text="我去查一下。"),
    ])
    agent = _build_agent(client)
    agent._execute_cognitive_tool_calls = MethodType(  # type: ignore[method-assign]
        lambda _self, **_kwargs: ({
            "state": "accepted",
            "activity_id": "cognitive-a",
            "kind": "lookup_current_fact",
        },),
        agent,
    )

    events = list(_handle_calls(
        agent,
        tool_calls=[
            _tool_call(
                "lookup-a",
                "lookup_current_fact",
                {"brief": "某个角色现在是什么定位"},
            )
        ],
        visible_parts=[],
    ))
    _accept_final(events)

    assert len(client.calls) == 1
    trace = client.calls[0]["trace"]
    assert trace.request_id == "request-a"
    assert trace.generation_id == "generation-a"
    assert trace.round == "tool_followup"
    assert trace.llm_call_id.startswith("llm-")
    assert client.calls[0]["tools"] == []
    assert client.calls[0]["intent"] == LlmInvocationIntent.text()
    followup_messages = client.calls[0]["messages"]
    assert isinstance(followup_messages, list)
    assert [message["role"] for message in followup_messages] == [
        "system",
        "system",
        "system",
        "system",
        "user",
        "assistant",
        "tool",
    ]
    rendered_followup = json.dumps(followup_messages, ensure_ascii=False)
    assert rendered_followup.count("测试请求") == 1
    assert "请只确认这项任务已经开始处理" not in rendered_followup
    assert "一两句话，说完就停" in rendered_followup
    assert "全都是你编的" in rendered_followup
    assert "发言合同" in rendered_followup
    assert "某个角色现在是什么定位" in rendered_followup
    assert "【系统事实】这件事已经真实开始，结果还没有出来。" in rendered_followup
    assert "cognitive-a" not in rendered_followup
    assert "旧聊天摘要哨兵" not in rendered_followup
    assert "动态环境哨兵" not in rendered_followup
    assert "材料正文哨兵" not in rendered_followup
    assert _final_text(events) == "我去查一下。"

    # 生成上下文被裁剪不影响最终原生工具事务的完整持久化。
    history = agent._session_context.recent_messages()
    assert "某个角色现在是什么定位" in json.dumps(
        history[0].to_llm_message(),
        ensure_ascii=False,
    )
    assert "cognitive-a" in history[1].content


def test_public_turn_followup_uses_frozen_visible_dialogue_and_current_question(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """工具二轮承接冻结的可见对话与当前原话，同时隔离内部上下文。"""

    class TwoRoundClient(RecordingClient):
        """首轮选择后台查询，第二轮返回等待反馈。"""

        def stream_message(self, **kwargs: object) -> list[ForegroundModelStreamChunk]:
            """按调用次数返回工具选择或自然语言结果。

            Args:
                **kwargs: 当前 LLM 请求参数。

            Returns:
                首轮工具调用或第二轮等待正文。
            """

            self.calls.append(dict(kwargs))
            if len(self.calls) == 1:
                return [
                    ForegroundModelStreamChunk(
                        kind="tool_calls",
                        tool_calls=[
                            _tool_call(
                                "lookup-a",
                                "lookup_current_fact",
                                {"brief": "核对演员胡一天的年龄和出生年份"},
                            )
                        ],
                    )
                ]
            return [
                ForegroundModelStreamChunk(
                    kind="text_delta",
                    text="他具体多大，我也得查准了再说，稍等会儿。",
                )
            ]

    class CognitiveToolService:
        """只向首轮暴露认知工具声明。"""

        def tool_schemas(self, **_: object) -> list[dict[str, object]]:
            """返回认知工具声明。"""

            return cognitive_tool_schemas()

        def execute(self, _name: str, _arguments: dict[str, object]) -> dict[str, object]:
            """后台认知工具不应进入 Inline 执行。"""

            raise AssertionError("认知工具不应进入 Inline 执行")

    client = TwoRoundClient([])
    agent = _build_agent(client)
    agent._prompt_builder = PromptBuilder()
    agent._foreground_tools = CognitiveToolService()
    agent._session_context.ensure_session("session-a")
    agent._session_context.record_user_message("之前我们聊到胡一天演过的角色。")
    agent._session_context.record_assistant_reply(content="胡一天的外形确实很适合那类角色。")
    agent._session_context.rolling_summary = "旧摘要哨兵：此前讨论过胡一天。"
    agent._session_context.summary_covered_from = date(2026, 8, 1)
    agent._session_context.summary_covered_to = date(2026, 8, 1)
    def execute_cognitive_tool_calls(
        processor: ForegroundTurnProcessor,
        **_kwargs: object,
    ) -> tuple[dict[str, object], ...]:
        """模拟工具执行期间发生另一条可见消息写入。

        Args:
            processor: 当前被测 Foreground 处理器。
            **_kwargs: 工具执行传入的其余上下文。

        Returns:
            已准备并等待提交的认知任务结果。
        """

        # 二轮必须使用 initial 前冻结的快照，不能读取工具执行期间变化的会话窗口。
        processor._session_context.record_assistant_reply(
            content="执行期新增哨兵：这句话不能进入本轮工具二轮。",
        )
        return ({
            "state": "accepted",
            "activity_id": "cognitive-a",
            "brief": "核对演员胡一天的年龄和出生年份",
            "source_user_text": "他今年应该也挺大了吧",
        },)

    agent._execute_cognitive_tool_calls = MethodType(  # type: ignore[method-assign]
        execute_cognitive_tool_calls,
        agent,
    )

    material_path = tmp_path / "notes.txt"
    material_path.write_text("材料正文哨兵", encoding="utf-8")
    caplog.set_level(
        logging.INFO,
        logger="companion.llm.foreground_turn_processor",
    )
    events = list(_commit_tool_events(agent.stream_user_text(
        "他今年应该也挺大了吧",
        now=0.0,
        interaction_session_id="session-a",
        character_card=CHARACTER_CARD,
        user_call_name="用户画像哨兵",
        user_self_intro="用户自述哨兵",
        user_extra_notes="用户备注哨兵",
        foreground_context={
            "active_turn_material_context": {
                "attachments": [{
                    "material_id": "material-a",
                    "display_name": "notes.txt",
                    "path": str(material_path),
                    "media_class": "text",
                }],
            },
        },
    )))
    _accept_final(events)

    assert len(client.calls) == 2
    initial_payload = json.dumps(client.calls[0]["messages"], ensure_ascii=False)
    followup_payload = json.dumps(client.calls[1]["messages"], ensure_ascii=False)
    for sentinel in ("旧摘要哨兵", "用户画像哨兵", "用户自述哨兵", "用户备注哨兵"):
        assert sentinel in initial_payload
        assert sentinel not in followup_payload
    assert "之前我们聊到胡一天演过的角色" in followup_payload
    assert "胡一天的外形确实很适合那类角色" in followup_payload
    assert followup_payload.count("他今年应该也挺大了吧") == 1
    assert "执行期新增哨兵" not in followup_payload
    assert "核对演员胡一天的年龄和出生年份" in followup_payload
    assert "请只确认这项任务已经开始处理" not in followup_payload
    assert "材料正文哨兵" in initial_payload
    assert "材料正文哨兵" not in followup_payload
    assert "cognitive-a" not in followup_payload
    assert "<visible_conversation_history>" in followup_payload
    assert "<dynamic_environment>" in initial_payload
    assert "<dynamic_environment>" not in followup_payload
    assert "<example_dialogues>" in initial_payload
    assert "<example_dialogues>" not in followup_payload
    assert client.calls[1]["tools"] == []
    assert client.calls[1]["intent"] == LlmInvocationIntent.text()
    assert _final_text(events) == "他具体多大，我也得查准了再说，稍等会儿。"
    context_record = next(
        record
        for record in caplog.records
        if getattr(record, "event_name", "") == "foreground_tool_followup_context_built"
    )
    structured_fields = context_record.structured_fields
    assert structured_fields["visible_dialogue_count"] == 2
    assert "之前我们聊到胡一天" not in json.dumps(
        structured_fields,
        ensure_ascii=False,
    )


def test_initial_and_tool_followup_prompts_keep_attached_notice_facts() -> None:
    """用户轮调用工具时，initial 和 tool_followup 都必须带同一批强 Notice。"""

    class TwoRoundNoticeClient(RecordingClient):
        """首轮发布可见前缀并调用工具，二轮只汇报附着 Notice。"""

        def stream_message(self, **kwargs: object) -> list[ForegroundModelStreamChunk]:
            """按轮次返回工具选择或 Notice 正文。

            Args:
                **kwargs: 当前 LLM 请求参数。

            Returns:
                首轮前缀和工具调用，或二轮 Notice 汇报。
            """

            self.calls.append(dict(kwargs))
            if len(self.calls) == 1:
                return [
                    ForegroundModelStreamChunk(kind="text_delta", text="好，我查一下。"),
                    ForegroundModelStreamChunk(kind="tool_call_started"),
                    ForegroundModelStreamChunk(
                        kind="tool_calls",
                        tool_calls=[
                            _tool_call(
                                "lookup-a",
                                "lookup_current_fact",
                                {"brief": "查询天气"},
                            )
                        ],
                    ),
                ]
            return [
                ForegroundModelStreamChunk(
                    kind="text_delta",
                    text="另外，会议纪要已经整理完成。",
                )
            ]

    client = TwoRoundNoticeClient([])
    agent = _build_agent(client)
    agent._prompt_builder = PromptBuilder()
    agent._foreground_tools = ForegroundToolService()
    agent._pre_tool_prefix_min_tokens = 1
    agent._execute_cognitive_tool_calls = MethodType(  # type: ignore[method-assign]
        lambda _self, **_kwargs: ({
            "state": "accepted",
            "activity_id": "cognitive-a",
            "brief": "查询天气",
        },),
        agent,
    )

    events = list(_commit_tool_events(agent.stream_user_text(
        "帮我查一下天气",
        now=0.0,
        interaction_session_id="session-a",
        character_card=CHARACTER_CARD,
        user_call_name="用户",
        user_self_intro="",
        user_extra_notes="",
        foreground_context={
            "notice_batch": [
                {
                    "intent": "work_result",
                    "goal": "整理会议纪要",
                    "result_context": "会议纪要已经整理完成。",
                }
            ]
        },
    )))
    _accept_final(events)

    assert len(client.calls) == 2
    initial_payload = json.dumps(client.calls[0]["messages"], ensure_ascii=False)
    followup_messages = client.calls[1]["messages"]
    assert isinstance(followup_messages, list)
    followup_payload = json.dumps(followup_messages, ensure_ascii=False)
    assert "<attached_notice_requirement>" in initial_payload
    assert "整理会议纪要" in initial_payload
    assert "会议纪要已经整理完成" in initial_payload
    assert "<attached_notice_environment>" in followup_payload
    assert "整理会议纪要" in followup_payload
    assert "会议纪要已经整理完成" in followup_payload
    assert "查询天气已经开始" not in followup_payload
    assert not any(message.get("tool_calls") for message in followup_messages)
    assert _final_text(events) == "好，我查一下。另外，会议纪要已经整理完成。"


def test_inspect_without_visible_text_requests_single_waiting_reply() -> None:
    """后台 inspect 尚未开口时应生成一次等待回复，不等待视觉终态。"""

    client = RecordingClient([
        ForegroundModelStreamChunk(kind="text_delta", text="我看一下屏幕。"),
    ])
    agent = _build_agent(client)
    agent._execute_cognitive_tool_calls = MethodType(  # type: ignore[method-assign]
        lambda _self, **_kwargs: ({
            "state": "accepted",
            "kind": "lookup_current_fact",
            "activity_id": "cognitive-a",
        },),
        agent,
    )

    events = list(_handle_calls(
        agent,
        tool_calls=[_tool_call("inspect-a", "lookup_current_fact")],
        visible_parts=[],
    ))
    _accept_final(events)

    assert len(client.calls) == 1
    assert _final_text(events) == "我看一下屏幕。"
    continuation_messages = client.calls[0]["messages"]
    assert isinstance(continuation_messages, list)
    continuation_payload = json.dumps(continuation_messages, ensure_ascii=False)
    assert "【系统事实】这件事已经真实开始，结果还没有出来。" in continuation_payload
    assert continuation_payload.count("测试请求") == 1
    assert "请只确认这项任务已经开始处理" not in continuation_payload
    assert "lookup_current_fact" in continuation_payload
    assert "cognitive-a" not in continuation_payload
    history = agent._session_context.recent_messages()
    assert [message.role for message in history] == ["assistant", "tool", "assistant"]
    assert history[0].content == ""
    assert "accepted" in history[1].content
    assert history[2].content == "我看一下屏幕。"


def test_generation_cancel_before_history_commit_leaves_no_half_tool_exchange() -> None:
    """工具执行期间取消 generation 时不得持久化缺少最终 assistant 的协议。"""

    client = RecordingClient([])
    agent = _build_agent(client)
    cancellation = Event()

    def cancel_during_execution(
        _self: ForegroundTurnProcessor,
        **_kwargs: object,
    ) -> tuple[dict[str, object], ...]:
        cancellation.set()
        return ({
            "state": "accepted",
            "kind": "lookup_current_fact",
            "activity_id": "cognitive-a",
        },)

    agent._execute_cognitive_tool_calls = MethodType(  # type: ignore[method-assign]
        cancel_during_execution,
        agent,
    )

    events = list(_handle_calls(
        agent,
        tool_calls=[_tool_call("inspect-a", "lookup_current_fact")],
        visible_parts=["我看一下。"],
        cancellation_event=cancellation,
    ))

    assert events == []
    assert client.calls == []
    assert agent._session_context.recent_messages() == []


def test_generation_cancel_after_waiting_phrase_never_enters_tool_prepare() -> None:
    """等待句 yield 后 generation 被取消时，不得再启动任何工具准备。"""

    client = RecordingClient([])
    agent = _build_agent(client)
    cancellation = Event()

    class FailIfPrepared:
        """记录工具编排是否越过取消边界。"""

        def __init__(self) -> None:
            self.calls = 0

        def prepare(self, **_: object) -> object:
            """任何调用都表示取消边界失效。"""

            self.calls += 1
            raise AssertionError("取消后不应进入工具 prepare")

    orchestrator = FailIfPrepared()
    agent._tool_call_orchestrator = orchestrator
    agent._batch_waiting_phrase_events = MethodType(  # type: ignore[method-assign]
        lambda self, *, visible_parts, **_kwargs: self._text_chunk_events(
            text_chunk="好，我查一下。",
            visible_parts=visible_parts,
        ),
        agent,
    )
    events = agent._handle_tool_calls(
        tool_calls=[_tool_call("weather-a", "get_weather")],
        character_card=CHARACTER_CARD,
        streamvox_model="",
        current_user_text="测试请求",
        current_time="2026-09-05 13:25:00 UTC+08:00",
        current_user_message=None,
        interaction_session_id="session-a",
        execution_context=_execution_context(),
        environment_snapshot=None,
        visible_parts=[],
        emit_batch_waiting_phrase=True,
        cancellation_event=cancellation,
        request_trace=ForegroundRequestTrace(
            request_id="request-a",
            generation_id="generation-a",
            request_source="text_input",
        ),
        tool_followup_visible_dialogue=(),
    )

    waiting_event = next(events)
    cancellation.set()

    assert waiting_event.type == "assistant.text_chunk"
    assert waiting_event.payload["text"] == "好，我查一下。"
    assert list(events) == []
    assert orchestrator.calls == 0
    assert agent._session_context.recent_messages() == []


def test_inline_result_with_visible_text_still_requests_continuation() -> None:
    """同步工具必须让第二轮转述真实结果，不能被等待话快速路径吞掉。"""

    client = RecordingClient([
        ForegroundModelStreamChunk(kind="text_delta", text="现在是十点。"),
    ])
    agent = _build_agent(client)
    agent._foreground_tools = InlineToolService()

    events = list(_handle_calls(
        agent,
        tool_calls=[_tool_call("weather-a", "get_weather")],
        visible_parts=["我看一下。"],
    ))
    _accept_final(events)

    assert len(client.calls) == 1
    assert _final_text(events) == "我看一下。现在是十点。"
    continuation_messages = client.calls[0]["messages"]
    assert isinstance(continuation_messages, list)
    continuation_payload = json.dumps(continuation_messages, ensure_ascii=False)
    assert "十点" in continuation_payload
    # 首轮已公开前缀必须作为独立 assistant 消息进入二轮（ADR 0042 组装顺序），
    # 否则二轮不知道哪句话已经说出口，会换说法重复。
    assert "我看一下。" in continuation_payload
    assert "测试请求" in continuation_payload
    assert "get_weather" in continuation_payload
    assert client.calls[0]["tools"] == []
    assert client.calls[0]["intent"] == LlmInvocationIntent.text()
    assert agent._session_context.recent_messages()[0].content == ""


def test_inline_result_and_strong_notice_share_combined_followup() -> None:
    """Inline 真实结果和附着强 Notice 必须在同一次二轮中同时出现。"""

    client = RecordingClient([
        ForegroundModelStreamChunk(
            kind="text_delta",
            text="现在是十点。另外，会议纪要已经整理完成。",
        ),
    ])
    agent = _build_agent(client)
    agent._foreground_tools = InlineToolService()

    events = list(_handle_calls(
        agent,
        tool_calls=[_tool_call("weather-a", "get_weather")],
        visible_parts=["我看一下。"],
        prompt_environment={
            "notice_batch": [
                {
                    "intent": "work_result",
                    "goal": "整理会议纪要",
                    "result_context": "会议纪要已经整理完成。",
                }
            ]
        },
    ))
    _accept_final(events)

    followup_messages = client.calls[0]["messages"]
    assert isinstance(followup_messages, list)
    followup_payload = json.dumps(followup_messages, ensure_ascii=False)
    assert "先把 tool 消息里的最终结果用你自己的话告诉他" in followup_payload
    assert "十点" in followup_payload
    assert "会议纪要已经整理完成" in followup_payload
    assert any(message.get("tool_calls") for message in followup_messages)
    assert _final_text(events) == (
        "我看一下。现在是十点。另外，会议纪要已经整理完成。"
    )


def test_agent_and_cognitive_acceptance_with_visible_prefix_skips_second_llm() -> None:
    """两个后台任务均已接收且模型前缀可见时，不应再次生成同义确认。"""

    client = RecordingClient([
        ForegroundModelStreamChunk(kind="text_delta", text="重复确认。"),
    ])
    agent = _build_agent(client)
    _install_accepting_background_executor(agent)
    agent._execute_cognitive_tool_calls = MethodType(  # type: ignore[method-assign]
        lambda _self, **_kwargs: ({
            "state": "accepted",
            "activity_id": "cognitive-a",
            "kind": "lookup_current_fact",
        },),
        agent,
    )

    events = list(_handle_calls(
        agent,
        tool_calls=[
            _tool_call("agent-call-a", "start_background_work"),
            _tool_call("lookup-a", "lookup_current_fact"),
        ],
        visible_parts=["我一起处理一下。"],
    ))
    _accept_final(events)

    assert client.calls == []
    assert _final_text(events) == "我一起处理一下。"
    history = agent._session_context.recent_messages()
    assert [message.role for message in history] == [
        "assistant",
        "tool",
        "tool",
        "assistant",
    ]


def test_agent_and_cognitive_acceptance_without_prefix_requests_confirmation() -> None:
    """两个后台任务均已接收但尚未开口时，应由第二轮生成一次综合确认。"""

    client = RecordingClient([
        ForegroundModelStreamChunk(kind="text_delta", text="两项任务都开始处理了。"),
    ])
    agent = _build_agent(client)
    _install_accepting_background_executor(agent)
    agent._execute_cognitive_tool_calls = MethodType(  # type: ignore[method-assign]
        lambda _self, **_kwargs: ({
            "state": "accepted",
            "activity_id": "cognitive-a",
        },),
        agent,
    )

    events = list(_handle_calls(
        agent,
        tool_calls=[
            _tool_call("agent-call-a", "start_background_work"),
            _tool_call("lookup-a", "lookup_current_fact"),
        ],
        visible_parts=[],
    ))
    _accept_final(events)

    assert len(client.calls) == 1
    assert _final_text(events) == "两项任务都开始处理了。"


@pytest.mark.parametrize(
    ("agent_state", "cognitive_state"),
    [
        ("rejected", "accepted"),
        ("rejected", "rejected"),
    ],
)
def test_background_failure_with_visible_prefix_still_requests_explanation(
    agent_state: str,
    cognitive_state: str,
) -> None:
    """后台任务部分或全部失败时，自然前缀不能吞掉真实失败说明。"""

    client = RecordingClient([
        ForegroundModelStreamChunk(kind="text_delta", text="后台智能体没启动，查询任务状态已说明。"),
    ])
    agent = _build_agent(client)
    _install_accepting_background_executor(agent)
    agent._execute_cognitive_tool_calls = MethodType(  # type: ignore[method-assign]
        lambda _self, **_kwargs: ({
            "state": cognitive_state,
            "activity_id": "cognitive-a" if cognitive_state == "accepted" else None,
            "safe_message": "查询任务没有启动。",
        },),
        agent,
    )
    agent._background_agent_tool_executor.execute_start_background_work = MethodType(  # type: ignore[method-assign]
        lambda _self, **_kwargs: {
            "state": agent_state,
            "safe_message": "后台智能体没有启动。",
        },
        agent,
    )

    events = list(_handle_calls(
        agent,
        tool_calls=[
            _tool_call("agent-call-a", "start_background_work"),
            _tool_call("lookup-a", "lookup_current_fact"),
        ],
        visible_parts=["我一起处理一下。"],
    ))
    _accept_final(events)

    assert len(client.calls) == 1
    followup_payload = json.dumps(client.calls[0]["messages"], ensure_ascii=False)
    assert "后台智能体没有启动。" in followup_payload
    if cognitive_state == "accepted":
        assert "【系统事实】这件事已经真实开始，结果还没有出来。" in followup_payload
        assert "查询任务没有启动。" not in followup_payload
    else:
        assert "查询任务没有启动。" in followup_payload
    assert "safe_message" not in followup_payload
    assert "activity_id" not in followup_payload
    assert "instruction" not in followup_payload
    assert _final_text(events).endswith("后台智能体没启动，查询任务状态已说明。")


def test_duplicate_agent_dispatch_is_silent_but_keeps_complete_tool_protocol() -> None:
    """重复派单不打扰用户，但稳定历史必须回应每个 tool_call_id。"""

    client = RecordingClient([
        ForegroundModelStreamChunk(kind="text_delta", text="不应生成。"),
    ])
    agent = _build_agent(client)
    _install_accepting_background_executor(agent)

    events = list(_handle_calls(
        agent,
        tool_calls=[
            _tool_call("agent-call-a", "start_background_work"),
            _tool_call("agent-call-b", "start_background_work"),
        ],
        visible_parts=["我开始整理材料。"],
    ))
    _accept_final(events)

    assert client.calls == []
    assert _final_text(events) == "我开始整理材料。"
    history = agent._session_context.recent_messages()
    assert [message.role for message in history] == [
        "assistant",
        "tool",
        "tool",
        "assistant",
    ]
    assert [message.tool_call_id for message in history[1:3]] == [
        "agent-call-a",
        "agent-call-b",
    ]
    assert "ignored_duplicate" in history[2].content


def test_retired_task_result_call_cannot_create_background_reservation() -> None:
    """异常模型返回未声明旧工具名时，只能得到不支持结果，不能启动旧任务。"""

    client = RecordingClient([
        ForegroundModelStreamChunk(kind="text_delta", text="旧工具已经不可用。"),
    ])
    agent = _build_agent(client)
    agent._foreground_tools = ForegroundToolService()

    events = list(_handle_calls(
        agent,
        tool_calls=[_tool_call("legacy-call-a", "task_result")],
        visible_parts=[],
    ))
    _accept_final(events)

    completed_event = next(
        event
        for event in events
        if getattr(event, "type", "") == "llm.foreground.tool_call_completed"
    )
    assert completed_event.payload["activity_id"] is None
    assert completed_event.commit_action is None
    followup_payload = json.dumps(client.calls[0]["messages"], ensure_ascii=False)
    assert "不支持的 foreground 工具：task_result" in followup_payload


def test_mixed_followup_omits_silent_call_but_history_preserves_it() -> None:
    """混合二轮应成对省略静默调用，同时完整持久化原始工具事务。"""

    client = RecordingClient([
        ForegroundModelStreamChunk(kind="text_delta", text="天气结果出来了，材料也开始处理了。"),
    ])
    agent = _build_agent(client)
    agent._foreground_tools = InlineToolService()
    _install_accepting_background_executor(agent)

    events = list(_handle_calls(
        agent,
        tool_calls=[
            _tool_call("weather-a", "get_weather"),
            _tool_call("agent-call-a", "start_background_work"),
            _tool_call("agent-call-b", "start_background_work"),
        ],
        visible_parts=[],
    ))
    _accept_final(events)

    followup_messages = client.calls[0]["messages"]
    assert isinstance(followup_messages, list)
    assistant_message = next(
        message for message in followup_messages if message.get("tool_calls")
    )
    assert [call["id"] for call in assistant_message["tool_calls"]] == [
        "weather-a",
        "agent-call-a",
    ]
    assert [
        message["tool_call_id"]
        for message in followup_messages
        if message.get("role") == "tool"
    ] == ["weather-a", "agent-call-a"]

    history = agent._session_context.recent_messages()
    assert [message.role for message in history] == [
        "assistant",
        "tool",
        "tool",
        "tool",
        "assistant",
    ]
    assert [message.tool_call_id for message in history[1:4]] == [
        "weather-a",
        "agent-call-a",
        "agent-call-b",
    ]
    assert "ignored_duplicate" in history[3].content


def test_question_answer_and_inline_result_share_one_foreground_turn() -> None:
    """资料回答提交后，本轮其它即时结果仍应一起进入自然续写。"""

    client = RecordingClient([
        ForegroundModelStreamChunk(
            kind="text_delta",
            text="好，第三版已经交回去继续处理；现在是十点。",
        ),
    ])
    agent = _build_agent(client)
    agent._background_agent_tool_executor.execute_work_interaction = MethodType(  # type: ignore[method-assign]
        lambda _self, **_kwargs: {
            "state": "accepted",
            "request_id": "question-a",
            "activity_id": "activity-a",
            "outcome": "answer",
            "answers": {"使用哪个版本？": "第三版"},
            "safe_message": "本次回应已被当前工作接收。",
        },
        agent,
    )
    event_iterator = iter(_handle_calls(
        agent,
        tool_calls=[
            _tool_call(
                "answer-a",
                "respond_to_work_interaction",
                {
                    "request_id": "question-a",
                    "outcome": "answer",
                    "answers": {"使用哪个版本？": "第三版"},
                },
            ),
            _tool_call("weather-a", "get_weather"),
        ],
        visible_parts=[],
        prompt_environment={
            "pending_work_interactions": [
                {
                    "request_id": "question-a",
                    "activity_id": "activity-a",
                    "kind": "question",
                    "questions": [{"question": "使用哪个版本？"}],
                }
            ]
        },
    ))
    events: list[object] = []
    for event in event_iterator:
        events.append(event)
        if (
            getattr(event, "type", "") == "llm.foreground.tool_call_completed"
            and event.payload.get("name") == "respond_to_work_interaction"
        ):
            assert event.commit_action is None
    _accept_final(events)

    assert _final_text(events) == "好，第三版已经交回去继续处理；现在是十点。"
    followup_payload = json.dumps(client.calls[0]["messages"], ensure_ascii=False)
    assert "用户的资料答案已被当前工作真实接收；当前没有新的工作结果" in followup_payload
    assert "十点" in followup_payload
    answer_result_message = next(
        message
        for message in client.calls[0]["messages"]
        if message.get("role") == "tool"
        and message.get("tool_call_id") == "answer-a"
    )
    assert answer_result_message["content"] == (
        "用户的资料答案已被当前工作真实接收；当前没有新的工作结果。"
    )


def test_question_answer_followup_keeps_the_user_request_context() -> None:
    """资料回答不应被误判为新后台任务的接单回应。"""

    client = RecordingClient([
        ForegroundModelStreamChunk(kind="text_delta", text="第三版已经交回去继续处理。"),
    ])
    agent = _build_agent(client)
    agent._background_agent_tool_executor.execute_work_interaction = MethodType(  # type: ignore[method-assign]
        lambda _self, **_kwargs: {
            "state": "accepted",
            "request_id": "question-a",
            "activity_id": "activity-a",
            "outcome": "answer",
            "answers": {"使用哪个版本？": "第三版"},
            "safe_message": "本次回应已被当前工作接收。",
        },
        agent,
    )

    events = list(_handle_calls(
        agent,
        tool_calls=[
            _tool_call(
                "answer-a",
                "respond_to_work_interaction",
                {
                    "request_id": "question-a",
                    "outcome": "answer",
                    "answers": {"使用哪个版本？": "第三版"},
                },
            ),
        ],
        visible_parts=[],
        prompt_environment={
            "pending_work_interactions": [
                {
                    "request_id": "question-a",
                    "activity_id": "activity-a",
                    "kind": "question",
                    "questions": [{"question": "使用哪个版本？"}],
                }
            ]
        },
    ))
    _accept_final(events)

    followup_payload = json.dumps(client.calls[0]["messages"], ensure_ascii=False)
    assert "测试请求" in followup_payload
    assert "请只对已受理且尚无结果的任务生成一条自然接单回应" not in followup_payload
    assert _final_text(events) == "第三版已经交回去继续处理。"


def test_deferred_tool_followup_prompt_includes_visible_prefix() -> None:
    """deferred 工具（权限确认）的二轮 Prompt 必须带上首轮已公开前缀，避免二轮换说法重复。

    前缀只对后台 Activity 工具做独立 segment 结算（ADR 0020），但进入二轮 Prompt
    不受工具类型限制（ADR 0042 的组装顺序）；否则模型看不到首轮已经说过的话。
    """

    client = RecordingClient([
        ForegroundModelStreamChunk(kind="text_delta", text="好，我接着处理。"),
    ])
    agent = _build_agent(client)
    agent._background_agent_tool_executor.execute_work_interaction = MethodType(  # type: ignore[method-assign]
        lambda _self, **_kwargs: {
            "state": "accepted",
            "request_id": "permission-a",
            "activity_id": "activity-a",
            "outcome": "allow",
            "safe_message": "用户已明确允许，本次决定已被当前工作真实接收。",
        },
        agent,
    )

    events = list(_handle_calls(
        agent,
        tool_calls=[
            _tool_call(
                "permission-a",
                "respond_to_work_interaction",
                {"request_id": "permission-a", "outcome": "allow"},
            ),
        ],
        visible_parts=["既然你放话让我放手去办，这事便接着往下做。"],
        prompt_environment={
            "pending_work_interactions": [
                {
                    "request_id": "permission-a",
                    "activity_id": "activity-a",
                    "kind": "permission",
                    "action_summary": "访问网络获取完成这项工作所需的信息",
                }
            ]
        },
    ))
    _accept_final(events)

    assert len(client.calls) == 1
    followup_messages = client.calls[0]["messages"]
    assert isinstance(followup_messages, list)
    # 前缀必须作为独立 assistant 消息，位于当前原话之后、工具事务之前。
    prefix_index = next(
        index
        for index, message in enumerate(followup_messages)
        if message.get("role") == "assistant"
        and message.get("content") == "既然你放话让我放手去办，这事便接着往下做。"
    )
    user_index = next(
        index
        for index, message in enumerate(followup_messages)
        if message.get("role") == "user"
    )
    tool_call_index = next(
        index
        for index, message in enumerate(followup_messages)
        if message.get("tool_calls")
    )
    assert user_index < prefix_index < tool_call_index
    assert _final_text(events) == "既然你放话让我放手去办，这事便接着往下做。好，我接着处理。"


def test_empty_inline_continuation_cannot_leave_only_waiting_phrase() -> None:
    """Inline 二轮空流时，已有等待句不能冒充工具结果回复。"""

    client = RecordingClient([])
    agent = _build_agent(client)
    agent._foreground_tools = InlineToolService()

    events = list(_handle_calls(
        agent,
        tool_calls=[_tool_call("weather-a", "get_weather")],
        visible_parts=["好，我查一下。"],
    ))
    _accept_final(events)

    final_text = _final_text(events)
    assert final_text.startswith("好，我查一下。")
    assert final_text != "好，我查一下。"
    assert final_text.endswith("十点")
    assert agent._session_context.recent_messages()[-1].content == final_text


def test_repeated_second_round_tool_call_uses_fact_fallback() -> None:
    """第二轮再次请求工具时，应拒绝调用并用真实结果完成回复。"""

    client = RecordingClient([
        ForegroundModelStreamChunk(
            kind="tool_calls",
            tool_calls=[_tool_call("weather-b", "get_weather")],
        ),
    ])
    agent = _build_agent(client)
    agent._foreground_tools = InlineToolService()

    events = list(_handle_calls(
        agent,
        tool_calls=[_tool_call("weather-a", "get_weather")],
        visible_parts=["好，我查一下。"],
    ))
    _accept_final(events)

    assert any(
        getattr(event, "type", "") == "llm.foreground.tool_call_rejected"
        for event in events
    )
    assert _final_text(events).endswith("十点")


def test_repeated_second_round_tool_call_appends_fallback_after_partial_text() -> None:
    """二轮先流出半句再请求工具时，必须追加最终事实兜底。"""

    client = RecordingClient([
        ForegroundModelStreamChunk(kind="text_delta", text="我先说半句"),
        ForegroundModelStreamChunk(
            kind="tool_calls",
            tool_calls=[_tool_call("weather-b", "get_weather")],
        ),
    ])
    agent = _build_agent(client)
    agent._foreground_tools = InlineToolService()

    events = list(_handle_calls(
        agent,
        tool_calls=[_tool_call("weather-a", "get_weather")],
        visible_parts=["好，我查一下。"],
    ))
    _accept_final(events)

    final_text = _final_text(events)
    assert "我先说半句" in final_text
    assert final_text.endswith("十点")


def test_followup_protocol_leak_uses_deterministic_fallback() -> None:
    """禁止工具的第二轮发生厂商协议泄漏时，应提交完整事务并使用确定性兜底。"""

    class ProtocolLeakClient(RecordingClient):
        """记录续写参数后模拟厂商协议泄漏。"""

        def stream_message(self, **kwargs: object) -> list[ForegroundModelStreamChunk]:
            """记录请求并抛出协议异常。

            Args:
                **kwargs: Foreground 续写请求参数。

            Raises:
                LlmProtocolLeakError: 模拟禁止工具请求中的结构化协议泄漏。
            """

            self.calls.append(dict(kwargs))
            raise LlmProtocolLeakError(
                provider_id="deepseek",
                protocol="structured_tool_call",
            )

    client = ProtocolLeakClient([])
    agent = _build_agent(client)
    agent._foreground_tools = InlineToolService()

    events = list(_handle_calls(
        agent,
        tool_calls=[_tool_call("weather-a", "get_weather")],
        visible_parts=["好，我查一下。"],
    ))
    _accept_final(events)

    assert client.calls[0]["tools"] == []
    assert client.calls[0]["intent"] == LlmInvocationIntent.text()
    assert _final_text(events).endswith("十点")
    assert [message.role for message in agent._session_context.recent_messages()] == [
        "assistant",
        "tool",
        "assistant",
    ]


def test_followup_protocol_leak_appends_fallback_after_partial_text() -> None:
    """二轮先输出半句再协议泄漏时，必须继续追加完整事实兜底。"""

    class PartialProtocolLeakClient(RecordingClient):
        """先输出半句，再模拟厂商协议泄漏。"""

        def stream_message(
            self,
            **kwargs: object,
        ) -> Iterator[ForegroundModelStreamChunk]:
            """记录请求并在半句后抛出协议异常。

            Args:
                **kwargs: Foreground 续写请求参数。

            Yields:
                协议异常前已经流出的半句文字。

            Raises:
                LlmProtocolLeakError: 模拟半句之后出现的结构化协议泄漏。
            """

            self.calls.append(dict(kwargs))
            yield ForegroundModelStreamChunk(kind="text_delta", text="我只说了半句")
            raise LlmProtocolLeakError(
                provider_id="deepseek",
                protocol="structured_tool_call",
            )

    client = PartialProtocolLeakClient([])
    agent = _build_agent(client)
    agent._foreground_tools = InlineToolService()

    events = list(_handle_calls(
        agent,
        tool_calls=[_tool_call("weather-a", "get_weather")],
        visible_parts=["好，我查一下。"],
    ))
    _accept_final(events)

    final_text = _final_text(events)
    assert "我只说了半句" in final_text
    assert final_text.endswith("十点")


def test_windows_application_empty_followup_lists_ambiguous_candidates() -> None:
    """应用名称并列时，即使二轮没有正文也必须列出全部候选请用户确认。"""

    client = RecordingClient([])
    agent = _build_agent(client)
    agent._foreground_tools = WindowsApplicationInlineToolService(
        {
            "state": "rejected",
            "reason": "ambiguous_application",
            "safe_message": "名称无法唯一确定。",
            "candidates": ["QQ", "QQ音乐"],
        }
    )

    events = list(_handle_calls(
        agent,
        tool_calls=[
            _tool_call(
                "application-a",
                "open_installed_windows_app",
                {"application_name": "QQ"},
            )
        ],
        visible_parts=[],
    ))
    _accept_final(events)

    assert _final_text(events) == (
        "找到了多个同样匹配的应用：QQ、QQ音乐。请告诉我要打开哪一个。"
    )


def test_windows_application_repeated_followup_call_uses_launch_request_fact() -> None:
    """二轮重复请求工具时，兜底只能说明 Windows 接受了启动请求。"""

    client = RecordingClient([
        ForegroundModelStreamChunk(
            kind="tool_calls",
            tool_calls=[
                _tool_call(
                    "application-b",
                    "open_installed_windows_app",
                    {"application_name": "QQ"},
                )
            ],
        ),
    ])
    agent = _build_agent(client)
    agent._foreground_tools = WindowsApplicationInlineToolService(
        {
            "state": "completed",
            "outcome": "launch_requested",
            "application_name": "QQ",
        }
    )

    events = list(_handle_calls(
        agent,
        tool_calls=[
            _tool_call(
                "application-a",
                "open_installed_windows_app",
                {"application_name": "QQ"},
            )
        ],
        visible_parts=[],
    ))
    _accept_final(events)

    assert _final_text(events) == "Windows 已接受 QQ 的启动请求。"


def test_windows_application_protocol_leak_preserves_new_instance_fact() -> None:
    """二轮协议泄漏时，已确认的新实例事实不能退化成无摘要提示。"""

    class ProtocolLeakClient(RecordingClient):
        """记录二轮参数后模拟禁止工具请求发生协议泄漏。"""

        def stream_message(self, **kwargs: object) -> list[ForegroundModelStreamChunk]:
            """抛出协议异常，迫使处理器使用确定性应用启动兜底。"""

            self.calls.append(dict(kwargs))
            raise LlmProtocolLeakError(
                provider_id="deepseek",
                protocol="structured_tool_call",
            )

    client = ProtocolLeakClient([])
    agent = _build_agent(client)
    agent._foreground_tools = WindowsApplicationInlineToolService(
        {
            "state": "completed",
            "outcome": "new_instance_created",
            "application_name": "QQ",
        }
    )

    events = list(_handle_calls(
        agent,
        tool_calls=[
            _tool_call(
                "application-a",
                "open_installed_windows_app",
                {"application_name": "QQ", "launch_mode": "new_instance"},
            )
        ],
        visible_parts=[],
    ))
    _accept_final(events)

    assert client.calls[0]["tools"] == []
    assert _final_text(events) == "已确认 QQ 创建了一个新实例。"


def test_protocol_leak_fallback_preserves_inline_result_and_notice() -> None:
    """协议泄漏不能覆盖 Inline 结果，也不能吞掉附着强 Notice。"""

    class ProtocolLeakClient(RecordingClient):
        """记录二轮参数后模拟禁止工具请求发生协议泄漏。"""

        def stream_message(self, **kwargs: object) -> list[ForegroundModelStreamChunk]:
            """始终抛出协议异常。

            Args:
                **kwargs: Foreground 续写请求参数。

            Raises:
                LlmProtocolLeakError: 模拟结构化工具协议泄漏。
            """

            self.calls.append(dict(kwargs))
            raise LlmProtocolLeakError(
                provider_id="deepseek",
                protocol="structured_tool_call",
            )

    client = ProtocolLeakClient([])
    agent = _build_agent(client)
    agent._foreground_tools = InlineToolService()

    events = list(_handle_calls(
        agent,
        tool_calls=[_tool_call("weather-a", "get_weather")],
        visible_parts=["我看一下。"],
        prompt_environment={
            "notice_batch": [
                {
                    "intent": "work_result",
                    "goal": "整理会议纪要",
                    "result_context": "会议纪要已经整理完成。",
                }
            ]
        },
    ))
    _accept_final(events)

    final_text = _final_text(events)
    assert "十点" in final_text
    assert "整理会议纪要已经完成" in final_text
    assert "当前版本只能执行一轮工具调用" not in final_text


def test_background_already_running_uses_no_tool_followup_without_commit() -> None:
    """误调占用槽位只能进入无工具二轮，不能产生第二个 Activity 提交动作。"""

    client = RecordingClient([
        ForegroundModelStreamChunk(
            kind="text_delta",
            text="我这会儿还有一项工作在处理，暂时不能再开新的。",
        ),
    ])
    agent = _build_agent(client)
    agent._background_agent_tool_executor.execute_start_background_work = MethodType(  # type: ignore[method-assign]
        lambda _self, **_kwargs: {
            "state": "already_running",
            "reason": "background_slot_occupied",
            "safe_message": "已有任务正在处理，当前无法开启新任务。",
        },
        agent,
    )

    events = list(_handle_calls(
        agent,
        tool_calls=[_tool_call("background-b", "start_background_work")],
        visible_parts=[],
    ))
    _accept_final(events)

    completed = next(
        event
        for event in events
        if getattr(event, "type", "") == "llm.foreground.tool_call_completed"
    )
    assert completed.payload["state"] == "already_running"
    assert completed.payload["activity_id"] is None
    assert completed.commit_action is None
    assert len(client.calls) == 1
    assert client.calls[0]["tools"] == []
    assert client.calls[0]["intent"] == LlmInvocationIntent.text()
    followup_payload = json.dumps(client.calls[0]["messages"], ensure_ascii=False)
    assert "请求未能开始：已有任务正在处理，当前无法开启新任务" in followup_payload
    assert _final_text(events) == "我这会儿还有一项工作在处理，暂时不能再开新的。"


def test_background_rejection_never_emits_a_waiting_phrase_before_prepare() -> None:
    """后台 prepare 拒绝时，批次等待句不能抢先制造正在处理的假象。"""

    client = RecordingClient([
        ForegroundModelStreamChunk(
            kind="text_delta",
            text="我现在还有一项工作，暂时不能再开新的。",
        ),
    ])
    agent = _build_agent(client)
    agent._batch_waiting_phrase_events = MethodType(  # type: ignore[method-assign]
        lambda self, *, visible_parts, **_kwargs: self._text_chunk_events(
            text_chunk="好，我这就开始处理。",
            visible_parts=visible_parts,
        ),
        agent,
    )
    agent._background_agent_tool_executor.execute_start_background_work = MethodType(  # type: ignore[method-assign]
        lambda _self, **_kwargs: {
            "state": "already_running",
            "reason": "background_slot_occupied",
            "safe_message": "已有任务正在处理，当前无法开启新任务。",
        },
        agent,
    )

    events = list(_handle_calls(
        agent,
        tool_calls=[_tool_call("background-b", "start_background_work")],
        visible_parts=[],
        emit_batch_waiting_phrase=True,
    ))
    _accept_final(events)

    assert "好，我这就开始处理" not in _final_text(events)
    assert all(
        "好，我这就开始处理" not in str(event.payload.get("text") or "")
        for event in events
        if hasattr(event, "payload")
    )


@pytest.mark.parametrize(
    "repeat_tool_call",
    [False, True],
    ids=["empty_reply", "repeated_tool_call"],
)
def test_background_already_running_uses_fixed_rejection_fallback(
    repeat_tool_call: bool,
) -> None:
    """占用拒绝的二轮为空或再调工具时，只能返回固定安全事实。"""

    followup_chunks = (
        [
            ForegroundModelStreamChunk(
                kind="tool_calls",
                tool_calls=[_tool_call("background-c", "start_background_work")],
            )
        ]
        if repeat_tool_call
        else []
    )
    client = RecordingClient(followup_chunks)
    agent = _build_agent(client)
    agent._background_agent_tool_executor.execute_start_background_work = MethodType(  # type: ignore[method-assign]
        lambda _self, **_kwargs: {
            "state": "already_running",
            "reason": "background_slot_occupied",
            "safe_message": "已有任务正在处理，当前无法开启新任务。",
        },
        agent,
    )

    events = list(_handle_calls(
        agent,
        tool_calls=[_tool_call("background-b", "start_background_work")],
        visible_parts=[],
    ))
    _accept_final(events)

    assert _final_text(events) == "已有任务正在处理，当前无法开启新任务。"
    assert "我已经接下" not in _final_text(events)


def test_background_already_running_protocol_leak_uses_fixed_rejection() -> None:
    """占用拒绝的二轮发生协议泄漏时，不得回退成接单确认或轮次说明。"""

    class ProtocolLeakClient(RecordingClient):
        """模拟拒绝续写阶段的厂商协议泄漏。"""

        def stream_message(self, **kwargs: object) -> list[ForegroundModelStreamChunk]:
            """记录请求后抛出协议异常。

            Args:
                **kwargs: Foreground 续写参数。

            Raises:
                LlmProtocolLeakError: 始终模拟结构化协议泄漏。
            """

            self.calls.append(dict(kwargs))
            raise LlmProtocolLeakError(
                provider_id="deepseek",
                protocol="structured_tool_call",
            )

    client = ProtocolLeakClient([])
    agent = _build_agent(client)
    agent._background_agent_tool_executor.execute_start_background_work = MethodType(  # type: ignore[method-assign]
        lambda _self, **_kwargs: {
            "state": "already_running",
            "reason": "background_slot_occupied",
            "safe_message": "已有任务正在处理，当前无法开启新任务。",
        },
        agent,
    )

    events = list(_handle_calls(
        agent,
        tool_calls=[_tool_call("background-b", "start_background_work")],
        visible_parts=[],
    ))
    _accept_final(events)

    assert _final_text(events) == "已有任务正在处理，当前无法开启新任务。"
    assert "当前版本只能执行一轮工具调用" not in _final_text(events)


def test_occupied_background_call_commits_public_prefix_before_rejection() -> None:
    """Activity 已占用时，已经公开的接单前缀要和二轮拒绝分别结算。"""

    class TwoRoundClient(RecordingClient):
        """首轮错误接单并派单，二轮按安全事实拒绝。"""

        def stream_message(self, **kwargs: object) -> list[ForegroundModelStreamChunk]:
            """按轮次返回错误首轮和正确拒绝。

            Args:
                **kwargs: 当前模型请求参数。

            Returns:
                首轮工具调用或二轮自然拒绝。
            """

            self.calls.append(dict(kwargs))
            if len(self.calls) == 1:
                return [
                    ForegroundModelStreamChunk(
                        kind="text_delta",
                        text="我已经接下了，这就去处理。",
                    ),
                    ForegroundModelStreamChunk(kind="tool_call_started"),
                    ForegroundModelStreamChunk(
                        kind="tool_calls",
                        tool_calls=[
                            _tool_call(
                                "background-b",
                                "start_background_work",
                                {"goal": "处理第二项工作", "deliverable": True},
                            )
                        ],
                    ),
                ]
            return [
                ForegroundModelStreamChunk(
                    kind="text_delta",
                    text="我这会儿还有一项工作在处理，暂时不能再开新的。",
                )
            ]

    class OccupiedBackgroundExecutor:
        """提供全局占用快照，并拒绝任何误派单。"""

        def activity_snapshots(
            self,
            _interaction_session_id: str,
        ) -> tuple[ForegroundActivitySnapshot, ...]:
            """返回不随当前会话变化的占用事实集合。"""

            return (
                ForegroundActivitySnapshot(
                    activity_id="background-a",
                    interaction_session_id="session-old",
                    source="background_agent",
                    category="后台工作",
                    goal="当前已有后台工作正在处理。",
                    state="running",
                ),
            )

        def prepare(self, **_kwargs: object) -> tuple[bool, list[dict[str, object]]]:
            """拒绝创建第二个后台 Activity。"""

            return False, [
                {
                    "type": "background_agent.rejected",
                    "payload": {"reason": "background_slot_occupied"},
                }
            ]

    client = TwoRoundClient([])
    agent = _build_agent(client)
    agent._prompt_builder = PromptBuilder()
    agent._foreground_tools = ForegroundToolService()
    agent._cognitive_activity_executor = None
    occupied_executor = OccupiedBackgroundExecutor()
    agent._background_agent_activity_executor = occupied_executor
    agent._background_agent_tool_executor = BackgroundAgentToolExecutor(
        occupied_executor
    )
    agent._token_counter = len
    agent._pre_tool_prefix_min_tokens = 1

    events = list(agent.stream_user_text(
        "再帮我处理一项工作",
        now=0.0,
        interaction_session_id="session-new",
        character_card=CHARACTER_CARD,
        user_call_name="用户",
        user_self_intro="",
        user_extra_notes="",
    ))
    _accept_response_events(events)

    visible_chunks = [
        str(event.payload.get("text") or "")
        for event in events
        if getattr(event, "type", "") == "assistant.text_chunk"
    ]
    assert any("我已经接下了" in chunk for chunk in visible_chunks)
    segment = next(
        event
        for event in events
        if getattr(event, "type", "") == "assistant.text_segment_final"
    )
    assert segment.payload["text"] == "我已经接下了，这就去处理。"
    assert _final_text(events) == "我这会儿还有一项工作在处理，暂时不能再开新的。"
    history = agent._session_context.recent_messages()
    assert history[1].content == "我已经接下了，这就去处理。"
    assert history[-1].content == "我这会儿还有一项工作在处理，暂时不能再开新的。"
    assert len(client.calls) == 2
    assert client.calls[1]["tools"] == []
    assert client.calls[1]["intent"] == LlmInvocationIntent.text()
    followup_messages = client.calls[1]["messages"]
    assert isinstance(followup_messages, list)
    assert followup_messages[-3] == {
        "role": "assistant",
        "content": "我已经接下了，这就去处理。",
    }


@pytest.mark.parametrize("publish_prefix", [False, True])
def test_unconfigured_background_call_uses_protected_console_rejection(
    publish_prefix: bool,
) -> None:
    """未配置误调用要丢弃未公开前缀，并把已公开前缀独立结算。"""

    class UnconfiguredClient(RecordingClient):
        """首轮错误接单并调用工具，二轮模拟模型没有返回正文。"""

        def stream_message(self, **kwargs: object) -> list[ForegroundModelStreamChunk]:
            """按轮次返回未配置场景需要的模型流。

            Args:
                **kwargs: 当前 Foreground 模型请求参数。

            Returns:
                首轮返回错误接单和工具调用，二轮返回空流。
            """

            self.calls.append(dict(kwargs))
            if len(self.calls) > 1:
                return []
            return [
                ForegroundModelStreamChunk(
                    kind="text_delta",
                    text="我已经接下了，这就去处理。",
                ),
                ForegroundModelStreamChunk(kind="tool_call_started"),
                ForegroundModelStreamChunk(
                    kind="tool_calls",
                    tool_calls=[
                        _tool_call(
                            "background-a",
                            "start_background_work",
                            {"goal": "整理录音", "deliverable": True},
                        )
                    ],
                ),
            ]

    class UnconfiguredBackgroundExecutor:
        """模拟没有 Activity、也没有完整 Claude Code 配置的执行器。"""

        def __init__(self) -> None:
            """初始化 prepare 调用计数。"""

            self.prepare_call_count = 0

        def activity_snapshots(
            self,
            _interaction_session_id: str,
        ) -> tuple[ForegroundActivitySnapshot, ...]:
            """表示当前没有占用后台槽位的 Activity。

            Args:
                _interaction_session_id: 当前产品交互会话编号。

            Returns:
                当前没有 Activity，因此固定返回空集合。
            """

            return ()

        def prepare(self, **_kwargs: object) -> tuple[bool, list[dict[str, object]]]:
            """在创建任何任务前返回未配置拒绝。

            Args:
                **_kwargs: 已经由后台工具执行边界校验的任务输入。

            Returns:
                未配置拒绝事件。
            """

            self.prepare_call_count += 1
            return False, [
                {
                    "type": "background_agent.rejected",
                    "payload": {"reason": "background_agent_unconfigured"},
                }
            ]

    client = UnconfiguredClient([])
    background_executor = UnconfiguredBackgroundExecutor()
    agent = build_foreground_processor_for_test(
        client=client,
        prompt_builder=PromptBuilder(),
        session_context=SessionContext(token_counter=len),
        foreground_tools=ForegroundToolService(),
        background_agent_activity_executor=background_executor,
    )
    agent._pre_tool_prefix_min_tokens = 1 if publish_prefix else 100

    events = list(agent.stream_user_text(
        "请帮我整理这段录音",
        now=0.0,
        interaction_session_id="session-a",
        character_card=CHARACTER_CARD,
        user_call_name="用户",
        user_self_intro="",
        user_extra_notes="",
    ))
    _accept_response_events(events)

    expected_message = (
        "后台工作尚未配置。请先打开 Console 的“后台工作”设置，填写 "
        "ANTHROPIC_BASE_URL，并配置 ANTHROPIC_API_KEY 或 ANTHROPIC_AUTH_TOKEN。"
    )
    assert background_executor.prepare_call_count == 1
    assert len(client.calls) == 2
    assert client.calls[1]["tools"] == []
    assert client.calls[1]["intent"] == LlmInvocationIntent.text()
    assert _final_text(events) == expected_message
    segment_events = [
        event
        for event in events
        if getattr(event, "type", "") == "assistant.text_segment_final"
    ]
    assert len(segment_events) == (1 if publish_prefix else 0)
    if publish_prefix:
        assert segment_events[0].payload["text"] == "我已经接下了，这就去处理。"
        followup_messages = client.calls[1]["messages"]
        assert isinstance(followup_messages, list)
        assert followup_messages[-3] == {
            "role": "assistant",
            "content": "我已经接下了，这就去处理。",
        }
        assert [message.content for message in agent._session_context.recent_messages()] == [
            "请帮我整理这段录音",
            "我已经接下了，这就去处理。",
            "",
            json.dumps(
                {
                    "state": "rejected",
                    "reason": "background_agent_unconfigured",
                    "safe_message": expected_message,
                },
                ensure_ascii=False,
            ),
            expected_message,
        ]
    else:
        assert all(
            "我已经接下了" not in str(event.payload.get("text") or "")
            for event in events
            if hasattr(event, "payload")
        )
    completed = next(
        event
        for event in events
        if getattr(event, "type", "") == "llm.foreground.tool_call_completed"
    )
    assert completed.payload["state"] == "rejected"
    assert completed.payload["activity_id"] is None
    assert completed.commit_action is None
    assert agent._session_context.recent_messages()[-1].content == expected_message


class InlineToolService:
    """返回固定同步结果的最小工具服务。"""

    def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        inline_execution_context: object | None = None,
    ) -> dict[str, object]:
        """返回同步工具测试结果。

        Args:
            name: 工具名称。
            arguments: 工具参数。
            inline_execution_context: 本测试不使用的 Inline 执行门禁。

        Returns:
            固定时间结果。
        """

        del inline_execution_context
        return {
            "ok": True,
            "state": "completed",
            "name": name,
            "arguments": arguments,
            "text": "十点",
        }


class WindowsApplicationInlineToolService:
    """返回调用方指定的已安装应用同步启动结果。"""

    def __init__(self, result: dict[str, object]) -> None:
        """冻结每次执行都要返回的安全应用启动事实。

        Args:
            result: 已经符合公开 Tool payload 合同的测试结果。
        """

        self._result = dict(result)

    def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        inline_execution_context: object | None = None,
    ) -> dict[str, object]:
        """返回独立副本，避免测试之间共享可变结果。

        Args:
            name: 工具名称。
            arguments: 工具参数。
            inline_execution_context: 本测试不使用的 Inline 执行门禁。

        Returns:
            调用方构造的应用启动结果副本。
        """

        del name, arguments, inline_execution_context
        return dict(self._result)


class ActivityCommitExecutor:
    """模拟工具事件提交后能够成功启动的 Activity 执行器。"""

    def start(self, _activity_id: str) -> bool:
        """确认预留 Activity 已经启动。

        Args:
            _activity_id: prepare 阶段分配的 Activity 编号。

        Returns:
            固定返回真，表示提交后的启动成功。
        """

        return True

    def cancel_before_start(self, _activity_id: str) -> bool:
        """模拟提交前回滚预留。

        Args:
            _activity_id: 需要释放的 Activity 编号。

        Returns:
            固定返回真，表示占位已经释放。
        """

        return True


def _build_agent(client: RecordingClient) -> ForegroundTurnProcessor:
    """构造只包含工具续写所需依赖的 ForegroundTurnProcessor。

    Args:
        client: 记录第二轮请求的测试客户端。

    Returns:
        可直接调用工具分支的测试实例。
    """

    agent = build_foreground_processor_for_test(
        client=client,
        session_context=SessionContext(token_counter=len),
        foreground_tools=InlineToolService(),
        cognitive_activity_executor=ActivityCommitExecutor(),
        background_agent_activity_executor=ActivityCommitExecutor(),
    )
    agent._batch_waiting_phrase_events = MethodType(  # type: ignore[method-assign]
        lambda _self, **_kwargs: [],
        agent,
    )
    return agent


def _handle_calls(
    agent: ForegroundTurnProcessor,
    *,
    tool_calls: list[ForegroundToolCall],
    visible_parts: list[str],
    emit_batch_waiting_phrase: bool = False,
    cancellation_event: Event | None = None,
    prompt_environment: dict[str, object] | None = None,
) -> Iterator[object]:
    """使用统一测试上下文执行一组工具调用。

    Args:
        agent: 被测 ForegroundTurnProcessor。
        tool_calls: 本轮模型返回的工具调用。
        visible_parts: 工具调用前已经展示的文字。
        emit_batch_waiting_phrase: 是否启用批次等待句。
        cancellation_event: 可选 generation 取消信号。
        prompt_environment: 可选的本轮动态产品态势。

    Returns:
        Foreground 工具事件迭代器。
    """

    raw_events = agent._handle_tool_calls(
        tool_calls=tool_calls,
        character_card=CHARACTER_CARD,
        streamvox_model="",
        current_user_text="测试请求",
        current_time="2026-09-05 13:25:00 UTC+08:00",
        current_user_message=None,
        interaction_session_id="session-a",
        execution_context=ForegroundExecutionContext(
            available_tools=(),
            prompt_environment=prompt_environment or {},
        ),
        environment_snapshot=None,
        visible_parts=visible_parts,
        visible_transition_kind=(
            VisibleTransitionKind.MODEL_PREFIX
            if visible_parts
            else VisibleTransitionKind.NONE
        ),
        emit_batch_waiting_phrase=emit_batch_waiting_phrase,
        cancellation_event=cancellation_event,
        request_trace=ForegroundRequestTrace(
            request_id="request-a",
            generation_id="generation-a",
            request_source="text_input",
        ),
        tool_followup_visible_dialogue=(),
    )
    yield from _commit_tool_events(raw_events)


def _install_accepting_background_executor(
    agent: ForegroundTurnProcessor,
) -> None:
    """给 Processor 安装会真实走 canonical reservation 的后台测试执行器。

    Args:
        agent: 需要模拟 Background Work 接单成功的 Processor。
    """

    class AcceptingBackgroundExecutor:
        """用固定 Activity 实现 prepare、commit 后启动和提交前回滚。"""

        def __init__(self) -> None:
            self.prepared = False

        def prepare(self, **_kwargs: object) -> tuple[bool, list[dict[str, object]]]:
            """占住唯一测试 Activity。"""

            self.prepared = True
            return True, [
                {
                    "type": "background_agent.prepared",
                    "payload": {"activity_id": "agent-a"},
                }
            ]

        def commit_prepared(self, activity_id: str) -> ExecutionSlotSubmissionOutcome:
            """模拟 Scheduler 在 after_commit 后让 Runner 接管任务。

            Args:
                activity_id: prepare 阶段分配的 Activity 编号。

            Returns:
                表示任务已经启动的 canonical Scheduler 事实。
            """

            assert activity_id == "agent-a"
            assert self.prepared is True
            self.prepared = False
            return ExecutionSlotSubmissionOutcome(
                kind="started",
                activity_id=activity_id,
            )

        def cancel_before_start(self, activity_id: str) -> bool:
            """模拟提交失败时释放尚未启动的占位。"""

            assert activity_id == "agent-a"
            was_prepared = self.prepared
            self.prepared = False
            return was_prepared

    background_executor = AcceptingBackgroundExecutor()
    agent._background_agent_activity_executor = background_executor
    agent._background_agent_tool_executor = BackgroundAgentToolExecutor(
        background_executor
    )


def _commit_tool_events(events: Iterable[object]) -> Iterator[object]:
    """按生产输出车道的顺序提交工具完成事件。

    Args:
        events: Foreground 逐条产生的事件迭代器。

    Yields:
        原样返回每条事件，并在继续拉取前提交工具事务。
    """

    for event in events:
        yield event
        action = getattr(event, "commit_action", None)
        if (
            getattr(event, "type", "") == "llm.foreground.tool_call_completed"
            and action is not None
        ):
            # 生产输出车道会在拉取下一个事件前完成提交；测试也必须保持同样顺序，
            # 否则二轮会在 after_commit 最终事实产生前错误地做决策。
            assert action.accept() is True
            assert action.commit_enqueued() is True
            assert action.run_after_commit() is True


def _tool_call(
    tool_call_id: str,
    name: str,
    arguments: dict[str, object] | None = None,
) -> ForegroundToolCall:
    """构造测试工具调用。

    Args:
        tool_call_id: 工具调用编号。
        name: 工具名称。
        arguments: 可选工具参数。

    Returns:
        Foreground 工具调用对象。
    """

    resolved_arguments = arguments
    if resolved_arguments is None and name == "start_background_work":
        resolved_arguments = {
            "goal": "完成测试后台任务",
            "deliverable": True,
        }
    return ForegroundToolCall(
        tool_call_id=tool_call_id,
        name=name,
        arguments=resolved_arguments or {},
    )


def _final_text(events: list[object]) -> str:
    """读取事件列表中的最终助手文本。

    Args:
        events: Foreground 事件列表。

    Returns:
        `assistant.text_final` 的文本。
    """

    finals = [
        event.payload["text"]
        for event in events
        if getattr(event, "type", "") == "assistant.text_final"
    ]
    assert len(finals) == 1
    return str(finals[0])


def _accept_final(events: list[object]) -> None:
    """模拟 generation supervisor 接受唯一 final 事件。

    Args:
        events: ForegroundTurnProcessor 直接产生的事件列表。
    """

    finals = [
        event
        for event in events
        if getattr(event, "type", "") == "assistant.text_final"
    ]
    assert len(finals) == 1
    action = getattr(finals[0], "commit_action", None)
    assert action is not None
    assert action.accept() is True
    assert action.commit_enqueued() is True
    action.run_after_commit()


def _accept_response_events(events: list[object]) -> None:
    """按公开顺序提交消息分段和最终回复事务。

    Args:
        events: ForegroundTurnProcessor 直接产生的事件列表。
    """

    response_events = [
        event
        for event in events
        if getattr(event, "type", "")
        in {"assistant.text_segment_final", "assistant.text_final"}
    ]
    assert response_events
    for event in response_events:
        action = getattr(event, "commit_action", None)
        assert action is not None
        assert action.accept() is True
        assert action.commit_enqueued() is True
        action.run_after_commit()
