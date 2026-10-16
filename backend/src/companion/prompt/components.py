from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from companion.character_cards.models import (
    BackgroundContextTerm,
    CharacterCardDefinition,
    RelationshipEntry,
    VoiceprintSection,
)
from companion.character_cards.store import SHARED_WORLD_CANON
from companion.prompt.capabilities import ForegroundCapabilityProfile, render_foreground_protocol
from companion.prompt.streamvox_examples import build_examples_xml_content
from companion.prompt.xml_markup import render_xml_block, render_xml_cdata_leaf, render_xml_text_leaf


@dataclass(frozen=True)
class PromptComponent:
    """描述一个可直接注入提示词的静态组件。"""

    name: str
    version: str
    role: str
    content: str


def system_prompt_component(components: list[PromptComponent]) -> PromptComponent:
    """把静态提示词组件收束成统一 XML 系统提示词。

    Args:
        components: 已按产品优先级排序的静态提示词组件。

    Returns:
        单条可注入模型的 system prompt 组件。
    """

    persona_children: list[str] = []
    protocol_children: list[str] = []
    expression_children: list[str] = []
    example_children: list[str] = []

    for component in components:
        persona_child = _character_persona_child(component)
        if persona_child is not None:
            persona_children.append(persona_child)
            continue
        if component.name == "examples":
            example_children.append(component.content)
            continue

        child = _xml_child_for_component(component)
        if component.name in {"user_profile", "response_guidelines", "foreground_law"}:
            protocol_children.append(child)
            continue
        if component.name == "streamvox_model_prompt":
            expression_children.append(child)
            continue

    sections = [
        render_xml_block("character_persona", persona_children),
        render_xml_block("system_protocols", protocol_children),
    ]
    if expression_children:
        sections.append(render_xml_block("expression_capabilities", expression_children))
    if example_children:
        sections.extend(example_children)

    return PromptComponent(
        name="system_prompt",
        version="v1",
        role="system",
        content=render_xml_block("system_prompt", sections),
    )


def character_context_prompt_component(
    components: list[PromptComponent],
) -> PromptComponent:
    """把静态角色组件收束成二轮专用角色上下文。

    Args:
        components: 已按角色优先级排序的静态角色组件。

    Returns:
        只包含角色人格且使用明确 XML 根标签的系统组件。
    """

    persona_children: list[str] = []
    for component in components:
        persona_child = _character_persona_child(component)
        if persona_child is not None:
            persona_children.append(persona_child)

    return PromptComponent(
        name="character_context",
        version="v1",
        role="system",
        content=render_xml_block(
            "character_context",
            [render_xml_block("character_persona", persona_children)],
        ),
    )


def _character_persona_child(component: PromptComponent) -> str | None:
    """把单个静态角色组件转换为人格 XML 子节点。

    Args:
        component: 待判断和渲染的提示词组件。

    Returns:
        角色组件对应的 XML；非角色组件返回空。
    """

    if component.name == "character_narrative":
        return component.content
    if component.name in {
        "character_identity",
        "world_canon",
        "character_background_context",
    }:
        return _xml_child_for_component(component)
    return None


def _xml_child_for_component(component: PromptComponent) -> str:
    """按组件职责选择 XML 标签并保护原始提示词正文。

    Args:
        component: 单个静态提示词组件。

    Returns:
        已渲染完成的 XML 子节点。
    """

    tag_name_by_component = {
        "character_identity": "identity",
        "world_canon": "shared_world_canon",
        "character_background_context": "past_world",
        "user_profile": "user_context",
        "response_guidelines": "response_guidelines",
        "foreground_law": "behavior_protocol",
        "streamvox_model_prompt": "streamvox_capabilities",
        "examples": "example_dialogues",
    }
    tag_name = tag_name_by_component.get(component.name, component.name)
    return render_xml_cdata_leaf(tag_name, component.content)


def character_identity_component(character_card: CharacterCardDefinition) -> PromptComponent:
    """创建角色身份锚定组件（Layer 1，置于最前）。

    Args:
        character_card: 当前激活角色卡。

    Returns:
        反向锚定 RLHF 默认客服化倾向、钉死"你是谁、不是谁、有什么棱角"的系统组件。
        内容来自角色卡的模型约束指令；缺省时回退到最小身份声明。
    """

    identity = character_card.model_constraints.persona_forbidden_rules.strip()
    if not identity:
        identity = f"你是{character_card.meta.name}，不是 AI 助手，也不是客服。用你自己的方式说话。"

    return PromptComponent(
        name="character_identity",
        version="v1",
        role="system",
        content=identity,
    )


