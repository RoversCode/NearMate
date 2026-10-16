from __future__ import annotations

from queue import Empty
from threading import Event
from types import SimpleNamespace
from typing import cast

import pytest

from companion.character_cards.models import CharacterCardDefinition
from companion.foreground_self_reflection.contracts import (
    SelfReflectionInterruptionReplyContext,
    SelfReflectionRecoveryContext,
)
from companion.foreground_self_reflection.prompt import RecoveryPromptFacts
from companion.llm.foreground_models import ForegroundEvent
from companion.llm.response_commit import ForegroundResponseCommit
from companion.realtime.assistant_events import AssistantRequest
from companion.realtime.assistant_generation import (
    AssistantRequestWorkItem,
    TtsGenerationFinished,
    TtsSegmentWorkItem,
)
from companion.realtime.assistant_output_lanes import AssistantOutputLanes


class NumericAgent:
    """按跨增量形式输出合法标签和带数字的完整台词。"""

    def stream_user_text(self, _text: str, **_kwargs: object):  # type: ignore[no-untyped-def]
        """产生用于验证句级正则化的固定事件。"""

        chunks = ("[laughing] Kimi K2.5", "在2010年发布，", "价格520元。")
        for chunk in chunks:
            yield ForegroundEvent(type="assistant.text_chunk", payload={"text": chunk})
        yield ForegroundEvent(
            type="assistant.text_final",
            payload={"text": "".join(chunks)},
        )


class UrlAgent:
    """跨增量输出带查询参数 URL 的测试 Agent。"""

    def stream_user_text(self, _text: str, **_kwargs: object):  # type: ignore[no-untyped-def]
        """产生真实 URL 可见文本和最终事件。"""

        chunks = (
            "请看 h",
            "ttps://example.com/search?q=a",
            ";b.c&x=1，然后告诉我。",
        )
        for chunk in chunks:
            yield ForegroundEvent(type="assistant.text_chunk", payload={"text": chunk})
        yield ForegroundEvent(
            type="assistant.text_final",
            payload={"text": "".join(chunks)},
        )


class IncompleteUrlAgent:
    """输出在 HTTP scheme 中途结束的测试 Agent。"""

    def stream_user_text(self, _text: str, **_kwargs: object):  # type: ignore[no-untyped-def]
        """产生保留可见原文、但不得原样播报的尾部候选。"""

        text = "请看 https:"
        yield ForegroundEvent(type="assistant.text_chunk", payload={"text": text})
        yield ForegroundEvent(type="assistant.text_final", payload={"text": text})


class DirectRecoveryFinalAgent:
    """模拟适配器直接发 Recovery final、没有先发 text_chunk 的异常形态。"""

    def stream_user_text(self, _text: str, **_kwargs: object):  # type: ignore[no-untyped-def]
        """只产生一条非空 final，验证输出层补齐首正文交接。"""

        yield ForegroundEvent(
            type="assistant.text_final",
            payload={"text": "我已经重新梳理好了。"},
        )


class SegmentedReplyAgent:
    """输出两条属于同一 generation 的独立角色消息。"""

    def stream_user_text(self, _text: str, **_kwargs: object):  # type: ignore[no-untyped-def]
        """先结束接单前缀，再继续输出工具拒绝说明。"""

        prefix = "我先去处理这件事情，请稍等一下。"
        rejection = "不过后台工作尚未配置，请先去 Console 完成设置。"
        yield ForegroundEvent(type="assistant.text_chunk", payload={"text": prefix})
        yield ForegroundEvent(
            type="assistant.text_segment_final",
            payload={"text": prefix},
        )
        yield ForegroundEvent(type="assistant.text_chunk", payload={"text": rejection})
        yield ForegroundEvent(type="assistant.text_final", payload={"text": rejection})


