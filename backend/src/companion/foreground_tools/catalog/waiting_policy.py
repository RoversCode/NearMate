from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from companion.foreground_tools.catalog.metadata import ToolFamilyMetadata, ToolLatencyClass, ToolMetadata


PolicySource = Literal["tool_override", "family", "builtin", "disabled"]
BatchWaitingMode = Literal["none", "single_inline", "multi_inline", "mixed"]

MULTI_INLINE_WAITING_PHRASE = "好，我一起查一下。"
MIXED_TOOL_WAITING_PHRASE = "好，我一起处理一下。"


@dataclass(frozen=True)
class ToolBatchWaitingDecision:
    """描述一轮工具调用唯一允许播放的固定等待句。

    Args:
        text: 需要立即展示并送入 TTS 的文本；无需等待反馈时为空。
        mode: 无等待、单 Inline 工具族、多 Inline 工具族或混合任务。
    """

    text: str | None
    mode: BatchWaitingMode


@dataclass(frozen=True)
class ToolWaitingPolicy:
    """描述一条等待反馈策略。

    Args:
        enabled: 是否开启等待反馈。
        phrases: 可选等待句列表；开启时至少需要一条非空文案。
    """

    enabled: bool
    phrases: list[str] = field(default_factory=list)

    def to_payload(self) -> dict[str, object]:
        """转换为 API 响应或配置写入使用的字典。"""

        return {"enabled": self.enabled, "phrases": list(self.phrases)}


@dataclass(frozen=True)
class EffectiveToolWaitingPolicy(ToolWaitingPolicy):
    """描述最终生效的等待策略及其来源。

    Args:
        enabled: 是否开启等待反馈。
        phrases: 生效等待句列表。
        source: 策略来源，用于控制台解释继承关系。
    """

    source: PolicySource = "disabled"

    def first_phrase(self) -> str | None:
        """返回第一条可播等待句。"""

        if not self.enabled or not self.phrases:
            return None
        return self.phrases[0]

    def to_payload(self) -> dict[str, object]:
        """转换为 API 响应字典。"""

        payload = super().to_payload()
        payload["source"] = self.source
        return payload


@dataclass(frozen=True)
class ToolWaitingPolicyConfig:
    """描述用户持久化的等待策略配置。

    Args:
        families: family 级策略。
        tools: 单工具 override 策略。
    """

    families: dict[str, ToolWaitingPolicy] = field(default_factory=dict)
    tools: dict[str, ToolWaitingPolicy] = field(default_factory=dict)

    def to_payload(self) -> dict[str, object]:
        """转换为配置文件写入字典。"""

        return {
            "families": {family_id: policy.to_payload() for family_id, policy in self.families.items()},
            "tools": {tool_name: policy.to_payload() for tool_name, policy in self.tools.items()},
        }


class ToolWaitingPolicyStore:
    """读写独立的工具等待反馈策略配置文件。"""

    def __init__(self, path: str | Path) -> None:
        """初始化策略仓储。

        Args:
            path: `tool-waiting-policies.json` 的路径。
        """

        self._path = Path(path)

    def load(self) -> ToolWaitingPolicyConfig:
        """读取并归一化等待策略配置。

        Returns:
            缺失文件返回空配置；非法结构会降级为空配置。
        """

        if not self._path.exists():
            return ToolWaitingPolicyConfig()

        try:
            raw_payload = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return ToolWaitingPolicyConfig()

        if not isinstance(raw_payload, dict):
            return ToolWaitingPolicyConfig()

        return ToolWaitingPolicyConfig(
            families=_normalize_policy_mapping(raw_payload.get("families")),
            tools=_normalize_policy_mapping(raw_payload.get("tools")),
        )

    def save_family_policy(self, family_id: str, *, enabled: bool, phrases: list[str]) -> ToolWaitingPolicyConfig:
        """保存 family 级等待策略。

        Args:
            family_id: 目标工具族标识。
            enabled: 是否开启等待反馈。
            phrases: 等待句列表，开启时必须至少有一条非空文案。

        Returns:
            保存后的完整配置。
        """

        normalized_family_id = _normalize_key(family_id, field_name="family_id")
        policy = _create_policy(enabled=enabled, phrases=phrases)
        config = self.load()
        next_config = ToolWaitingPolicyConfig(
            families={**config.families, normalized_family_id: policy},
            tools=dict(config.tools),
        )
        self._save(next_config)
        return next_config

    def save_tool_override(self, tool_name: str, *, enabled: bool, phrases: list[str]) -> ToolWaitingPolicyConfig:
        """保存单工具 override 策略。

        Args:
            tool_name: 目标工具函数名。
            enabled: 是否开启等待反馈。
            phrases: 等待句列表，开启时必须至少有一条非空文案。

        Returns:
            保存后的完整配置。
        """

        normalized_tool_name = _normalize_key(tool_name, field_name="tool_name")
        policy = _create_policy(enabled=enabled, phrases=phrases)
        config = self.load()
        next_config = ToolWaitingPolicyConfig(
            families=dict(config.families),
            tools={**config.tools, normalized_tool_name: policy},
        )
        self._save(next_config)
        return next_config

    def clear_tool_override(self, tool_name: str) -> ToolWaitingPolicyConfig:
        """清除单工具 override，让工具恢复继承 family 策略。"""

        normalized_tool_name = _normalize_key(tool_name, field_name="tool_name")
        config = self.load()
        next_tools = dict(config.tools)
        next_tools.pop(normalized_tool_name, None)
        next_config = ToolWaitingPolicyConfig(families=dict(config.families), tools=next_tools)
        self._save(next_config)
        return next_config

    def _save(self, config: ToolWaitingPolicyConfig) -> None:
        """把配置原子语义地写回本地 JSON 文件。"""

        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(config.to_payload(), ensure_ascii=False, indent=2), encoding="utf-8")


