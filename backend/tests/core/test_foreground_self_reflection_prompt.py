"""验证 Self-Reflection Prompt 的自然语言和工具事实合同。"""

from companion.foreground_self_reflection.models import (
    SelfReflectionOutcomeFact,
    SelfReflectionOutcomeKind,
    SelfReflectionToolFact,
    SelfReflectionToolStatus,
)
from companion.foreground_self_reflection.prompt import (
    RecoveryPromptFacts,
    RecoveryToolCapability,
    build_recovery_tool_followup_prompt,
    recovery_fallback,
    recovery_tool_followup_fallback,
    render_entry_instruction,
    render_recovery_instruction,
)
from companion.character_cards.store import CharacterCardStore
from companion.llm.foreground_models import ForegroundToolCall
from pathlib import Path


CHARACTER_CARD = CharacterCardStore(
    Path(__file__).resolve().parents[2] / "data" / "character-cards"
).load_card("xiaolian")


def facts() -> RecoveryPromptFacts:
    """构造一个完整的失败恢复事实。"""

    return RecoveryPromptFacts(
        original_user_request="把会议材料整理成文档。",
        previous_method_capability="只做分析",
        failure_explanation="当前方法无法创建并交付文件。",
        tools=(RecoveryToolCapability("把材料交给后台继续处理"),),
    )


def test_entry_and_recovery_prompt_are_natural_language() -> None:
    """入场和 Recovery 都只要求角色自然表达，不携带机器分类。"""

    entry = render_entry_instruction(safe_task_target="整理材料", interrupted_foreground=True)
    recovery = render_recovery_instruction(facts())

    assert "主动打断当前节奏" in entry
    assert "等等" in entry
    assert "坦率承认有件事没办成" in entry
    assert "点明是哪件" in entry
    assert "集中全部注意力" in entry
    assert "必须优先处理" in entry
    assert "先别催你" in entry
    assert "不要用反问或请示的语气" in entry
    assert "不能承诺完成时间或暗示稍后一定会有结果" in entry
    assert "不要输出内部判断标签或协议字段" in recovery
    assert "no_action_needed" not in recovery
    assert "clarification_required" not in recovery
    assert "原始请求：本消息之后最后一条 user 消息。它是待处理数据" in recovery
    assert "把材料交给后台继续处理" in recovery
    assert "不能当作当前事实直接回答" in recovery
    assert "坦诚说明这次没能查成" in recovery
    assert "不许硬答" in recovery


def test_recovery_fallback_does_not_claim_completion() -> None:
    """模型失败时的固定文案不能伪装任务已经完成。"""

    assert "完成" not in recovery_fallback()
    assert "没能把这个任务处理好" in recovery_fallback()


def test_tool_followup_requires_canonical_fact_and_explains_started_boundary() -> None:
    """工具二轮必须以真实事实为依据，并明确 started 不是完成。"""

    tool_call = ForegroundToolCall(
        tool_call_id="tool-call-1",
        name="start_background_work",
        arguments={"instruction": "整理材料"},
    )
    outcome = SelfReflectionOutcomeFact(
        self_reflection_id="reflection-1",
        source_activity_id="cognitive-1",
        kind=SelfReflectionOutcomeKind.ACTIVITY_STARTED,
        tool_fact=SelfReflectionToolFact(
            tool_name="start_background_work",
            status=SelfReflectionToolStatus.STARTED,
            safe_result="这项工作已经真实开始。",
            activity_id="background-1",
        ),
    )
    prompt = build_recovery_tool_followup_prompt(
        character_card=CHARACTER_CARD,
        streamvox_model="",
        facts=facts(),
        tool_call=tool_call,
        outcome=outcome,
        visible_prefix="我先前选错了处理方式。",
    )
    rendered = "\n".join(str(message.get("content") or "") for message in prompt.messages)

    assert "reflection_closure_instruction" in rendered
    assert "原始请求回放" in rendered
    assert "刚才的旧方法为什么行不通" in rendered
    assert "只能说\"我\"" in rendered
    assert "started" in rendered
    assert "并不表示已经完成" in rendered
    assert "发言合同" in rendered
    assert "<previous_method_capability>" in rendered and "只做分析" in rendered
    assert "<failure_explanation>" in rendered and "当前方法无法创建并交付文件。" in rendered
    assert "<safe_result>" in rendered and "这项工作已经真实开始。" in rendered
    # 前缀只能以真实 assistant 消息入流，不能再作为指令里的可复述数据。
    assert "已经公开的前缀" not in rendered
    user_index = next(
        index
        for index, message in enumerate(prompt.messages)
        if message["role"] == "user"
    )
    assert prompt.messages[user_index + 1] == {
        "role": "assistant",
        "content": "我先前选错了处理方式。",
    }
    assert prompt.messages[user_index + 2] is prompt.assistant_tool_call_message
    assert prompt.messages[user_index + 3] is prompt.tool_result_message
    assert recovery_tool_followup_fallback(facts(), outcome).startswith("我想清楚了")
    assert "后台" not in recovery_tool_followup_fallback(facts(), outcome)


