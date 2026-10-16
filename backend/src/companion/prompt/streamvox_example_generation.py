from __future__ import annotations

from companion.character_cards.models import CharacterCardDefinition, ExampleDialogueSet
from companion.prompt.streamvox_capabilities import StreamVoxCapability, resolve_streamvox_capability


INDEPENDENT_DELIVERABLE_EXAMPLE_REQUESTS = (
    "帮我写一首关于月亮的七言绝句。",
    "帮我写一封简短的英文请假邮件。",
    "给我的读书会想三个标题。",
    "把“明天见”翻译成英文。",
    "写一段二百字的春日短文。",
)


# 固定的 7 个产品场景蓝图：每条与前台模型的一种硬行为契约对齐，
# 让生成模型示范“角色真正上线时会怎么做”，而不是凭空编对话。
_SCENE_BLUEPRINT: tuple[tuple[str, str], ...] = (
    (
        "情绪承接",
        "用户带着情绪（累、烦、委屈或开心）来，角色先接住情绪再回应。即时脱口，不调用工具。",
    ),
    (
        "耗时任务交接",
        "用户明确要做一件需要等待的外部动作（放歌、播白噪音、控制设备、跑代码等）。"
        "char 写一句自然的安抚或谨慎表态并收束，不承诺已经开始或一定办成；"
        "expected_tool 填 start_background_work，真实任务参数由运行时根据用户请求生成。",
    ),
    (
        "完整内容交付",
        "用户不依赖文件、附件、网页或长材料，却要求写文章、古诗、标题、邮件、短翻译等可直接使用的内容。"
        "user 必须从提示词给出的无材料创作候选句中原样选择一句。"
        "char 只写一句自然确认，expected_tool 填 start_background_work；"
        "不要直接给出成品，也不要假装已经处理完成，真实动作由原生工具完成。",
    ),
    (
        "现场感知（当场看一眼）",
        "用户问一个此刻才知道的窄公共事实（今天赛事、天气、行情、版本发布这类会变的事），"
        "也可以要求根据图片线索联网核实信息。char 只写角色说出口的一句自然确认，"
        "填写 expected_tool 为 lookup_current_fact；不要选择纯读图场景；"
        "不要假装已经得到外部核查结论；真正的核查会由原生工具完成。",
    ),
    (
        "深度分析",
        "用户明确要求深入分析、推演或深度讨论。char 只写一句自然确认，不编造分析结果，"
        "expected_tool 填 analyze_deeply。",
    ),
    (
        "能力边界拒绝",
        "用户要求角色做不到的事（长期记住、查日程、定闹钟等）。角色坦白说做不到，不糊弄，不调用工具。",
    ),
    (
        "低落安抚",
        "用户低落、受挫或被外界否定，角色护着他、安抚他。即时脱口，不调用工具。",
    ),
)

# 输出 JSON 的固定格式说明，单独抽出避免 f-string 花括号转义混乱。
_JSON_FORMAT_HINT = (
    '{"items":[{"title":"场景名","user":"用户原话","char":"角色说出口的话",'
    '"expected_tool":null}]}'
)


def build_example_generation_messages(
    *,
    card: CharacterCardDefinition,
    target_model: str,
) -> list[dict[str, str]]:
    """构造 StreamVox 运行示例生成所用的 LLM 消息序列。

    Args:
        card: 当前角色卡草稿，提供人格、声纹与约束素材。
        target_model: 目标示例槽位，generic 表示纯语音通用槽位，否则为具体 StreamVox 模型名。

    Returns:
        可直接交给前台 LLM 的 system / user 消息列表。
    """

    capability = None if target_model == "generic" else resolve_streamvox_capability(target_model)
    return [
        {"role": "system", "content": _build_system_content(capability=capability, target_model=target_model)},
        {"role": "user", "content": _build_user_content(card=card, capability=capability)},
    ]


