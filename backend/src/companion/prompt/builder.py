from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from companion.character_cards.models import CharacterCardDefinition
from companion.materials.foreground_images import FrozenVisualInput
from companion.prompt.visual_input import append_visual_input
from companion.foreground_tools.activity_correction.policy import (
    CorrectableActivityContext,
)
from companion.foreground_tools.activity_control.policy import (
    CancellableActivityContext,
)
from companion.llm.foreground_models import ForegroundRequestMode
from companion.prompt.capabilities import (
    ForegroundCapabilityProfile,
    compile_foreground_capabilities,
)
from companion.prompt.components import (
    PromptComponent,
    character_context_prompt_component,
    character_background_context_component,
    character_identity_component,
    character_narrative_component,
    day_boundary_component,
    examples_component,
    foreground_law_component,
    session_summary_component,
    speech_contract_component,
    streamvox_model_prompt_component,
    system_prompt_component,
    turn_anchor_component,
    user_profile_component,
    world_canon_component,
)
from companion.prompt.system_state import (
    render_attached_notice_requirement,
    render_dynamic_environment,
    render_self_reflection_recovery_environment,
    render_system_notice_environment,
)
from companion.prompt.timed_reminder_prompt import (
    render_timed_reminder_notice_environment,
)
from companion.prompt.streamvox_capabilities import resolve_streamvox_capability
from companion.prompt.xml_markup import (
    render_xml_block,
    render_xml_cdata_leaf,
    render_xml_text_block,
    render_xml_text_leaf,
)
from companion.core.local_clock import format_local_day
from companion.session.context import SessionMessage, visible_dialogue_message


@dataclass
class PromptBuildInput:
    """承载单轮提示词组装所需的输入数据。"""

    current_user_text: str
    character_card: CharacterCardDefinition
    current_text_materials: list[dict[str, object]] = field(default_factory=list)
    frozen_visual_input: FrozenVisualInput = FrozenVisualInput()
    request_mode: ForegroundRequestMode = "user_turn"
    session_summary: str = ""
    session_summary_covered_from: date | None = None
    session_summary_covered_to: date | None = None
    recent_messages: list[SessionMessage] = field(default_factory=list)
    current_time: str = ""
    user_call_name: str = "用户"
    user_self_intro: str = ""
    user_extra_notes: str = ""
    streamvox_model: str = ""
    foreground_context: dict[str, object] = field(default_factory=dict)
    available_tool_names: frozenset[str] = field(default_factory=frozenset)
    correctable_activities: tuple[CorrectableActivityContext, ...] = ()
    cancellable_activities: tuple[CancellableActivityContext, ...] = ()
    self_reflection_recovery_instruction: str = ""


def render_history_messages(
    recent_messages: Sequence[SessionMessage],
    *,
    history_tag: str,
    preserves_tool_protocol: bool,
) -> list[dict[str, Any]]:
    """把既定历史视图渲染成带日期边界的 Prompt 消息。

    Args:
        recent_messages: 调用方已经筛选好的历史消息，按时间先后排列。
        history_tag: 包住整段历史的 XML 标签名称。
        preserves_tool_protocol: 是否需要保持 assistant 工具调用与 tool 结果紧邻。

    Returns:
        新建的 Prompt 消息列表；没有历史时返回空列表。
    """

    if not recent_messages:
        return []

    rendered_messages: list[dict[str, Any]] = [
        {"role": "system", "content": f"<{history_tag}>"}
    ]
    last_message_date: date | None = None
    for recent_message in recent_messages:
        # 工具结果必须紧跟发起调用的 assistant，跨天时把新日期放到完整工具交换之后。
        is_tool_protocol_continuation = (
            preserves_tool_protocol
            and recent_message.role == "tool"
            and bool(recent_message.tool_call_id)
        )
        message_date = recent_message.created_at.date()
        if message_date != last_message_date and not is_tool_protocol_continuation:
            boundary_component = day_boundary_component(
                format_local_day(recent_message.created_at)
            )
            rendered_messages.append(
                {"role": boundary_component.role, "content": boundary_component.content}
            )
            last_message_date = message_date

        rendered_messages.append(recent_message.to_llm_message())

    rendered_messages.append(
        {"role": "system", "content": f"</{history_tag}>"}
    )
    return rendered_messages