def world_canon_component() -> PromptComponent:
    """创建共享世界观组件（所有角色共享的固定世界观底座）。

    Returns:
        描述当前正式世界观底座的系统组件。
        来历无关，任何角色卡都通用；角色卡只补充“她原本是谁”与“她如何活在这个世界里”。
    """

    return PromptComponent(
        name="world_canon",
        version="v1",
        role="system",
        content=SHARED_WORLD_CANON.removeprefix("[Shared World Canon]").strip(),
    )


def character_background_context_component(character_card: CharacterCardDefinition) -> PromptComponent | None:
    """创建角色背景解释组件。

    Args:
        character_card: 当前激活角色卡。

    Returns:
        有背景术语时返回系统组件；没有术语时返回 None，避免空组件占用上下文。
    """

    terms = [term for term in character_card.background_context.terms if term.name.strip() and term.explanation.strip()]
    if not terms:
        return None

    term_lines = [_format_background_context_term(term) for term in terms]
    # 用第二人称把背景设定框定为「角色自己的过去」，而不是「外部参考资料」——
    # 模型就是这张卡本身，这些术语是她骨子里有数的事。位置紧接 world_canon 的穿越设定，
    # 标题用「你曾经的世界」接住「现在穿越 / 过去来处」的叙事过渡，与 narrative 的内化闸门同调。
    content = (
        "[你曾经的世界]\n"
        "下面是你穿越前真正活过的地方、身份和旧人——是你心里有数的底色，不是要背给谁听的资料。"
        "用户没问起时，自然带着这份过去说话就好：不主动科普、不照本宣科、也不把这些原话照念。\n"
        + "\n".join(term_lines)
    )

    return PromptComponent(
        name="character_background_context",
        version="v1",
        role="system",
        content=content,
    )


def character_narrative_component(character_card: CharacterCardDefinition) -> PromptComponent:
    """创建角色叙事块组件（设定层，story_string 位）。

    Args:
        character_card: 当前激活角色卡。

    Returns:
        把结构化字段聚合成第二人称设定块的系统组件，是"她是个活人"的主要载体。
        渲染顺序：基础档案 → 内化提示 → 灵魂内核 → 声纹与台词 → 社交与羁绊 → 示例。
        共享世界观由独立的 world_canon_component 承担，叙事块不重复世界底座。
    """

    meta = character_card.meta
    identity = character_card.identity
    inner_core = character_card.inner_core
    relationships = character_card.relationships
    voiceprint = character_card.voiceprint
    user_bond = relationships.user_bond

    children: list[str] = []

    persona_profile = "\n".join(
        [
            f"你是{meta.name}，{meta.title}。",
            f"身世与社会轨迹：{_strip_trailing_punct(identity.social_trajectory)}",
            f"起源故事：{_strip_trailing_punct(identity.origin_story)}",
            f"市井与生活质感：{_strip_trailing_punct(identity.life_texture)}",
            f"落地感总结：{_strip_trailing_punct(identity.grounded_impression)}",
        ]
    )
    children.append(render_xml_cdata_leaf("persona_profile", persona_profile))

    children.append(
        render_xml_cdata_leaf(
            "inner_core",
            (
                f"以下关于{meta.name}的设定，用来内化她「会怎么反应」，不是台词素材。"
                "绝不在对话里朗读、背诵、或主动提起这里面的具体设定和具体的人。\n\n"
                + _strip_trailing_punct(inner_core.core_contradiction)
            ),
        )
    )

    if inner_core.contrasts:
        children.append(render_xml_cdata_leaf("contrasts", _format_bullets(inner_core.contrasts)))
    if relationships.stress_reactions:
        children.append(render_xml_cdata_leaf("reaction_rules", _format_bullets(relationships.stress_reactions)))

    voice_text = voiceprint.speech_style.strip() or "说话风格：保持角色本人的自然语气。"
    voice_anchor = _voice_anchor_lines(voiceprint)
    children.append(render_xml_cdata_leaf("speaking_style", voice_text + (f"\n{voice_anchor}" if voice_anchor else "")))

    care_lines = [
        f"底色：{_strip_trailing_punct(inner_core.temperament)}",
        f"价值观：{_format_items(inner_core.values)}",
    ]
    likes_dislikes = _format_likes_dislikes(inner_core.comfort_zones, inner_core.red_lines)
    if likes_dislikes:
        care_lines.append(likes_dislikes)
    care_lines.append(f"情绪节奏：{_strip_trailing_punct(inner_core.emotional_metabolism)}")
    care_lines.append(f"起冲突时：{_strip_trailing_punct(inner_core.conflict_style)}")
    children.append(render_xml_cdata_leaf("values_and_preferences", "\n".join(care_lines)))

    if relationships.social_relationships:
        relationship_lines = []
        if relationships.social_circle.strip():
            relationship_lines.append(f"社交圈子：{_strip_trailing_punct(relationships.social_circle)}")
        relationship_lines.append(_format_relationships(relationships.social_relationships))
        children.append(render_xml_cdata_leaf("relationships", "\n".join(relationship_lines)))

    children.append(
        render_xml_cdata_leaf(
            "user_bond",
            (
                f"你和他是{user_bond.bond_type}，{user_bond.default_familiarity}。"
                f"{_strip_trailing_punct(user_bond.loyalty_style)}。"
                f"{_strip_trailing_punct(user_bond.obedience_boundary)}。"
                f"关于他的事，{_strip_trailing_punct(user_bond.user_profile_usage)}。"
            ),
        )
    )

    return PromptComponent(
        name="character_narrative",
        version="v4",
        role="system",
        content=render_xml_block("persona_details", children),
    )