class ToolWaitingPolicyResolver:
    """按工具元数据和用户配置解析最终等待反馈策略。"""

    def __init__(self, *, store: ToolWaitingPolicyStore) -> None:
        """初始化策略解析器。

        Args:
            store: 等待策略配置仓储；每次解析都会重新读取，保证控制台保存后下一轮生效。
        """

        self._store = store

    def resolve(
        self,
        *,
        metadata: ToolMetadata,
        family_metadata: ToolFamilyMetadata | None,
    ) -> EffectiveToolWaitingPolicy:
        """解析单个工具最终生效的等待策略。

        Args:
            metadata: 工具元数据。
            family_metadata: 工具所属 family 元数据。

        Returns:
            按 tool override、family、builtin、disabled 优先级得到的策略。
        """

        return _resolve_effective_policy(
            config=self._store.load(),
            metadata=metadata,
            family_metadata=family_metadata,
        )

    def resolve_batch(
        self,
        *,
        tool_names: Sequence[str],
        is_background_tool: Callable[[str], bool],
        metadata_resolver: Callable[[str], ToolMetadata],
        family_metadata_resolver: Callable[[str], ToolFamilyMetadata],
    ) -> ToolBatchWaitingDecision:
        """为整批工具选择至多一句 Inline 等待反馈。

        Args:
            tool_names: 模型本轮返回的全部工具名，保持原始顺序。
            is_background_tool: 判断工具是否属于 TaskResult 或 Cognitive 后台通道。
            metadata_resolver: 按工具名读取工具元数据。
            family_metadata_resolver: 按工具族编号读取工具族元数据。

        Returns:
            整批工具唯一等待句及其组合模式。
        """

        # 拿到用户当前保存的等待策略配置，后续所有工具都按同一快照判断。
        config = self._store.load()
        has_background_tool = False
        # 按 family 把同一族的 Inline 工具拢在一起，因为不同族的话术不能共用。
        inline_tools_by_family: dict[
            str,
            list[tuple[ToolMetadata, ToolFamilyMetadata]],
        ] = {}

        # 遍历本轮模型要调的所有工具，分两类：后台工具、Inline 工具。
        # 后台工具自己不播固定等待句，但它们的存在会影响整批的“混合模式”话术。
        for tool_name in tool_names:
            if is_background_tool(tool_name):
                has_background_tool = True
                continue

            # 能拿到元数据的才算 Inline 工具；拿不到的就忽略，不因此整批报错。
            try:
                metadata = metadata_resolver(tool_name)
                family_metadata = family_metadata_resolver(metadata.family_id)
            except KeyError:
                continue
            inline_tools_by_family.setdefault(metadata.family_id, []).append(
                (metadata, family_metadata)
            )

        # 如果全是后台工具，或者全都没找到元数据，那就没有 Inline 等待句可播。
        if not inline_tools_by_family:
            return ToolBatchWaitingDecision(text=None, mode="none")

        # 混合批次和多 Inline 工具族表达的是整轮行动，不能被某个单工具的关闭配置拆散。
        if has_background_tool:
            return ToolBatchWaitingDecision(text=MIXED_TOOL_WAITING_PHRASE, mode="mixed")
        if len(inline_tools_by_family) > 1:
            return ToolBatchWaitingDecision(text=MULTI_INLINE_WAITING_PHRASE, mode="multi_inline")

        # 只剩单一工具族了：按族内每个工具的策略顺序试播，第一个有生效文案的胜出。
        family_tools = next(iter(inline_tools_by_family.values()))
        for metadata, family_metadata in family_tools:
            policy = _resolve_effective_policy(
                config=config,
                metadata=metadata,
                family_metadata=family_metadata,
            )
            waiting_text = policy.first_phrase()
            if waiting_text is not None:
                return ToolBatchWaitingDecision(text=waiting_text, mode="single_inline")
        return ToolBatchWaitingDecision(text=None, mode="none")


