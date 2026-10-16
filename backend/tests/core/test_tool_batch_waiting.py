from __future__ import annotations

from pathlib import Path

import pytest

from companion.foreground_tools.catalog.metadata import (
    ToolFamilyMetadata,
    ToolLatencyClass,
    ToolMetadata,
)
from companion.foreground_tools.catalog.waiting_policy import (
    MIXED_TOOL_WAITING_PHRASE,
    MULTI_INLINE_WAITING_PHRASE,
    ToolWaitingPolicyResolver,
    ToolWaitingPolicyStore,
)
from companion.llm.foreground_turn_processor import ForegroundTurnProcessor
from backend.tests.foreground_processor_factory import (
    build_foreground_processor_for_test,
)
from companion.llm.foreground_models import ForegroundToolCall, TTS_DELIVERY_IMMEDIATE


WEATHER_FAMILY = ToolFamilyMetadata(
    family_id="weather",
    display_name="天气",
    latency_class=ToolLatencyClass.EXTERNAL_API,
    waiting_feedback_eligible=True,
    default_waiting_enabled=True,
    default_waiting_phrases=["好，我查一下天气。"],
)
AIR_FAMILY = ToolFamilyMetadata(
    family_id="air",
    display_name="空气",
    latency_class=ToolLatencyClass.EXTERNAL_API,
    waiting_feedback_eligible=True,
    default_waiting_enabled=True,
    default_waiting_phrases=["好，我查一下空气质量。"],
)
BACKGROUND_AGENT_FAMILY = ToolFamilyMetadata(
    family_id="background_agent",
    display_name="后台智能体",
    latency_class=ToolLatencyClass.LONG_RUNNING,
    waiting_feedback_eligible=False,
    default_waiting_enabled=False,
    default_waiting_phrases=[],
)

TOOL_METADATA = {
    "weather_now": ToolMetadata(
        name="weather_now",
        display_name="实时天气",
        family_id="weather",
        family_display_name="天气",
        latency_class=ToolLatencyClass.EXTERNAL_API,
        capability_description="查询实时天气。",
        self_reflection_safe=True,
    ),
    "weather_forecast": ToolMetadata(
        name="weather_forecast",
        display_name="天气预报",
        family_id="weather",
        family_display_name="天气",
        latency_class=ToolLatencyClass.EXTERNAL_API,
        capability_description="查询天气预报。",
        self_reflection_safe=True,
    ),
    "air_quality": ToolMetadata(
        name="air_quality",
        display_name="空气质量",
        family_id="air",
        family_display_name="空气",
        latency_class=ToolLatencyClass.EXTERNAL_API,
        capability_description="查询空气质量。",
        self_reflection_safe=False,
    ),
    "start_background_work": ToolMetadata(
        name="start_background_work",
        display_name="派发后台任务",
        family_id="background_agent",
        family_display_name="后台智能体",
        latency_class=ToolLatencyClass.LONG_RUNNING,
        capability_description="提交后台工作。",
        self_reflection_safe=True,
    ),
}
FAMILY_METADATA = {
    "weather": WEATHER_FAMILY,
    "air": AIR_FAMILY,
    "background_agent": BACKGROUND_AGENT_FAMILY,
}


class BatchToolCatalog:
    """为批次等待测试提供稳定的工具和工具族目录。"""

    def tool_metadata(self, name: str) -> ToolMetadata:
        """返回指定工具元数据。

        Args:
            name: 工具名称。

        Returns:
            对应的工具元数据。
        """

        return TOOL_METADATA[name]

    def tool_family_metadata(self, family_id: str) -> ToolFamilyMetadata:
        """返回指定工具族元数据。

        Args:
            family_id: 工具族编号。

        Returns:
            对应的工具族元数据。
        """

        return FAMILY_METADATA[family_id]