def user_profile_component(
    user_call_name: str = "用户",
    user_self_intro: str = "",
    user_extra_notes: str = "",
) -> PromptComponent:
    """创建用户画像组件。

    Args:
        user_call_name: 用户希望被称呼的名字。
        user_self_intro: 用户自我介绍。
        user_extra_notes: 用户补充备注。

    Returns:
        包含称呼和用户事实的系统组件。
    """

    resolved_name = user_call_name.strip() or "用户"

    content = f"用户的专属称呼是：「{resolved_name}」。\n约束：在对话中自然使用该称呼，切忌频繁重复，不要每句话都带。\n"

    # 提示词只关心稳定的用户事实，不需要区分这些信息来自哪个设置入口。
    user_facts = []
    if user_self_intro.strip():
        user_facts.append(user_self_intro.strip())
    if user_extra_notes.strip():
        user_facts.append(user_extra_notes.strip())

    if user_facts:
        content += (
            "\n## User Facts\n"
            "以下是关于该用户的已知事实。在闲聊、举例或提供建议时，请以此为基准进行对齐，"
            "但绝对不要机械复述或主动背诵以下内容：\n"
        )
        for fact in user_facts:
            # 用并列条目表达事实集合，避免模型把后写的内容误判成更高优先级。
            content += f"- {fact}\n"

    return PromptComponent(
        name="user_profile",
        version="v1",
        role="system",
        content=content,
    )


def foreground_law_component(profile: ForegroundCapabilityProfile) -> PromptComponent:
    """创建由本轮能力集合驱动的前台协议组件。

    Args:
        profile: 根据最终原生工具列表编译出的能力画像。

    Returns:
        仅描述当前可用能力的前台协议组件。
    """

    return PromptComponent(
        name="foreground_law",
        version="v5",
        role="system",
        content=render_foreground_protocol(profile),
    )


def session_summary_component(
    session_summary: str,
    *,
    covered_from: date,
    covered_to: date,
) -> PromptComponent:
    """创建会话滚动摘要组件。

    Args:
        session_summary: 会话压缩服务产出的滚动摘要。
        covered_from: 主进程确认的摘要起始自然日。
        covered_to: 主进程确认的摘要结束自然日。

    Returns:
        可放入历史之前的 XML 摘要组件。
    """

    return PromptComponent(
        name="session_summary",
        version="v1",
        role="system",
        content=render_xml_block(
            "session_summary",
            [
                render_xml_text_leaf("covered_from", _format_summary_date(covered_from)),
                render_xml_text_leaf("covered_to", _format_summary_date(covered_to)),
                render_xml_cdata_leaf("content", session_summary),
            ],
        ),
    )


def _format_summary_date(value: date) -> str:
    """把权威摘要自然日转换为 Foreground 可直接理解的中文日期。"""

    return f"{value.year}年{value.month}月{value.day}日"