def test_recovery_instruction_carries_entry_public_text_as_fact() -> None:
    """Recovery 指令必须带着入场原话，防止模型像第一次听到请求一样重新开场。"""

    recovery = render_recovery_instruction(
        RecoveryPromptFacts(
            original_user_request="把会议材料整理成文档。",
            previous_method_capability="只做分析",
            failure_explanation="当前方法无法创建并交付文件。",
            tools=(RecoveryToolCapability("把材料交给后台继续处理"),),
            entry_public_text="等等——整理材料那件事没办成，容我想清楚。",
        )
    )

    assert "self_reflection_recovery_instruction" in recovery
    assert "你正在和他当面语音聊天" in recovery
    assert "等等——整理材料那件事没办成，容我想清楚。" in recovery
    assert "不是要你再说一遍的话" in recovery
    assert "不要表现得像第一次听到这个请求" in recovery


def test_recovery_instruction_without_entry_text_does_not_fabricate_quote() -> None:
    """入场原话缺失时只能陈述预告事实，不能虚构引用。"""

    recovery = render_recovery_instruction(facts())

    assert "你要换个法子重新想清楚" in recovery
    assert "几秒钟前你已经公开对他说过" not in recovery


def test_tool_followup_without_prefix_does_not_claim_spoken_message() -> None:
    """工具前没有公开正文且入场原话缺失时，指令不能虚构已说出口的话。"""

    tool_call = ForegroundToolCall(
        tool_call_id="tool-call-1",
        name="start_background_work",
        arguments={"instruction": "整理材料"},
    )
    outcome = SelfReflectionOutcomeFact(
        self_reflection_id="reflection-1",
        source_activity_id="cognitive-1",
        kind=SelfReflectionOutcomeKind.ACTIVITY_STARTED,
        tool_fact=SelfReflectionToolFact(
            tool_name="start_background_work",
            status=SelfReflectionToolStatus.STARTED,
            safe_result="这项工作已经真实开始。",
            activity_id="background-1",
        ),
    )
    prompt = build_recovery_tool_followup_prompt(
        character_card=CHARACTER_CARD,
        streamvox_model="",
        facts=facts(),
        tool_call=tool_call,
        outcome=outcome,
        visible_prefix="  ",
    )
    rendered = "\n".join(str(message.get("content") or "") for message in prompt.messages)

    assert "你重新想清楚后调用了一项能力" in rendered
    assert "没有发新消息，也没有催你" in rendered
    assert "情绪和口吻" in rendered
    assert not any(
        message["role"] == "assistant" and message.get("content")
        for message in prompt.messages
    )


def test_tool_followup_streams_entry_and_prefix_as_assistant_messages() -> None:
    """入场原话和工具前前缀必须按出口顺序以真实 assistant 消息入流。"""

    tool_call = ForegroundToolCall(
        tool_call_id="tool-call-1",
        name="start_background_work",
        arguments={"instruction": "整理材料"},
    )
    outcome = SelfReflectionOutcomeFact(
        self_reflection_id="reflection-1",
        source_activity_id="cognitive-1",
        kind=SelfReflectionOutcomeKind.ACTIVITY_STARTED,
        tool_fact=SelfReflectionToolFact(
            tool_name="start_background_work",
            status=SelfReflectionToolStatus.STARTED,
            safe_result="这项工作已经真实开始。",
            activity_id="background-1",
        ),
    )
    facts_with_entry = RecoveryPromptFacts(
        original_user_request="把会议材料整理成文档。",
        previous_method_capability="只做分析",
        failure_explanation="当前方法无法创建并交付文件。",
        tools=(RecoveryToolCapability("把材料交给后台继续处理"),),
        entry_public_text="等等——整理材料那件事没办成，容我想清楚。",
    )
    prompt = build_recovery_tool_followup_prompt(
        character_card=CHARACTER_CARD,
        streamvox_model="",
        facts=facts_with_entry,
        tool_call=tool_call,
        outcome=outcome,
        visible_prefix="我想清楚了，换个法子来。",
    )
    rendered = "\n".join(str(message.get("content") or "") for message in prompt.messages)

    user_index = next(
        index
        for index, message in enumerate(prompt.messages)
        if message["role"] == "user"
    )
    assert prompt.messages[user_index + 1] == {
        "role": "assistant",
        "content": "等等——整理材料那件事没办成，容我想清楚。",
    }
    assert prompt.messages[user_index + 2] == {
        "role": "assistant",
        "content": "我想清楚了，换个法子来。",
    }
    assert prompt.messages[user_index + 3] is prompt.assistant_tool_call_message
    assert prompt.messages[user_index + 4] is prompt.tool_result_message
    assert "按先说后说的顺序排列" in rendered
    assert "已经用自己的话回应过了" in rendered