@pytest.mark.parametrize(
    ("tool_names", "expected_text", "expected_mode"),
    [
        (
            ["weather_now", "weather_forecast"],
            "好，我查一下天气。",
            "single_inline",
        ),
        (
            ["weather_now", "air_quality"],
            MULTI_INLINE_WAITING_PHRASE,
            "multi_inline",
        ),
        (
            ["start_background_work", "weather_now"],
            MIXED_TOOL_WAITING_PHRASE,
            "mixed",
        ),
        (["start_background_work"], None, "none"),
    ],
)
def test_batch_waiting_resolves_one_phrase_for_complete_tool_batch(
    tmp_path: Path,
    tool_names: list[str],
    expected_text: str | None,
    expected_mode: str,
) -> None:
    """整批工具只能解析出一句符合批次组成的等待反馈。"""

    catalog = BatchToolCatalog()
    resolver = ToolWaitingPolicyResolver(
        store=ToolWaitingPolicyStore(tmp_path / "waiting.json")
    )

    decision = resolver.resolve_batch(
        tool_names=tool_names,
        is_background_tool=lambda name: name == "start_background_work",
        metadata_resolver=catalog.tool_metadata,
        family_metadata_resolver=catalog.tool_family_metadata,
    )

    assert decision.text == expected_text
    assert decision.mode == expected_mode


def test_multi_family_batch_uses_builtin_phrase_even_if_one_family_is_disabled(
    tmp_path: Path,
) -> None:
    """多工具族等待句描述整批行动，不受单个工具族关闭配置影响。"""

    catalog = BatchToolCatalog()
    store = ToolWaitingPolicyStore(tmp_path / "waiting.json")
    store.save_family_policy("weather", enabled=False, phrases=[])
    resolver = ToolWaitingPolicyResolver(store=store)

    decision = resolver.resolve_batch(
        tool_names=["weather_now", "air_quality"],
        is_background_tool=lambda _name: False,
        metadata_resolver=catalog.tool_metadata,
        family_metadata_resolver=catalog.tool_family_metadata,
    )

    assert decision.text == MULTI_INLINE_WAITING_PHRASE
    assert decision.mode == "multi_inline"


def test_background_family_ignores_stale_waiting_override(tmp_path: Path) -> None:
    """历史配置不能重新开启后台工具族的固定等待句。"""

    store = ToolWaitingPolicyStore(tmp_path / "waiting.json")
    store.save_family_policy("background_agent", enabled=True, phrases=["旧等待句"])
    resolver = ToolWaitingPolicyResolver(store=store)

    policy = resolver.resolve(
        metadata=TOOL_METADATA["start_background_work"],
        family_metadata=BACKGROUND_AGENT_FAMILY,
    )

    assert policy.enabled is False
    assert policy.phrases == []
    assert policy.source == "disabled"


@pytest.mark.parametrize(
    "tool_names",
    [
        ["weather_now"],
        ["weather_now", "air_quality"],
        ["start_background_work", "weather_now"],
    ],
)
def test_visible_model_prefix_suppresses_every_batch_waiting_mode(
    tmp_path: Path,
    tool_names: list[str],
) -> None:
    """模型前缀已经可见时，单族、多族和混合批次都不能再播固定句。"""

    agent = build_foreground_processor_for_test()
    agent._foreground_tools = BatchToolCatalog()
    agent._tool_waiting_policy_resolver = ToolWaitingPolicyResolver(
        store=ToolWaitingPolicyStore(tmp_path / "waiting.json")
    )
    visible_parts = ["我来处理一下。"]

    events = agent._batch_waiting_phrase_events(
        tool_calls=[
            ForegroundToolCall(
                tool_call_id=f"call-{index}",
                name=name,
                arguments={},
            )
            for index, name in enumerate(tool_names)
        ],
        visible_parts=visible_parts,
    )

    assert events == []
    assert visible_parts == ["我来处理一下。"]


def test_mixed_batch_emits_one_immediate_tts_chunk(tmp_path: Path) -> None:
    """无模型前缀的混合批次只产生一个立即播放事件。"""

    agent = build_foreground_processor_for_test()
    agent._foreground_tools = BatchToolCatalog()
    agent._tool_waiting_policy_resolver = ToolWaitingPolicyResolver(
        store=ToolWaitingPolicyStore(tmp_path / "waiting.json")
    )
    visible_parts: list[str] = []

    events = agent._batch_waiting_phrase_events(
        tool_calls=[
            ForegroundToolCall("agent-a", "start_background_work", {}),
            ForegroundToolCall("weather-a", "weather_now", {}),
        ],
        visible_parts=visible_parts,
    )

    assert len(events) == 1
    assert events[0].payload == {
        "text": MIXED_TOOL_WAITING_PHRASE,
        "tts_delivery": TTS_DELIVERY_IMMEDIATE,
    }
    assert visible_parts == [MIXED_TOOL_WAITING_PHRASE]