def _resolve_effective_policy(
    *,
    config: ToolWaitingPolicyConfig,
    metadata: ToolMetadata,
    family_metadata: ToolFamilyMetadata | None,
) -> EffectiveToolWaitingPolicy:
    """使用同一份配置快照解析单工具最终等待策略。

    Args:
        config: 本次解析使用的用户配置快照。
        metadata: 工具元数据。
        family_metadata: 工具所属 family 元数据。

    Returns:
        按覆盖优先级解析后的等待策略。
    """

    # 后台任务只允许使用模型自然前缀或 accepted/failed 后的回复，不能被配置重新开启固定等待句。
    if family_metadata is not None and not family_metadata.waiting_feedback_eligible:
        return EffectiveToolWaitingPolicy(enabled=False, phrases=[], source="disabled")

    # 单工具 override 表达用户明确意图，必须高于 family 默认。
    if metadata.name in config.tools:
        return _effective(config.tools[metadata.name], source="tool_override")

    if metadata.family_id in config.families:
        return _effective(config.families[metadata.family_id], source="family")

    builtin_policy = _builtin_policy(metadata=metadata, family_metadata=family_metadata)
    if builtin_policy.enabled:
        return _effective(builtin_policy, source="builtin")

    return EffectiveToolWaitingPolicy(enabled=False, phrases=[], source="disabled")


def _normalize_policy_mapping(value: object) -> dict[str, ToolWaitingPolicy]:
    """归一化配置文件里的策略映射。"""

    if not isinstance(value, dict):
        return {}

    policies: dict[str, ToolWaitingPolicy] = {}
    for raw_key, raw_policy in value.items():
        if not isinstance(raw_key, str) or not raw_key.strip() or not isinstance(raw_policy, dict):
            continue
        enabled = bool(raw_policy.get("enabled", False))
        phrases = _normalize_phrases(raw_policy.get("phrases"))

        # 历史或手写配置中开启但无有效文案时降级关闭，避免运行时输出空文本。
        if enabled and not phrases:
            policies[raw_key.strip()] = ToolWaitingPolicy(enabled=False, phrases=[])
            continue

        policies[raw_key.strip()] = ToolWaitingPolicy(enabled=enabled, phrases=phrases)
    return policies


def _create_policy(*, enabled: bool, phrases: list[str]) -> ToolWaitingPolicy:
    """创建并校验用户提交的等待策略。"""

    normalized_phrases = _normalize_phrases(phrases)
    if enabled and not normalized_phrases:
        raise ValueError("enabled waiting policy must include non-empty waiting phrases")
    return ToolWaitingPolicy(enabled=enabled, phrases=normalized_phrases)


def _normalize_phrases(value: object) -> list[str]:
    """裁剪等待句列表并移除空白项。"""

    if not isinstance(value, list):
        return []

    phrases: list[str] = []
    for item in value:
        if not isinstance(item, str):
            continue
        normalized_item = item.strip()
        if normalized_item:
            phrases.append(normalized_item)
    return phrases


def _normalize_key(value: str, *, field_name: str) -> str:
    """裁剪并校验配置键。"""

    normalized_value = value.strip()
    if not normalized_value:
        raise ValueError(f"{field_name} must not be empty")
    return normalized_value


def _builtin_policy(
    *,
    metadata: ToolMetadata,
    family_metadata: ToolFamilyMetadata | None,
) -> ToolWaitingPolicy:
    """从工具元数据生成内置默认策略。"""

    if family_metadata is not None:
        return ToolWaitingPolicy(
            enabled=family_metadata.default_waiting_enabled,
            phrases=list(family_metadata.default_waiting_phrases),
        )

    if metadata.latency_class == ToolLatencyClass.EXTERNAL_API:
        return ToolWaitingPolicy(enabled=True, phrases=["好，我查一下。"])

    return ToolWaitingPolicy(enabled=False, phrases=[])


def _effective(policy: ToolWaitingPolicy, *, source: PolicySource) -> EffectiveToolWaitingPolicy:
    """给普通策略补充来源字段。"""

    return EffectiveToolWaitingPolicy(enabled=policy.enabled, phrases=list(policy.phrases), source=source)