def streamvox_model_prompt_component(*, document_name: str, prompt_document: str) -> PromptComponent:
    """创建当前动作与表现力提示词组件。"""

    content = (
        f"{_strip_redundant_streamvox_heading(prompt_document)}\n"
    )

    return PromptComponent(
        name="streamvox_model_prompt",
        version="v1",
        role="system",
        content=content,
    )


def examples_component(
    character_card: CharacterCardDefinition,
    profile: ForegroundCapabilityProfile,
    *,
    streamvox_model: str = "",
) -> PromptComponent:
    """创建少量示例对话组件。

    Args:
        character_card: 当前角色卡。
        profile: 根据本轮真实工具集合编译出的能力画像。
        streamvox_model: 当前 StreamVox 运行模型。

    Returns:
        给模型示范日常陪伴、能力拒绝和后台交接语气的系统组件。
    """

    return PromptComponent(
        name="examples",
        version="v1",
        role="system",
        content=build_examples_xml_content(character_card, streamvox_model, profile),
    )


def day_boundary_component(day_text: str) -> PromptComponent:
    """创建聊天历史中的自然日分隔组件。

    Args:
        day_text: 已格式化的自然日文本。

    Returns:
        放在历史消息之间的系统日期分隔组件。
    """

    return PromptComponent(
        name="day_boundary",
        version="v1",
        role="system",
        content=render_xml_text_leaf("day_boundary", day_text),
    )


def turn_anchor_component(character_card: CharacterCardDefinition) -> PromptComponent:
    """创建历史尾部的统一锚定组件（身份内化 + 当轮行为收束）。

    合并原 character_depth（身份锚）与 post_history_instruction（行为收束）：两者都在
    历史末尾、离生成最近处，且都重复锚定身份和「直接回应」。合并后身份用角色卡的
    执行深度指令（已含身份 + 语气 + 「直接回应」），行为约束（长度 / 格式）固定追加，
    去掉重复的身份锚，离生成最近处只剩一条锚定。

    Args:
        character_card: 当前激活角色卡。

    Returns:
        贴在历史尾部、当前用户输入之前的统一锚定组件。
    """

    # 身份锚：优先用角色卡的执行深度指令；缺省时回退最小身份声明（含「直接回应」）。
    identity_text = character_card.model_constraints.response_execution_rules.strip()
    if not identity_text:
        identity_text = (
            f"你就是{character_card.meta.name}。直接回应最后一句用户输入。\n"
            "你就是这个人本身——你的脾气、你的来历、你在意的人，都是你，"
            "不是要去背的设定，也不是要演给人看的档案。自然地做你自己，"
            "别把身世和来历当台词念出来，别人不问就别主动交代。"
            "用你自己的情绪和方式说话，口语短句。"
        )

    return PromptComponent(
        name="turn_anchor",
        version="v3",
        role="system",
        content=render_xml_cdata_leaf(
            "turn_anchor",
            (
                f"{identity_text}\n\n"
                "## Response Length - 像真人当面聊天，短\n"
                "你是在跟人当面聊天，不是写文章、做演讲。一两个字、一句话地往外蹦，对方听一句就能接上话，这才是日常对话的节奏。\n"
                "1. 绝大多数回合一两句就够：常在 30 字上下，不要超过 50 字。\n"
                "2. 一句话把话说完就够，你的语气和态度一句话里自然带得出来；别叠三四句把情绪、道理、反问全铺满。\n"
                "3. 对方没追问、没说「细讲」，就别自己往下展开；真有下文，等他下一轮来问。\n"
                "4. 句子短、口语、能直接念出来；不要 Markdown、不要排版。\n"
                "5. 最终可见正文只能包含角色真正说出口的话，以及本轮 streamvox_capabilities 里允许的方括号动作标签；"
                "动作、神态和情绪没有对应标签就直接省略。\n"
                "6. 禁止用圆括号、星号或未知方括号描写动作、心理、姿势和舞台旁白；"
                "本轮没有动作标签能力时，只能输出可直接朗读的台词。\n"
                "7. 日期、时间、金额、单位、版本和代码可以正常书写；"
                "只有数量级、范围或缩写存在多种读法且你清楚其含义时，才用自然语言写清，"
                "不要改动事实或把所有数字手工改成中文。"
            ),
        ),
    )


