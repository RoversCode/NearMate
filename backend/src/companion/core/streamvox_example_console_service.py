from __future__ import annotations

import json

from pydantic import ValidationError

from companion.character_cards.models import (
    CharacterCardDefinition,
    StreamVoxExampleScenario,
    StreamVoxExampleSet,
)
from companion.core.broadcast import BroadcastHub
from companion.llm.client_factory import LlmClientFactory
from companion.llm.client_protocol import LlmClientProtocol
from companion.llm.request_intent import LlmInvocationIntent
from companion.observability.prompt_audit import PromptAuditRecorder
from companion.prompt.streamvox_capabilities import StreamVoxTagSanitizer, resolve_streamvox_capability
from companion.prompt.streamvox_example_generation import (
    INDEPENDENT_DELIVERABLE_EXAMPLE_REQUESTS,
    build_example_generation_messages,
)


STREAMVOX_EXAMPLE_TARGETS = ("generic", "voxcpm2-gguf", "s2-pro-4b-gguf")
LEGACY_SENSE_TO_EXPECTED_TOOL = {
    "lookup_current_fact": "lookup_current_fact",
}


class StreamVoxExampleConsoleService:
    """管理角色卡 StreamVox 示例生成。"""

    def __init__(
        self,
        *,
        hub: BroadcastHub,
        prompt_audit_recorder: PromptAuditRecorder,
    ) -> None:
        """创建生成服务。

        Args:
            hub: 控制面广播中心，提供 LLM 配置和角色卡仓储。
            prompt_audit_recorder: 当前 Backend 主进程的 Prompt 审查记录器。
        """

        self.hub = hub
        self.prompt_audit_recorder = prompt_audit_recorder

    def generate_examples(self, *, path_card_id: str, card: CharacterCardDefinition, target_model: str) -> StreamVoxExampleSet:
        """根据页面草稿生成指定模型槽位的结构化示例。

        Args:
            path_card_id: 路由路径里的角色卡编号。
            card: 页面当前草稿角色卡。
            target_model: 目标示例槽位，generic 或具体 StreamVox 模型名。

        Returns:
            已清洗并归一化的示例集合。
        """

        normalized_path_card_id = path_card_id.strip()
        normalized_target_model = target_model.strip().lower()
        if normalized_path_card_id != card.meta.card_id:
            raise ValueError("角色卡编号不匹配")
        if normalized_target_model not in STREAMVOX_EXAMPLE_TARGETS:
            raise ValueError("StreamVox 示例目标模型不支持")

        # 确认卡槽存在，避免把任意草稿写入不存在的角色上下文。
        self.hub.character_cards.load_card(normalized_path_card_id)

        client = self._client()
        try:
            raw_text = client.complete_text(
                messages=build_example_generation_messages(card=card, target_model=normalized_target_model),
                intent=LlmInvocationIntent.json_object(
                    thinking_level="off",
                    max_output_tokens=1800,
                ),
                audit_source="console",
                audit_operation="streamvox_example_generation",
            )
        finally:
            # 角色创作不是常驻运行时，每次请求创建的连接池必须在本次调用结束时回收。
            client.close()
        return self._normalize_generated_examples(raw_text, target_model=normalized_target_model)

    def _client(self) -> LlmClientProtocol:
        """创建结构化生成使用的 LLM 客户端。"""

        return LlmClientFactory(
            self.hub.settings.llm,
            prompt_audit_recorder=self.prompt_audit_recorder,
        ).client("character_authoring")

    def _normalize_generated_examples(self, raw_text: str, *, target_model: str) -> StreamVoxExampleSet:
        """解析并清洗 LLM 返回的示例集合。"""

        try:
            payload = json.loads(raw_text)
            example_set = StreamVoxExampleSet.model_validate(_drop_legacy_sense_fields(payload))
        except (json.JSONDecodeError, ValidationError) as exception:
            raise RuntimeError("LLM StreamVox 示例生成返回了无效 JSON") from exception

        sanitizer = StreamVoxTagSanitizer(None if target_model == "generic" else resolve_streamvox_capability(target_model))
        if len(example_set.items) != 7:
            raise RuntimeError("LLM StreamVox 示例生成必须返回恰好 7 条示例")
        _validate_generated_scene_contract(example_set.items)
        return StreamVoxExampleSet(
            items=[
                StreamVoxExampleScenario(
                    title=item.title,
                    user=item.user,
                    char=sanitizer.sanitize_sentence(item.char),
                    expected_tool=item.expected_tool,
                )
                for item in example_set.items[:7]
            ]
        )


def _validate_generated_scene_contract(items: list[StreamVoxExampleScenario]) -> None:
    """校验自动生成的七个场景是否遵守固定动作结构。

    Args:
        items: 已完成 Pydantic 字段校验的七条生成示例。

    Returns:
        无返回值；全部场景结构符合蓝图时正常结束。

    Raises:
        RuntimeError: 任一槽位携带错误的原生工具期望。
    """

    expected_tools: tuple[str | frozenset[str] | None, ...] = (
        None,
        "start_background_work",
        "start_background_work",
        frozenset({"lookup_current_fact"}),
        "analyze_deeply",
        None,
        None,
    )
    for index, (item, expected_tool) in enumerate(zip(items, expected_tools, strict=True)):
        if isinstance(expected_tool, frozenset):
            matches = item.expected_tool in expected_tool
        else:
            matches = item.expected_tool == expected_tool
        if not matches:
            raise RuntimeError(
                f"LLM StreamVox 示例生成的第 {index + 1} 个场景工具结构无效"
            )

    if items[2].user not in INDEPENDENT_DELIVERABLE_EXAMPLE_REQUESTS:
        raise RuntimeError("LLM StreamVox 示例生成的完整内容交付场景必须使用无材料创作请求")


def _drop_legacy_sense_fields(payload: object) -> object:
    """迁移并丢弃旧版 StreamVox 示例里的 sense 字段。

    Args:
        payload: LLM 返回的 JSON 对象。

    Returns:
        移除 sense 后的 payload；有效 sense 会迁移为 expected_tool，非对象保持原样交给 Pydantic 报错。
    """

    if not isinstance(payload, dict):
        return payload
    raw_items = payload.get("items")
    if not isinstance(raw_items, list):
        return payload

    items: list[object] = []
    for raw_item in raw_items:
        if not isinstance(raw_item, dict):
            items.append(raw_item)
            continue
        item = dict(raw_item)
        had_sense = item.pop("sense", None)
        legacy_expected_tool = LEGACY_SENSE_TO_EXPECTED_TOOL.get(had_sense)
        if legacy_expected_tool is not None and item.get("expected_tool") is None:
            item["expected_tool"] = legacy_expected_tool
        # 旧字段不能继续进入严格角色卡模型，避免历史暗号重新污染 few-shot。
        item.pop("route", None)
        item.pop("brief", None)
        items.append(item)

    return {**payload, "items": items}