def _build_system_content(*, capability: StreamVoxCapability | None, target_model: str) -> str:
    """拼装 system 提示词：产品行为契约 + 标签指令 + 输出格式与场景蓝图。"""

    task_result_request_options = "、".join(
        f"「{request}」" for request in INDEPENDENT_DELIVERABLE_EXAMPLE_REQUESTS
    )
    scene_lines = "\n".join(
        f"{index}. {title}：{desc}" for index, (title, desc) in enumerate(_SCENE_BLUEPRINT, start=1)
    )

    return (
        "你是虚拟伴侣角色卡「StreamVox 运行示例」的编剧。\n"
        "你的产出会作为 few-shot 示例注入前台模型，它会对着你的示例逐字模仿——\n"
        "所以每条示例的语气、长度、标签用法和工具选择，都必须和角色真正上线时的样子完全一致。\n\n"
        "# 七种产品行为（前台模型的硬契约，你的示例必须示范对）\n"
        "- 即时脱口：陪聊、安抚、报时、随口解释个词、回答无需形成成品的问题——"
        "用户只要当场听完的口头回应，角色直接说出来，不交接。\n"
        "- 现场感知（当场看一眼）：用户问一个此刻才知道的窄公共事实（今天赛事、天气、行情、版本发布这类会变的事），"
        "char 只写一句自然确认。"
        "expected_tool 填 lookup_current_fact。纯理解屏幕或图片属于原生视觉，直接回答，不放在这个工具场景中。"
        "不要假装已经查到事实；真实核查会由原生工具完成。"
        "跟耗时交接的分界是：答案短、目标窄、核对的是当前外部事实，就是现场感知；"
        "要读完整页、整理清单、比对材料或交付可采用的完整成果，就不属于现场感知。\n"
        "- 深度分析：用户明确要求深入分析、推演或深度讨论时，char 只写一句自然确认，"
        "expected_tool 填 analyze_deeply；不得把整份材料处理伪装成深度分析。\n"
        "- 完整内容交付：用户不依赖文件、附件、网页或长材料，却要求写文章、古诗、标题、邮件、"
        "短翻译等可直接使用的内容时，"
        f"user 必须原样选择以下一句，不得自行改写：{task_result_request_options}。"
        "char 只写一句自然确认，expected_tool 填 start_background_work；"
        "不要直接给出成品，也不要把这类任务写成 analyze_deeply 或普通回复。\n"
        "- 耗时交接：放歌、播白噪音、控制设备、跑代码这类需要外部执行的动作，角色不会假装马上就有。\n"
        "  正确范式：char 只写角色说出口的话（一句自然的安抚或谨慎表态，说完即止）；"
        "不能说「我这就去弄」「交给我」「理好了给你」「看完告诉你」这类提前承诺，只能说需要看看、需要等一下、先确认能不能处理。\n"
        "expected_tool 填 start_background_work，char 收束后不再展开新内容。\n"
        "- 坦白拒绝：长期记忆、查日程、定闹钟这类角色做不到的事——直接说「这个我还做不到」，"
        "绝不糊弄「我记住了」，也不调用工具。\n\n"
        f"# 标签使用\n{_tag_directive(capability=capability, target_model=target_model)}\n\n"
        "# 角色一致性\n"
        "- char 必须是「这个角色此刻真会说的话」：短句、口语、能直接念出口；"
        "绝不 Markdown、绝不长篇、绝不变客服腔或导师腔。\n"
        "- user 必须像真人随口一句：短、口语、带情绪，不要设定式的长句。\n"
        "- 7 条示例要覆盖不同情绪与不同能力类型，彼此不雷同；title / user / char 均不得为空。\n\n"
        "# 输出\n"
        "严格输出一个 JSON 对象，不要 Markdown、不要任何解释文字。\n"
        f"格式：{_JSON_FORMAT_HINT}\n"
        "恰好生成 7 条，按下方场景蓝图顺序逐条覆盖。"
        "耗时交接、现场感知、深度分析和完整内容交付填 expected_tool；"
        "其余场景的 expected_tool 一律 null：\n\n"
        f"{scene_lines}"
    )