class DeferredMemoryCommitAgent:
    """只在 final 被接受时才把记忆提交附到原始事件载荷。"""

    def __init__(self) -> None:
        """初始化待提交的原始载荷。"""

        self.payload: dict[str, object] | None = None

    def stream_user_text(self, _text: str, **_kwargs: object):  # type: ignore[no-untyped-def]
        """产生一个模拟普通 assistant final 的延迟提交事件。"""

        self.payload = {"text": "这条回复必须进入历史"}
        response_commit = ForegroundResponseCommit.create(
            kind="assistant",
            visible_text="这条回复必须进入历史",
            on_accept=lambda: self.payload.__setitem__(
                "memory_commit",
                {"request_id": "request-memory"},
            ),
        )
        yield ForegroundEvent(
            type="assistant.text_final",
            payload=self.payload,
            commit_action=response_commit.action,
        )


class RecordingTtsProvider:
    """记录是否收到过期 TTS 工作项的测试 provider。"""

    def __init__(self, model: str) -> None:
        """初始化模型配置和空调用记录。

        Args:
            model: 当前 provider 模型名。
        """

        self.config = SimpleNamespace(model=model)
        self.calls: list[str] = []

    def stream_segment(self, text: str, _generation_id: str):  # type: ignore[no-untyped-def]
        """记录合成请求并返回空音频流。

        Args:
            text: 待合成句段。
            _generation_id: 当前 generation 标识。

        Returns:
            不产生音频块的空迭代器。
        """

        self.calls.append(text)
        return iter(())


class ForcedFallbackAgent:
    """让 Recovery 和辅助回应都停在取消点，验证兜底的真实输出交接。"""

    def __init__(self) -> None:
        """初始化两个 worker 的启动观察点。"""

        self.recovery_started = Event()
        self.auxiliary_started = Event()

    def stream_user_text(self, _text: str, **kwargs: object):  # type: ignore[no-untyped-def]
        """模拟模型持续思考，直到对应输出域收到取消。"""

        request_mode = kwargs.get("request_mode")
        cancellation_event = kwargs["cancellation_event"]
        if request_mode == "self_reflection_recovery":
            self.recovery_started.set()
            cancellation_event.wait(timeout=5.0)  # type: ignore[union-attr]
            return

        self.auxiliary_started.set()
        yield ForegroundEvent(
            type="assistant.text_chunk",
            payload={"text": "我听到了。"},
        )
        cancellation_event.wait(timeout=5.0)  # type: ignore[union-attr]

    def create_runtime_fallback_event(
        self,
        text: str,
        *,
        request_id: str,
        history_policy: str,
        mark_notice_delivery_failed: bool = True,
    ) -> ForegroundEvent:
        """创建测试所需的正常 Recovery final 事件。"""

        _ = request_id, history_policy, mark_notice_delivery_failed
        return ForegroundEvent(type="assistant.text_final", payload={"text": text})

    def create_interrupted_response_event(
        self,
        display_text: str,
        *,
        model_text: str,
        request_id: str,
        history_policy: str,
        progress_references: tuple[object, ...],
        interruption_reason: str = "",
    ) -> ForegroundEvent:
        """创建辅助回应被下一条输入取消时的 episode final。"""

        _ = model_text, request_id, history_policy, progress_references, interruption_reason
        return ForegroundEvent(
            type="assistant.text_final",
            payload={"text": display_text},
        )

    def warmup_foreground(self) -> None:
        """测试替身不需要预热模型。"""


def _recovery_context() -> SelfReflectionRecoveryContext:
    """构造可触发确定性 Recovery fallback 的冻结上下文。"""

    return SelfReflectionRecoveryContext(
        request_id="self-reflection-recovery-reflection-fallback",
        self_reflection_id="reflection-fallback",
        episode_id="episode-fallback",
        runtime_epoch=1,
        source_activity_id="cognitive-fallback",
        source_request_id="source-request-fallback",
        source_protocol_transaction_id="source-transaction-fallback",
        deadline=45.0,
        prompt_facts=RecoveryPromptFacts(
            original_user_request="整理文件",
            previous_method_capability="只做分析",
            failure_explanation="旧方法无法完成整理。",
            tools=(),
        ),
        fallback_text="我重新梳理时出了点问题，暂时没能处理好。",
    )


