from __future__ import annotations

from companion.character_cards.models import CharacterCardDefinition, StreamVoxExampleScenario
from companion.prompt.capabilities import ForegroundCapabilityProfile
from companion.prompt.xml_markup import render_xml_block, render_xml_cdata_leaf, render_xml_text_leaf


def build_examples_xml_content(
    character_card: CharacterCardDefinition,
    streamvox_model: str,
    profile: ForegroundCapabilityProfile,
) -> str:
    """按角色卡和当前 StreamVox 模型构建 XML 示例对话。

    Args:
        character_card: 当前角色卡。
        streamvox_model: 当前 StreamVox 运行模型。
        profile: 本轮规范化能力画像。

    Returns:
        使用 example/user/assistant 节点表达的示例对话 XML。
    """

    return _format_streamvox_scenarios_xml(_select_example_scenarios(character_card, streamvox_model, profile))


def _select_example_scenarios(
    character_card: CharacterCardDefinition,
    streamvox_model: str,
    profile: ForegroundCapabilityProfile,
) -> list[StreamVoxExampleScenario]:
    """选择当前模型应注入的示例场景。

    Args:
        character_card: 当前角色卡。
        streamvox_model: 当前 StreamVox 运行模型。
        profile: 本轮规范化能力画像。

    Returns:
        当前模型专属、通用或高光对白兜底示例。
    """

    normalized_model = streamvox_model.strip().lower()
    streamvox_examples = character_card.voiceprint.streamvox_examples
    model_examples = streamvox_examples.models.get(normalized_model)
    if model_examples is not None and model_examples.items:
        candidates = list(model_examples.items)
    elif streamvox_examples.generic.items:
        candidates = list(streamvox_examples.generic.items)
    else:
        candidates = _character_dialogue_fallback_scenarios(character_card)

    filtered_scenarios = [
        scenario
        for scenario in candidates
        if _scenario_matches_capabilities(scenario, profile)
    ]
    if filtered_scenarios:
        return filtered_scenarios
    return _character_dialogue_fallback_scenarios(character_card)


def _scenario_matches_capabilities(
    scenario: StreamVoxExampleScenario,
    profile: ForegroundCapabilityProfile,
) -> bool:
    """判断示例依赖的隐藏动作是否在本轮可用。

    Args:
        scenario: 待筛选的结构化示例。
        profile: 本轮规范化能力画像。

    Returns:
        示例不依赖不可用工具或后台交接时返回真。
    """

    if scenario.expected_tool is not None and not profile.supports_tool(scenario.expected_tool):
        return False
    return True


def _format_streamvox_scenarios_xml(scenarios: list[StreamVoxExampleScenario]) -> str:
    """把结构化示例渲染成 XML 对话样例。

    Args:
        scenarios: 当前模型槽位选中的示例列表。

    Returns:
        可直接放入 system_prompt 的 example_dialogues XML。
    """

    example_blocks = [
        render_xml_cdata_leaf(
            "note",
            "以下是语气与何时调用工具的示范，不是真实聊天记录。"
            "expected_tool 表示该场景下应发起的工具调用，只用于示范，回复时不要输出它。",
        )
    ]
    for scenario in scenarios:
        example_blocks.append(
            render_xml_block(
                "example",
                _example_children(
                    render_xml_text_leaf("title", scenario.title),
                    render_xml_text_leaf("user", scenario.user),
                    render_xml_cdata_leaf("assistant", scenario.char),
                    expected_tool=scenario.expected_tool,
                ),
            )
        )
    return render_xml_block("example_dialogues", example_blocks)


def _example_children(title: str, user: str, assistant: str, *, expected_tool: str | None) -> list[str]:
    """组装单条示例的 XML 子节点。

    Args:
        title: 已渲染的标题节点。
        user: 已渲染的用户节点。
        assistant: 已渲染的助手节点。
        expected_tool: 该示例期望模型调用的工具名；无工具期望时为空。

    Returns:
        单条 example 的子节点列表。
    """

    children = [title, user, assistant]
    if expected_tool is not None:
        children.append(render_xml_text_leaf("expected_tool", expected_tool))
    return children


def _character_dialogue_fallback_scenarios(character_card: CharacterCardDefinition) -> list[StreamVoxExampleScenario]:
    """用角色卡高光对白构建最后兜底示例。

    Args:
        character_card: 当前角色卡。

    Returns:
        不含共享人格风格的示例场景。
    """

    scenarios = [
        StreamVoxExampleScenario(
            title=f"{character_card.meta.name}的高光对白",
            user=dialogue.user,
            char=dialogue.char,
        )
        for dialogue in character_card.voiceprint.example_dialogues.items
    ]
    if scenarios:
        return scenarios

    return [
        StreamVoxExampleScenario(
            title="保持角色本人语气",
            user="你在吗？",
            char="我在。",
        )
    ]