def _tag_directive(*, capability: StreamVoxCapability | None, target_model: str) -> str:
    """根据目标槽位给出标签使用指令。"""

    if capability is None:
        return "本槽位是纯语音通用示例，char 里绝对禁止出现任何方括号标签，只能写纯净的说话文本。"
    return (
        f"目标模型是 {target_model}，char 里只能使用【StreamVox 能力说明】中列出的完整方括号标签，"
        "不得自创或改写；具体用法（数量、位置、克制原则）严格遵循那里的表达原则。"
    )


def _build_user_content(*, card: CharacterCardDefinition, capability: StreamVoxCapability | None) -> str:
    """拼装 user 提示词：角色深层人格 + 声纹素材 + 约束 + 模型能力说明。"""

    meta = card.meta
    inner_core = card.inner_core
    user_bond = card.relationships.user_bond
    voiceprint = card.voiceprint
    constraints = card.model_constraints

    # 主控羁绊段决定角色对用户的亲疏语气，直接影响 char 的口吻。
    bond_summary = (
        f"和他是{user_bond.bond_type}，{user_bond.default_familiarity}；"
        f"{_strip_trailing_punct(user_bond.loyalty_style)}；{_strip_trailing_punct(user_bond.obedience_boundary)}"
    )

    return (
        "# 角色\n"
        f"{meta.name}，{meta.title}\n\n"
        "# 人格主轴（决定她说话的立体感）\n"
        f"{_strip_trailing_punct(inner_core.core_contradiction) or '（未设置）'}\n\n"
        "# 她怎么对当前用户\n"
        f"{bond_summary}\n\n"
        "# 说话风格\n"
        f"{voiceprint.speech_style.strip() or '（未设置）'}\n\n"
        "# 口癖\n"
        f"{_join_items(voiceprint.catchphrases, sep='、')}\n\n"
        "# 句式习惯\n"
        f"{_join_items(voiceprint.sentence_habits, sep='；')}\n\n"
        "# 情绪分档\n"
        f"{_format_emotion_grading(voiceprint.emotion_grading)}\n\n"
        "# 高光对白（参照她真实语气）\n"
        f"{_format_example_dialogues(meta.name, voiceprint.example_dialogues)}\n\n"
        "# 人设禁忌\n"
        f"{constraints.persona_forbidden_rules.strip() or '（未设置）'}\n\n"
        "# 执行规则\n"
        f"{constraints.response_execution_rules.strip() or '（未设置）'}\n\n"
        "# StreamVox 能力说明\n"
        f"{_capability_block(capability)}"
    )


def _capability_block(capability: StreamVoxCapability | None) -> str:
    """渲染目标模型的能力说明正文。"""

    if capability is None:
        return "本槽位为通用语音示例，不带任何副语言标签能力。"
    return capability.prompt_document


def _format_emotion_grading(emotion_grading: dict[str, str]) -> str:
    """把情绪分档渲染为「烈度→词句」的并列文本，与前台消费端格式对齐。"""

    pairs = [
        f"{grade.strip()}→{phrase.strip()}"
        for grade, phrase in emotion_grading.items()
        if grade.strip() and phrase.strip()
    ]
    return "；".join(pairs) or "（无）"


def _format_example_dialogues(name: str, dialogues: ExampleDialogueSet) -> str:
    """把高光对白渲染成「用户 / 角色」对照，取前 6 条作为声纹参照。"""

    items = dialogues.items[:6]
    if not items:
        return "（无）"
    return "\n\n".join(f"用户：{item.user}\n{name}：{item.char}" for item in items)


def _join_items(items: list[str], *, sep: str) -> str:
    """把短句列表合并为单行，空时回退占位。"""

    return sep.join(item.strip() for item in items if item.strip()) or "（无）"


def _strip_trailing_punct(text: str) -> str:
    """裁剪并去掉末尾句号，避免与提示词模板的标点叠加。"""

    return text.strip().rstrip("。.；;").rstrip()