def _interruption_context() -> SelfReflectionInterruptionReplyContext:
    """构造同一 episode 下的辅助回应上下文。"""

    return SelfReflectionInterruptionReplyContext(
        request_id="reply-fallback",
        self_reflection_id="reflection-fallback",
        episode_id="episode-fallback",
        runtime_epoch=1,
        trigger_input_identity="input-fallback",
        instruction="只确认听到用户，不执行用户内容。",
        fallback_text="我听到了，我还在重新梳理。",
    )


def test_recovery_fallback_closes_auxiliary_before_visible_output_and_settlement() -> None:
    """deadline fallback 必须先收口辅助回应，再完成正文、TTS 和 settlement。"""

    agent = ForcedFallbackAgent()
    runner = AssistantOutputLanes(
        agent=agent,  # type: ignore[arg-type]
        tts_provider=RecordingTtsProvider("qwen3-tts-clone-0.6b-gguf"),  # type: ignore[arg-type]
        now=lambda: 1.0,
    )
    recovery_context = _recovery_context()
    recovery_request = AssistantRequest(
        request_id="recovery-fallback-request",
        source_session_id="session-fallback",
        interaction_session_id="interaction-fallback",
        character_card=cast(CharacterCardDefinition, None),
        user_call_name="用户",
        user_self_intro="",
        user_extra_notes="",
        user_text="整理文件",
        source="self_reflection_recovery",
        source_turn_id=None,
        requested_at=1.0,
        history_policy="stable_dialogue",
        self_reflection_context=recovery_context,
    )
    interruption_request = AssistantRequest(
        request_id="reply-fallback",
        source_session_id="session-fallback",
        interaction_session_id="interaction-fallback",
        character_card=cast(CharacterCardDefinition, None),
        user_call_name="用户",
        user_self_intro="",
        user_extra_notes="",
        user_text="先听我说",
        source="self_reflection_interruption_reply",
        source_turn_id=None,
        requested_at=1.0,
        history_policy="unattended_episode",
        self_reflection_context=_interruption_context(),
    )

    runner.start()
    try:
        runner.start_request(recovery_request)
        assert agent.recovery_started.wait(timeout=2.0)
        runner.start_request(interruption_request)
        assert agent.auxiliary_started.wait(timeout=2.0)

        assert runner.request_recovery_fallback(
            self_reflection_id="reflection-fallback",
            reason="reflection_deadline_exceeded",
        ) is True
        recovery_generation = runner._supervisor.current_context(lane="primary")  # type: ignore[attr-defined]
        assert recovery_generation is not None
        assert recovery_generation.completion_event.wait(timeout=3.0)

        events = runner.drain_events(limit=200)
        event_types = [event.type for event in events]
        close_index = event_types.index("foreground.self_reflection_reply_admission_closed")
        fallback_chunk_index = next(
            index
            for index, event in enumerate(events)
            if event.type == "assistant.text_chunk"
            and event.payload.get("request_id") == "recovery-fallback-request"
        )
        assert close_index < fallback_chunk_index
        assert any(
            event.type == "assistant.generation_interrupted"
            and event.payload.get("request_id") == "reply-fallback"
            for event in events
        )
        assert any(
            event.type == "assistant.audio_finished"
            and event.payload.get("request_id") == "recovery-fallback-request"
            for event in events
        )
        recovery_settlements = [
            event
            for event in events
            if event.type == "assistant.output_settled"
            and event.payload.get("request_id") == "recovery-fallback-request"
        ]
        assert len(recovery_settlements) == 1
        assert recovery_settlements[0].payload["outcome"] == "completed"
    finally:
        runner.shutdown(timeout=2.0)