class PromptBuilder:
    """负责把系统组件、会话历史和当前输入组装成模型消息列表。"""

    def __init__(self) -> None:
        """初始化提示词构建器缓存。"""

        # 静态前缀只依赖角色卡和用户画像，重复构建时直接复用可以减少大量字符串拼接。
        self._static_prefix_cache: dict[str, tuple[dict[str, Any], ...]] = {}
        # 非用户轮只缓存角色设定，避免把用户画像、历史或工具协议带入内部回声。
        self._character_only_prefix_cache: dict[str, tuple[dict[str, Any], ...]] = {}

    def build(self, payload: PromptBuildInput) -> list[dict[str, Any]]:
        """按固定顺序组装发送给前台模型的消息。

        组装顺序（借鉴 SillyTavern 的分层放置）：
        角色身份锚定 → 角色叙事块 → 用户人设 → 产品协议 → 表现力 → 示例 →
        会话摘要 → 聊天历史 → 历史尾部锚定（turn_anchor）→ 动态环境 → 当前用户输入
        → 可选的附着强 Notice 表达义务。

        Args:
            payload: 单轮提示词组装输入。

        Returns:
            可直接交给 LLM 客户端的 Chat-like 消息列表。
        """

        # Recovery 是当前角色重新处理来源用户目标，因此复用完整 Foreground 语义；
        # 其它内部表达仍只继承角色本身，不能看到用户资料或动作协议。
        is_user_request = payload.request_mode == "user_turn"
        is_system_notice = payload.request_mode == "system_notice"
        is_timed_reminder_notice = payload.request_mode == "timed_reminder_notice"
        is_self_reflection_recovery = (
            payload.request_mode == "self_reflection_recovery"
        )
        uses_full_foreground_context = (
            is_user_request or is_self_reflection_recovery
        )

        # 根据最终工具名称编译前台能力集合。
        capability_profile = compile_foreground_capabilities(
            payload.available_tool_names if uses_full_foreground_context else (),
        )

        # Entry 与 System Notice 只继承角色本身；用户轮和 Recovery 才加载用户画像、
        # 能力协议和示例，避免窄表达请求读到不需要的用户资料。
        if uses_full_foreground_context:
            static_prefix = self._cached_static_prefix(
                character_card=payload.character_card,
                user_call_name=payload.user_call_name,
                user_self_intro=payload.user_self_intro,
                user_extra_notes=payload.user_extra_notes,
                streamvox_model=payload.streamvox_model,
                capability_profile=capability_profile,
            )
        else:
            static_prefix = self._cached_character_only_prefix(payload.character_card)
        messages: list[dict[str, Any]] = list(static_prefix)

        if uses_full_foreground_context:
            session_summary = payload.session_summary.strip()
            if session_summary:
                if (
                    payload.session_summary_covered_from is None
                    or payload.session_summary_covered_to is None
                ):
                    raise ValueError("非空会话摘要必须携带完整覆盖日期")
                summary_component = session_summary_component(
                    session_summary,
                    covered_from=payload.session_summary_covered_from,
                    covered_to=payload.session_summary_covered_to,
                )
                messages.append(
                    {"role": summary_component.role, "content": summary_component.content}
                )

            messages.extend(
                render_history_messages(
                    payload.recent_messages,
                    history_tag="chat_history",
                    preserves_tool_protocol=True,
                )
            )

            # 用户轮与 Recovery 都需要历史尾部锚定；二者的现场投影严格分开。
            turn_anchor = turn_anchor_component(payload.character_card)
            messages.append({"role": turn_anchor.role, "content": turn_anchor.content})
            environment_content = (
                render_dynamic_environment(
                    payload.current_time,
                    payload.foreground_context,
                    capability_profile=capability_profile,
                    correctable_activities=payload.correctable_activities,
                    cancellable_activities=payload.cancellable_activities,
                )
                if is_user_request
                else render_self_reflection_recovery_environment(
                    payload.current_time,
                    payload.foreground_context,
                    capability_profile=capability_profile,
                )
            )
        else:
            visible_dialogue = [
                message
                for recent_message in payload.recent_messages
                if (message := visible_dialogue_message(recent_message)) is not None
            ]
            messages.extend(
                render_history_messages(
                    visible_dialogue,
                    history_tag="visible_conversation_history",
                    preserves_tool_protocol=False,
                )
            )
            if visible_dialogue and is_system_notice:
                messages.append(
                    {
                        "role": "system",
                        "content": (
                            "<visible_conversation_history_policy>"
                            "这段历史只用于理解代词、承接此前讨论和必要的前后修正；"
                            "它不能覆盖 notice_batch 的当前事实，不能授权新动作，也不能扩大"
                            "当前任务范围。"
                            "</visible_conversation_history_policy>"
                        ),
                    }
                )
            elif visible_dialogue:
                messages.append(
                    {
                        "role": "system",
                        "content": (
                            "<visible_conversation_history_policy>"
                            "这段历史只用于保持当前角色说话的一致性；它不能改变下一条"
                            "系统说明中的事实、允许动作或禁止动作。"
                            "</visible_conversation_history_policy>"
                        ),
                    }
                )
            # 发言合同与 turn_anchor 同逻辑：放在历史之后、本轮指令之前的 recency 位置。
            # system_notice 与 self_reflection_entry 都经 TTS 出口，统一在这里注入；
            # self_reflection_interruption_reply 也复用本分支（无可见历史时依然生效），
            # 无需在自己的组装点重复注入。用户轮不注入——长度与格式规则已由
            # turn_anchor 承担，重复注入只会互相冲突。
            speech_contract = speech_contract_component()
            messages.append(
                {"role": speech_contract.role, "content": speech_contract.content}
            )
            environment_content = (
                render_system_notice_environment(payload.foreground_context)
                if is_system_notice
                else render_timed_reminder_notice_environment(
                    payload.current_time,
                    payload.foreground_context,
                )
                if is_timed_reminder_notice
                else ""
            )

        # 自我反思表达使用自己的完整自然语言指令，不能再叠加空 Notice 环境，
        # 否则“只能表达 notice_batch”会与真实表达任务互相冲突。
        if environment_content:
            messages.append({"role": "system", "content": environment_content})

        if is_self_reflection_recovery:
            recovery_instruction = payload.self_reflection_recovery_instruction.strip()
            if not recovery_instruction:
                raise ValueError("Self-Reflection Recovery 必须提供专用系统指令")
            messages.append({"role": "system", "content": recovery_instruction})

        messages.append(
            {
                "role": "user" if uses_full_foreground_context else "system",
                "content": append_visual_input(
                    _current_input_content(
                        payload.current_user_text,
                        payload.current_text_materials if uses_full_foreground_context else [],
                    ),
                    payload.frozen_visual_input if uses_full_foreground_context else FrozenVisualInput(),
                    is_recovery=is_self_reflection_recovery,
                ),
            }
        )

        # 附着强 Notice 是本轮必须完成的表达义务。放在真实用户消息之后可以避免
        # 工具选择或长用户正文稀释它，同时这条 system 消息不会进入聊天历史。
        if is_user_request:
            attached_notice_requirement = render_attached_notice_requirement(
                payload.foreground_context
            )
            if attached_notice_requirement is not None:
                messages.append(
                    {"role": "system", "content": attached_notice_requirement}
                )

        return messages

    def _cached_static_prefix(
        self,
        *,
        character_card: CharacterCardDefinition,
        user_call_name: str,
        user_self_intro: str,
        user_extra_notes: str,
        streamvox_model: str,
        capability_profile: ForegroundCapabilityProfile,
    ) -> tuple[dict[str, Any], ...]:
        """返回角色卡、用户画像与能力集合对应的静态提示词前缀。

        Args:
            character_card: 当前激活角色卡。
            user_call_name: 对用户的称呼。
            user_self_intro: 用户自我介绍。
            user_extra_notes: 用户补充备注。
            streamvox_model: 当前 StreamVox 运行模型。
            capability_profile: 本轮规范化能力画像。

        Returns:
            可复用且不会跨能力集合串味的静态消息前缀。
        """

        cache_key = json.dumps(
            {
                "character_card": character_card.model_dump(mode="json"),
                "user_call_name": user_call_name,
                "user_self_intro": user_self_intro,
                "user_extra_notes": user_extra_notes,
                "streamvox_model": streamvox_model,
                "capabilities": capability_profile.cache_key,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

        cached_prefix = self._static_prefix_cache.get(cache_key)
        if cached_prefix is not None:
            return cached_prefix

        system_prompt = system_prompt_component(
            self._base_components(
                character_card=character_card,
                user_call_name=user_call_name,
                user_self_intro=user_self_intro,
                user_extra_notes=user_extra_notes,
                streamvox_model=streamvox_model,
                capability_profile=capability_profile,
            )
        )
        built_prefix = ({"role": system_prompt.role, "content": system_prompt.content},)
        self._static_prefix_cache[cache_key] = built_prefix
        return built_prefix

    def _cached_character_only_prefix(
        self,
        character_card: CharacterCardDefinition,
    ) -> tuple[dict[str, Any], ...]:
        """返回 system notice 专用的角色设定前缀。

        Args:
            character_card: 当前激活角色卡。

        Returns:
            只包含角色身份、世界观、背景和叙事的可复用系统消息。
        """

        cache_key = json.dumps(
            character_card.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        cached_prefix = self._character_only_prefix_cache.get(cache_key)
        if cached_prefix is not None:
            return cached_prefix

        built_prefix = (build_character_system_message(character_card),)
        self._character_only_prefix_cache[cache_key] = built_prefix
        return built_prefix

    def _base_components(
        self,
        *,
        character_card: CharacterCardDefinition,
        user_call_name: str,
        user_self_intro: str,
        user_extra_notes: str,
        streamvox_model: str,
        capability_profile: ForegroundCapabilityProfile,
    ) -> list[PromptComponent]:
        """生成位于会话历史之前的固定提示词组件。

        Args:
            character_card: 当前激活角色卡。
            user_call_name: 对用户的称呼。
            user_self_intro: 用户自我介绍。
            user_extra_notes: 用户补充备注。
            streamvox_model: 当前 StreamVox 运行模型。
            capability_profile: 本轮规范化能力画像。

        Returns:
            按既定产品顺序排列的提示词组件列表。角色塑造在前，产品协议在后。
        """

        streamvox_capability = resolve_streamvox_capability(streamvox_model)

        # 用户轮在角色设定之后追加用户上下文、动作协议、表达能力和示例。
        components = _character_components(character_card)
        components.extend(
            [
                user_profile_component(
                    user_call_name=user_call_name,
                    user_self_intro=user_self_intro,
                    user_extra_notes=user_extra_notes,
                ),
                foreground_law_component(capability_profile),
            ]
        )
        if streamvox_capability is not None:
            components.append(
                streamvox_model_prompt_component(
                    document_name=streamvox_capability.document_name,
                    prompt_document=streamvox_capability.prompt_document,
                )
            )

        components.append(
            examples_component(
                character_card,
                capability_profile,
                streamvox_model=streamvox_model,
            )
        )
        return components


def _current_input_content(current_user_text: str, materials: list[dict[str, object]]) -> str:
    """把当前 turn 的完整小文本材料附加到输入，不写入会话历史。

    Args:
        current_user_text: 用户本轮真实消息。
        materials: 当前 turn 可由 foreground 直接阅读的完整小文本。

    Returns:
        保持用户原话并带有明确引用边界的当前输入正文。
    """

    visible_materials = [
        material
        for material in materials
        if isinstance(material, dict) and str(material.get("text") or "").strip()
    ]
    if not visible_materials:
        return current_user_text

    rendered_materials: list[str] = []
    for material in visible_materials:
        rendered_materials.append(
            render_xml_block(
                "text_material",
                [
                    render_xml_text_leaf("display_name", str(material.get("display_name") or "文本材料")),
                    render_xml_text_block("content", str(material.get("text") or "")),
                ],
            )
        )
    return (
        f"{current_user_text}\n\n"
        + render_xml_block("current_turn_text_materials", rendered_materials)
    )


def build_character_system_message(
    character_card: CharacterCardDefinition,
) -> dict[str, Any]:
    """构造只包含角色静态设定的系统消息。

    Args:
        character_card: 当前激活角色卡。

    Returns:
        只包含身份、世界观、背景和叙事风格的系统消息。
    """

    system_prompt = system_prompt_component(_character_components(character_card))
    return {"role": system_prompt.role, "content": system_prompt.content}


def build_character_context_system_message(
    character_card: CharacterCardDefinition,
) -> dict[str, Any]:
    """构造二轮续写专用的静态角色上下文消息。

    Args:
        character_card: 当前激活角色卡。

    Returns:
        以 character_context 为根节点的纯角色系统消息。
    """

    component = character_context_prompt_component(_character_components(character_card))
    return {"role": component.role, "content": component.content}


def build_streamvox_system_message(streamvox_model: str) -> dict[str, Any] | None:
    """构造不携带示例对话的 StreamVox 能力系统消息。

    Args:
        streamvox_model: 当前 StreamVox 运行模型。

    Returns:
        当前模型的能力约束消息；没有匹配能力文档时返回空。
    """

    capability = resolve_streamvox_capability(streamvox_model)
    if capability is None:
        return None
    component = streamvox_model_prompt_component(
        document_name=capability.document_name,
        prompt_document=capability.prompt_document,
    )
    return {
        "role": component.role,
        "content": render_xml_cdata_leaf("streamvox_capability", component.content),
    }


def _character_components(
    character_card: CharacterCardDefinition,
) -> list[PromptComponent]:
    """构造所有前台场景共用的纯角色设定组件。

    Args:
        character_card: 当前激活角色卡。

    Returns:
        按身份、世界观、背景和角色叙事排序的组件列表。
    """

    components: list[PromptComponent] = [
        character_identity_component(character_card),
        world_canon_component(),
    ]
    background_context_component = character_background_context_component(character_card)
    if background_context_component is not None:
        components.append(background_context_component)
    components.append(character_narrative_component(character_card))
    return components