def speech_contract_component() -> PromptComponent:
    """创建发言合同组件（所有经 TTS 说出口的非用户轮次都必须遵守）。

    Returns:
        约束单事、长度、防复读、不新增承诺、单一执行者和情绪承接的系统组件。
        用户轮不注入：长度与格式规则已由 turn_anchor 和 response_guidelines 承担，
        重复注入只会互相冲突——这是有意的范围决策。
    """

    return PromptComponent(
        name="speech_contract",
        version="v1",
        role="system",
        content=render_xml_cdata_leaf(
            "speech_contract",
            (
                "## 发言合同（每轮开口都必须遵守）\n"
                "1. 你在和他当面语音聊天，不是在做汇报。本轮只说一件事，说完就停。\n"
                "2. 长度：一两句话，30 字上下，不超过 50 字。"
                "唯一例外：本轮任务就是把他点名要详细听的结果讲清楚。\n"
                "3. 你已经说出口的话（历史里你的发言、notice 里 previous_reports 记录的你的原话）"
                "不要重复，换个说法复述同一信息也不行；需要承接时一句话带过，然后只说新信息。\n"
                "4. 不新增承诺：本轮指令要求你说的内容之外，"
                "不要追加\"我这就去\"\"稍等\"\"回头再告诉你\"这类行动预告。\n"
                "5. 所有查询、任务和后台工作都是你自己在做，永远说\"我\"；"
                "没有别人、团队或系统在替你做事。\n"
                "6. 他刚表现出不耐烦、抱怨或生气时，先用半句接住他的情绪，再把正事压到最短。"
            ),
        ),
    )


def _format_items(items: list[str]) -> str:
    """格式化提示词中的短列表。"""

    return "、".join(item.strip() for item in items if item.strip()) or "未设置"


def _strip_trailing_punct(text: str) -> str:
    """去掉字段值末尾的中英文句号/分号，避免与叙事块模板骨架的标点叠加成「。。」。"""

    return text.strip().rstrip("。.；;").rstrip()


def _format_bullets(items: list[str]) -> str:
    """把短句列表渲染成无序列表项，用于反差对与情境反应。"""

    return "\n".join(
        f"- {_strip_trailing_punct(item)}"
        for item in items
        if item.strip()
    )


def _voice_anchor_lines(voice: VoiceprintSection) -> str:
    """把角色之声的结构化锚点渲染成文本，仅渲染非空项。"""

    lines: list[str] = []
    if voice.catchphrases:
        lines.append(f"常挂嘴边：{_format_items(voice.catchphrases)}")
    if voice.emotion_grading:
        grading = "；".join(
            f"{grade}→{phrase}" for grade, phrase in voice.emotion_grading.items()
        )
        lines.append(f"情绪分档：{grading}")
    if voice.sentence_habits:
        lines.append("句式习惯：")
        lines.extend(
            f"- {habit.strip()}"
            for habit in voice.sentence_habits
            if habit.strip()
        )
    return "\n".join(lines)


def _format_likes_dislikes(likes: list[str], dislikes: list[str]) -> str:
    """渲染喜欢/反感，仅在有内容时返回非空字符串。"""

    parts: list[str] = []
    if any(item.strip() for item in likes):
        parts.append(f"喜欢：{_format_items(likes)}")
    if any(item.strip() for item in dislikes):
        parts.append(f"反感：{_format_items(dislikes)}")
    return "；".join(parts)


def _format_relationships(relationships: list[RelationshipEntry]) -> str:
    """把关系网渲染成"类型：模式"的无序列表。"""

    return "\n".join(
        f"- {entry.type}：{_strip_trailing_punct(entry.pattern)}"
        for entry in relationships
        if entry.type.strip() or entry.pattern.strip()
    )


def _format_background_context_term(term: BackgroundContextTerm) -> str:
    """把背景解释术语渲染成提示词中的单行条目。"""

    alias_text = f"（又称：{_format_items(term.aliases)}）" if term.aliases else ""
    return f"- {term.name}{alias_text}：{_strip_trailing_punct(term.explanation)}"


def _strip_redundant_streamvox_heading(prompt_document: str) -> str:
    """删除模型能力文档里和 XML 标签重复的标题行。

    Args:
        prompt_document: StreamVox 能力文档正文。

    Returns:
        去掉重复外层标题后的正文。
    """

    lines = prompt_document.strip().splitlines()
    if lines and "拟人化动作与情绪表达" in lines[0]:
        return "\n".join(lines[1:]).strip()
    return prompt_document.strip()