def test_direct_recovery_final_closes_auxiliary_and_enters_tts() -> None:
    """异常直出 final 也必须先关闭辅助域，再公开正文并送入 TTS。"""

    runner = AssistantOutputLanes(
        agent=DirectRecoveryFinalAgent(),  # type: ignore[arg-type]
        tts_provider=RecordingTtsProvider("qwen3-tts-clone-0.6b-gguf"),  # type: ignore[arg-type]
        now=lambda: 1.0,
    )
    request = AssistantRequest(
        request_id="recovery-direct-final",
        source_session_id="session-direct-final",
        interaction_session_id="interaction-direct-final",
        character_card=cast(CharacterCardDefinition, None),
        user_call_name="用户",
        user_self_intro="",
        user_extra_notes="",
        user_text="重新处理",
        source="self_reflection_recovery",
        source_turn_id=None,
        requested_at=1.0,
        history_policy="stable_dialogue",
        self_reflection_context=_recovery_context(),
    )

    runner.start_request(request)
    runner.event_queue.get_nowait()
    work_item = runner._request_queue.get_nowait()  # type: ignore[attr-defined]
    assert isinstance(work_item, AssistantRequestWorkItem)

    runner._run_llm_generation(work_item.context)  # type: ignore[attr-defined]

    events = runner.drain_events(limit=100)
    event_types = [event.type for event in events]
    close_index = event_types.index("foreground.self_reflection_reply_admission_closed")
    chunk_index = next(
        index
        for index, event in enumerate(events)
        if event.type == "assistant.text_chunk"
        and event.payload.get("request_id") == "recovery-direct-final"
    )
    assert close_index < chunk_index
    assert any(
        event.type == "assistant.text_final"
        and event.payload.get("text") == "我已经重新梳理好了。"
        for event in events
    )
    tts_items = []
    while True:
        try:
            tts_items.append(runner._tts_queue.get_nowait())  # type: ignore[attr-defined]
        except Empty:
            break
    assert [
        item.text for item in tts_items if isinstance(item, TtsSegmentWorkItem)
    ] == ["我已经重新梳理好了。"]
    assert isinstance(tts_items[-1], TtsGenerationFinished)


def test_segmented_reply_flushes_each_message_to_tts_without_ending_generation() -> None:
    """消息分段要重置切句状态，让第二条回复仍能尽早进入 TTS。"""

    runner = AssistantOutputLanes(
        agent=SegmentedReplyAgent(),  # type: ignore[arg-type]
        tts_provider=SimpleNamespace(  # type: ignore[arg-type]
            config=SimpleNamespace(model="qwen3-tts-clone-0.6b-gguf")
        ),
        now=lambda: 1.0,
    )
    request = AssistantRequest(
        request_id="segment-a",
        source_session_id="desktop-text",
        interaction_session_id="interaction-a",
        character_card=cast(CharacterCardDefinition, None),
        user_call_name="用户",
        user_self_intro="",
        user_extra_notes="",
        user_text="请处理",
        source="text_input",
        source_turn_id=None,
        requested_at=1.0,
    )
    runner._supervisor.start_generation(request)  # type: ignore[attr-defined]
    runner.event_queue.get_nowait()
    work_item = runner._request_queue.get_nowait()  # type: ignore[attr-defined]
    assert isinstance(work_item, AssistantRequestWorkItem)

    runner._run_llm_generation(work_item.context)  # type: ignore[attr-defined]

    tts_items = []
    while True:
        try:
            tts_items.append(runner._tts_queue.get_nowait())  # type: ignore[attr-defined]
        except Empty:
            break
    spoken = [item.text for item in tts_items if isinstance(item, TtsSegmentWorkItem)]
    assert spoken == [
        "我先去处理这件事情，请稍等一下。",
        "不过后台工作尚未配置，请先去 Console 完成设置。",
    ]
    assert isinstance(tts_items[-1], TtsGenerationFinished)


def test_final_event_keeps_memory_commit_attached_after_accept() -> None:
    """final 事件提交前追加的 MemoryCommit 不能因载荷复制而丢失。"""

    agent = DeferredMemoryCommitAgent()
    runner = AssistantOutputLanes(
        agent=agent,  # type: ignore[arg-type]
        tts_provider=SimpleNamespace(
            config=SimpleNamespace(model="qwen3-tts-clone-0.6b-gguf")
        ),  # type: ignore[arg-type]
        now=lambda: 1.0,
    )
    request = AssistantRequest(
        request_id="request-memory",
        source_session_id="session-memory",
        interaction_session_id="interaction-memory",
        character_card=cast(CharacterCardDefinition, None),
        user_call_name="用户",
        user_self_intro="",
        user_extra_notes="",
        user_text="请回复",
        source="text_input",
        source_turn_id=None,
        requested_at=1.0,
    )
    runner._supervisor.start_generation(request)  # type: ignore[attr-defined]
    runner.event_queue.get_nowait()
    work_item = runner._request_queue.get_nowait()  # type: ignore[attr-defined]
    assert isinstance(work_item, AssistantRequestWorkItem)

    runner._run_llm_generation(work_item.context)  # type: ignore[attr-defined]

    final_event = None
    while True:
        try:
            event = runner.event_queue.get_nowait()
        except Empty:
            break
        if event.type == "assistant.text_final":
            final_event = event
    assert final_event is not None
    assert agent.payload is not None
    assert "memory_commit" in agent.payload
    assert final_event.payload["memory_commit"] == {
        "request_id": "request-memory"
    }


@pytest.mark.parametrize(
    ("model", "expected_prefix"),
    [
        ("qwen3-tts-clone-0.6b-gguf", ""),
        ("moss-tts-nano-onnx", ""),
        ("s2-pro-4b-gguf", "[laughing] "),
        ("voxcpm2-gguf", "[laughing] "),
    ],
)
def test_assistant_lane_normalizes_each_complete_sentence_once(
    model: str,
    expected_prefix: str,
) -> None:
    """实时链路必须保留可见文本并只向 TTS 投递句级正则化结果。

    Args:
        model: 本轮 generation 使用的 StreamVox 模型。
        expected_prefix: 当前模型合法标签清洗后应保留的控制前缀。
    """

    runner = AssistantOutputLanes(
        agent=NumericAgent(),  # type: ignore[arg-type]
        tts_provider=SimpleNamespace(config=SimpleNamespace(model=model)),  # type: ignore[arg-type]
        now=lambda: 1.0,
    )
    request = AssistantRequest(
        request_id="request-a",
        source_session_id="session-a",
        interaction_session_id="interaction-a",
        character_card=cast(CharacterCardDefinition, None),
        user_call_name="用户",
        user_self_intro="",
        user_extra_notes="",
        user_text="介绍一下",
        source="system_notice",
        source_turn_id=None,
        requested_at=1.0,
    )
    runner._supervisor.start_generation(request)  # type: ignore[attr-defined]
    runner.event_queue.get_nowait()
    work_item = runner._request_queue.get_nowait()  # type: ignore[attr-defined]
    assert isinstance(work_item, AssistantRequestWorkItem)

    runner._run_llm_generation(work_item.context)  # type: ignore[attr-defined]

    visible_chunks: list[str] = []
    while True:
        try:
            event = runner.event_queue.get_nowait()
        except Empty:
            break
        if event.type == "assistant.text_chunk":
            visible_chunks.append(str(event.payload["text"]))

    tts_items: list[object] = []
    while True:
        try:
            tts_items.append(runner._tts_queue.get_nowait())  # type: ignore[attr-defined]
        except Empty:
            break

    assert "".join(visible_chunks) == "[laughing] Kimi K2.5在2010年发布，价格520元。"
    assert [
        item.text for item in tts_items if isinstance(item, TtsSegmentWorkItem)
    ] == [f"{expected_prefix}Kimi K二点五在二零一零年发布，价格五百二十元。"]
    assert isinstance(tts_items[-1], TtsGenerationFinished)


def test_tts_lane_rejects_work_item_after_model_snapshot_changes() -> None:
    """模型切换后不得把旧模型正则化的句段发送给新 provider。"""

    provider = RecordingTtsProvider("voxcpm2-gguf")
    runner = AssistantOutputLanes(
        agent=NumericAgent(),  # type: ignore[arg-type]
        tts_provider=provider,  # type: ignore[arg-type]
        now=lambda: 1.0,
    )
    request = AssistantRequest(
        request_id="request-b",
        source_session_id="session-b",
        interaction_session_id="interaction-b",
        character_card=cast(CharacterCardDefinition, None),
        user_call_name="用户",
        user_self_intro="",
        user_extra_notes="",
        user_text="介绍一下",
        source="system_notice",
        source_turn_id=None,
        requested_at=1.0,
    )
    generation_id = runner._supervisor.start_generation(request)  # type: ignore[attr-defined]
    runner.event_queue.get_nowait()
    context = runner._supervisor.current_context()  # type: ignore[attr-defined]
    assert context is not None
    assert context.generation_id == generation_id

    runner._run_tts_segment(  # type: ignore[attr-defined]
        TtsSegmentWorkItem(
            context=context,
            text="旧模型句段",
            streamvox_model="s2-pro-4b-gguf",
        )
    )

    assert provider.calls == []


def test_assistant_lane_keeps_visible_url_but_replaces_tts_text() -> None:
    """聊天事件必须保留真实 URL，TTS 工作项只能收到“链接”。"""

    runner = AssistantOutputLanes(
        agent=UrlAgent(),  # type: ignore[arg-type]
        tts_provider=SimpleNamespace(
            config=SimpleNamespace(model="voxcpm2-gguf")
        ),  # type: ignore[arg-type]
        now=lambda: 1.0,
    )
    request = AssistantRequest(
        request_id="request-url",
        source_session_id="session-url",
        interaction_session_id="interaction-url",
        character_card=cast(CharacterCardDefinition, None),
        user_call_name="用户",
        user_self_intro="",
        user_extra_notes="",
        user_text="看看链接",
        source="system_notice",
        source_turn_id=None,
        requested_at=1.0,
    )
    runner._supervisor.start_generation(request)  # type: ignore[attr-defined]
    runner.event_queue.get_nowait()
    work_item = runner._request_queue.get_nowait()  # type: ignore[attr-defined]
    assert isinstance(work_item, AssistantRequestWorkItem)

    runner._run_llm_generation(work_item.context)  # type: ignore[attr-defined]

    visible_chunks: list[str] = []
    while True:
        try:
            event = runner.event_queue.get_nowait()
        except Empty:
            break
        if event.type == "assistant.text_chunk":
            visible_chunks.append(str(event.payload["text"]))
    tts_texts: list[str] = []
    while True:
        try:
            item = runner._tts_queue.get_nowait()  # type: ignore[attr-defined]
        except Empty:
            break
        if isinstance(item, TtsSegmentWorkItem):
            tts_texts.append(item.text)

    assert "".join(visible_chunks) == (
        "请看 https://example.com/search?q=a;b.c&x=1，然后告诉我。"
    )
    assert tts_texts == ["请看链接，然后告诉我。"]


def test_assistant_lane_keeps_incomplete_url_visible_but_hides_it_from_tts() -> None:
    """未完成 URL 可以保留在聊天原文，但 TTS 工作项只能收到“链接”。"""

    runner = AssistantOutputLanes(
        agent=IncompleteUrlAgent(),  # type: ignore[arg-type]
        tts_provider=SimpleNamespace(
            config=SimpleNamespace(model="voxcpm2-gguf")
        ),  # type: ignore[arg-type]
        now=lambda: 1.0,
    )
    request = AssistantRequest(
        request_id="request-incomplete-url",
        source_session_id="session-url",
        interaction_session_id="interaction-url",
        character_card=cast(CharacterCardDefinition, None),
        user_call_name="用户",
        user_self_intro="",
        user_extra_notes="",
        user_text="看看链接",
        source="system_notice",
        source_turn_id=None,
        requested_at=1.0,
    )
    runner._supervisor.start_generation(request)  # type: ignore[attr-defined]
    runner.event_queue.get_nowait()
    work_item = runner._request_queue.get_nowait()  # type: ignore[attr-defined]
    assert isinstance(work_item, AssistantRequestWorkItem)

    runner._run_llm_generation(work_item.context)  # type: ignore[attr-defined]

    visible_chunks: list[str] = []
    while True:
        try:
            event = runner.event_queue.get_nowait()
        except Empty:
            break
        if event.type == "assistant.text_chunk":
            visible_chunks.append(str(event.payload["text"]))
    tts_texts: list[str] = []
    while True:
        try:
            item = runner._tts_queue.get_nowait()  # type: ignore[attr-defined]
        except Empty:
            break
        if isinstance(item, TtsSegmentWorkItem):
            tts_texts.append(item.text)

    assert "".join(visible_chunks) == "请看 https:"
    assert tts_texts == ["请看链接"]
