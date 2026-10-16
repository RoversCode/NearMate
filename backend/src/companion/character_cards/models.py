from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator


class ExampleDialogue(BaseModel):
    """角色卡高光对白打样。"""

    model_config = ConfigDict(extra="forbid")

    user: str  # 示例里用户说的话，用于给模型提供稳定语气参照。
    char: str  # 示例里角色的回应，用于体现角色表达方式与边界。

    @field_validator("user", "char")
    @classmethod
    def trim_text(cls, value: str) -> str:
        """裁剪示例对话文本。"""

        normalized_value = value.strip()
        if not normalized_value:
            raise ValueError("example dialogue text must not be blank")
        return normalized_value


class IdentitySection(BaseModel):
    """基础档案，说明角色是谁、从哪里来、过什么日子。"""

    model_config = ConfigDict(extra="forbid")

    social_trajectory: str  # 家庭背景、老家、城市、工作与阶层底色。
    origin_story: str  # 角色怎样走到今天，写经历如何塑造人格。
    life_texture: str  # 吃穿用度、App、爱好、琐事和真实生活切片。
    grounded_impression: str  # 一句话总结角色的落地感，避免悬浮。


class BackgroundContextTerm(BaseModel):
    """角色背景解释中的单个专有名词。"""

    model_config = ConfigDict(extra="forbid")

    name: str  # 设定文本里会出现的核心专有名词。
    aliases: list[str] = Field(default_factory=list)  # 同一概念的别名或用户可能说出的叫法。
    explanation: str  # 这个名词在角色世界里的含义，只供模型理解，不要求直接说出。

    @field_validator("name", "explanation")
    @classmethod
    def trim_required_text(cls, value: str) -> str:
        """裁剪必填文本并拒绝空值。

        Args:
            value: YAML 中声明的术语名或解释文本。

        Returns:
            裁剪后的非空文本。
        """

        normalized_value = value.strip()
        if not normalized_value:
            raise ValueError("background context term name and explanation must not be blank")
        return normalized_value

    @field_validator("aliases")
    @classmethod
    def normalize_aliases(cls, value: list[str]) -> list[str]:
        """归一化术语别名列表。

        Args:
            value: YAML 中声明的别名列表。

        Returns:
            已裁剪、去空、去重后的别名列表。
        """

        normalized_values: list[str] = []
        seen_values: set[str] = set()
        for item in value:
            normalized_item = str(item).strip()
            if not normalized_item or normalized_item in seen_values:
                continue
            normalized_values.append(normalized_item)
            seen_values.add(normalized_item)
        return normalized_values


class BackgroundContextSection(BaseModel):
    """背景解释，帮助模型理解角色设定中的专有名词。"""

    model_config = ConfigDict(extra="forbid")

    terms: list[BackgroundContextTerm] = Field(default_factory=list)  # 只供模型内化理解的世界观术语表。


class InnerCoreSection(BaseModel):
    """灵魂内核，决定角色遇事时的真实心理活动。"""

    model_config = ConfigDict(extra="forbid")

    temperament: str  # 性格底色和别人对她的初印象。
    values: list[str] = Field(default_factory=list)  # 她长期看重、相信、会维护的东西。
    comfort_zones: list[str] = Field(default_factory=list)  # 她喜欢、放松、会自然靠近的事物。
    red_lines: list[str] = Field(default_factory=list)  # 她讨厌、排斥或绝不能踩的雷区。
    core_contradiction: str  # 最大性格反差，决定人物立体感。
    contrasts: list[str] = Field(default_factory=list)  # 其他反差对，每条最好是“X 但 Y”。
    emotional_metabolism: str  # 开心、难过、生气后的反应、恢复速度和记仇程度。
    conflict_style: str  # 面对分歧、争执时的典型处理方式。


class RelationshipEntry(BaseModel):
    """世俗关系网条目。"""

    model_config = ConfigDict(extra="forbid")

    type: str  # 关系类型，例如闺蜜、家人、同事、用户。
    pattern: str  # 她对这类人的态度、距离和互动模式。


class UserBondSection(BaseModel):
    """主控羁绊设定，描述角色如何对待正在对话的用户。"""

    model_config = ConfigDict(extra="forbid")

    bond_type: str  # 角色与用户关系的总体定义，例如伴侣、伙伴、守护者。
    default_familiarity: str  # 默认亲疏程度，决定初始互动距离感。
    loyalty_style: str  # 角色忠诚的表达方式与心理来源。
    obedience_boundary: str  # 角色服从用户时的边界与拒绝条件。
    user_profile_usage: str  # 角色如何使用用户资料，避免生硬调用信息。


class RelationshipsSection(BaseModel):
    """社交与羁绊，定义角色如何对待不同的人和当前用户。"""

    model_config = ConfigDict(extra="forbid")

    social_circle: str = ""  # 她身处的职业、家庭或社交圈子。
    social_relationships: list[RelationshipEntry] = Field(default_factory=list)  # 家人、同事、朋友等世俗关系网。
    user_bond: UserBondSection  # 她和当前用户之间的主控羁绊。
    stress_reactions: list[str] = Field(default_factory=list)  # 极端或高压情境下的第一反应。


class ExampleDialogueSet(BaseModel):
    """高光对白打样集合。"""

    model_config = ConfigDict(extra="forbid")

    items: list[ExampleDialogue] = Field(default_factory=list)


class StreamVoxExampleScenario(BaseModel):
    """单条 StreamVox 运行示例场景。"""

    model_config = ConfigDict(extra="forbid")

    title: str  # 场景名，用来说明这条示例演示的产品情境。
    user: str  # 用户输入示例。
    char: str  # 角色按当前模型表达能力生成的回应示例。
    expected_tool: Literal[
        "lookup_current_fact",
        "analyze_deeply",
        "start_background_work",
    ] | None = None  # 认知示例期望触发的原生工具。

    @field_validator("title", "user", "char")
    @classmethod
    def trim_required_text(cls, value: str) -> str:
        """裁剪必填文本并拒绝空值。"""

        normalized_value = value.strip()
        if not normalized_value:
            raise ValueError("streamvox example title, user and char must not be blank")
        return normalized_value

    @field_validator("expected_tool", mode="before")
    @classmethod
    def trim_optional_marker(cls, value: str | None) -> str | None:
        """裁剪可选隐藏动作字段。"""

        if value is None:
            return None
        normalized_value = value.strip()
        return normalized_value or None

class StreamVoxExampleSet(BaseModel):
    """某个模型槽位下的 StreamVox 示例集合。"""

    model_config = ConfigDict(extra="forbid")

    items: list[StreamVoxExampleScenario] = Field(default_factory=list)


class StreamVoxExampleLibrary(BaseModel):
    """角色卡内按 StreamVox 模型分槽保存的运行示例。"""

    model_config = ConfigDict(extra="forbid")

    generic: StreamVoxExampleSet = Field(default_factory=StreamVoxExampleSet)  # 不带模型标签的通用示例。
    models: dict[str, StreamVoxExampleSet] = Field(default_factory=dict)  # 具体模型名到专属示例集合的映射。

    @field_validator("models")
    @classmethod
    def normalize_model_keys(cls, value: dict[str, StreamVoxExampleSet]) -> dict[str, StreamVoxExampleSet]:
        """归一化模型示例槽位键名。"""

        normalized_models: dict[str, StreamVoxExampleSet] = {}
        for model, example_set in value.items():
            normalized_model = model.strip().lower()
            if not normalized_model:
                continue
            normalized_models[normalized_model] = example_set
        return normalized_models


class VoiceprintSection(BaseModel):
    """声纹与台词，把性格转译成可听见的表达方式。"""

    model_config = ConfigDict(extra="forbid")

    speech_style: str = ""  # 说话节奏、文字长度、语气、标点和整体声纹。
    catchphrases: list[str] = Field(default_factory=list)  # 高频口头禅和标志性语气词。
    emotion_grading: dict[str, str] = Field(default_factory=dict)  # 情绪烈度到具体词句的映射。
    sentence_habits: list[str] = Field(default_factory=list)  # 句式、断句、标点、语气词习惯。
    example_dialogues: ExampleDialogueSet = Field(default_factory=ExampleDialogueSet)  # 3-5 组最能定调的问答样例。
    streamvox_examples: StreamVoxExampleLibrary = Field(default_factory=StreamVoxExampleLibrary)  # 按模型分槽的运行示例。


class ModelConstraintsSection(BaseModel):
    """模型约束指令，直接收敛角色跑偏和客服化。"""

    model_config = ConfigDict(extra="forbid")

    persona_forbidden_rules: str = ""  # 人设绝对禁忌，例如不能变成客服、导师或鸡汤输出。
    response_execution_rules: str = ""  # 每次回复都要遵守的生成规则，例如短句、只回应最后一句。


class WakewordSection(BaseModel):
    """角色卡专属唤醒词配置。"""

    model_config = ConfigDict(extra="forbid")

    keywords: list[str] = Field(default_factory=lambda: ["你好火花"])  # 当前角色可响应的唤醒词列表。

    @field_validator("keywords")
    @classmethod
    def normalize_keywords(cls, value: list[str]) -> list[str]:
        """归一化唤醒词列表。

        Args:
            value: YAML 中配置的唤醒词列表。

        Returns:
            已裁剪、去空、去重后的唤醒词列表。
        """

        normalized_values: list[str] = []
        seen_values: set[str] = set()
        for item in value:
            normalized_item = str(item).strip()
            if not normalized_item or normalized_item in seen_values:
                continue
            normalized_values.append(normalized_item)
            seen_values.add(normalized_item)
        return normalized_values or ["你好火花"]


class VoiceCaptionTimingSection(BaseModel):
    """角色音色的桌宠字幕节奏校准结果。"""

    model_config = ConfigDict(extra="forbid")

    ms_per_unit: float  # 单个字幕权重单位对应的平均朗读毫秒数。
    calibration_text: str  # 用于测算语速的标准文本。
    duration_ms: int  # 标准文本合成音频的真实时长。
    unit_count: float  # 标准文本按字幕权重算法折算后的单位数量。
    measured_at: float  # 校准完成时的 Unix 时间戳。

    @field_validator("ms_per_unit", "unit_count", "measured_at")
    @classmethod
    def validate_positive_float(cls, value: float) -> float:
        """校验字幕节奏中的有限正浮点数。"""

        if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
            raise ValueError(
                "voice caption timing numeric fields must be finite and positive"
            )
        return value

    @field_validator("duration_ms")
    @classmethod
    def validate_positive_duration(cls, value: int) -> int:
        """校验校准音频时长。"""

        if isinstance(value, bool) or value <= 0:
            raise ValueError("voice caption timing duration must be positive")
        return value

    @field_validator("calibration_text")
    @classmethod
    def trim_calibration_text(cls, value: str) -> str:
        """裁剪校准文本并拒绝空值。"""

        normalized_value = value.strip()
        if not normalized_value:
            raise ValueError("voice caption timing calibration text must not be blank")
        return normalized_value


class VoiceBindingSection(BaseModel):
    """角色卡期望绑定的 StreamVox 语音配置。"""

    model_config = ConfigDict(extra="forbid")

    streamvox_model: str = "qwen3-tts-clone-0.6b-gguf"  # 角色期望使用的 StreamVox 模型。
    role_name: str = "huohua"  # 角色在对应 StreamVox 模型下的音色名。
    reference_audio_path: str | None = "voice/reference.wav"  # 后端管理的卡内音色参考音频路径。
    prompt_text: str = ""  # 参考音频对应文本，直接随角色卡保存。
    seed: int | None = 42  # 角色期望的 StreamVox 推理随机种子。
    inference_options: dict[str, Any] = Field(default_factory=lambda: {"language": "chinese", "icl": False})
    audio_sha256: str | None = None  # StreamVox prompt 参考音频的内容身份，用于识别同名文件是否漂移。
    caption_timing: VoiceCaptionTimingSection | None = None  # 当前音色的桌宠字幕语速校准结果。

    @field_validator("streamvox_model", "role_name")
    @classmethod
    def trim_required_text(cls, value: str) -> str:
        """裁剪必填语音绑定文本。

        Args:
            value: 模型名或音色名。

        Returns:
            裁剪后的非空文本。
        """

        normalized_value = value.strip()
        if not normalized_value:
            raise ValueError("voice binding model and role_name must not be blank")
        return normalized_value

    @field_validator("reference_audio_path")
    @classmethod
    def normalize_relative_asset_path(cls, value: str | None) -> str | None:
        """校验角色目录内的语音资产相对路径。

        Args:
            value: YAML 中声明的相对路径。

        Returns:
            标准化后的相对路径，空值返回 None。
        """

        if value is None:
            return None
        normalized_value = value.replace("\\", "/").strip().strip("/")
        if not normalized_value:
            return None
        path = Path(normalized_value)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("voice binding asset path must be relative to the character card")
        if path.parts[0] != "voice":
            raise ValueError("voice binding asset path must live under voice")
        return normalized_value

    @field_validator("prompt_text")
    @classmethod
    def trim_prompt_text(cls, value: str) -> str:
        """裁剪语音参考文本。

        Args:
            value: YAML 中声明的参考音频文本。

        Returns:
            裁剪后的文本；允许为空，表示还未绑定完整语音资产。
        """

        return value.strip()

    @field_validator("seed")
    @classmethod
    def normalize_seed(cls, value: int | None) -> int | None:
        """校验 StreamVox 随机种子。

        Args:
            value: YAML 中声明的随机种子。

        Returns:
            非负整数或 None。
        """

        if value is None:
            return None
        if isinstance(value, bool) or value < 0:
            raise ValueError("voice binding seed must be a non-negative integer or null")
        return value

    @field_validator("audio_sha256")
    @classmethod
    def normalize_audio_sha256(cls, value: str | None) -> str | None:
        """裁剪参考音频身份哈希。"""

        if value is None:
            return None
        normalized_value = value.strip()
        return normalized_value or None


class MetaSection(BaseModel):
    """角色卡元信息。"""

    model_config = ConfigDict(extra="forbid")

    card_id: str  # 角色卡稳定主键，用于落盘、切换与克隆。
    name: str  # 角色称呼，例如角色名字或用户日常叫法。
    title: str  # 卡片标题或人物画像，用一句话概括这张卡。
    user_call_name: str = "用户"  # 这个角色对当前用户的专属称呼，放在基础档案里便于首屏编辑。
    avatar_path: str | None = None  # 角色卡目录内的形象照片相对路径，例如 assets/icon.png。
    wakeword: WakewordSection = Field(default_factory=WakewordSection)  # 角色卡专属唤醒词。
    voice_binding: VoiceBindingSection = Field(default_factory=VoiceBindingSection)  # 角色卡专属语音绑定。

    @field_validator("card_id", "name", "title")
    @classmethod
    def trim_text(cls, value: str) -> str:
        """裁剪元信息文本。"""

        return value.strip()

    @field_validator("user_call_name")
    @classmethod
    def trim_user_call_name_or_default(cls, value: str) -> str:
        """裁剪角色对用户的称呼。

        Args:
            value: YAML 中声明的用户称呼。

        Returns:
            裁剪后的称呼；空白时回退为“用户”。
        """

        normalized_value = value.strip()
        return normalized_value or "用户"

    @field_validator("avatar_path")
    @classmethod
    def trim_and_validate_avatar_path(cls, value: str | None) -> str | None:
        """裁剪并校验形象照片相对路径。"""

        if value is None:
            return None

        normalized_value = value.replace("\\", "/").strip().strip("/")
        if not normalized_value:
            return None
        path_parts = Path(normalized_value).parts
        if Path(normalized_value).is_absolute() or ".." in path_parts or path_parts[0] != "assets":
            raise ValueError("avatar_path must be a relative path under assets")
        if normalized_value not in {"assets/icon.png", "assets/icon.jpg", "assets/icon.jpeg"}:
            raise ValueError("avatar_path must point to assets/icon.png or assets/icon.jpg")
        return normalized_value


class CharacterCardDefinition(BaseModel):
    """结构化角色卡定义。"""

    model_config = ConfigDict(extra="forbid")

    meta: MetaSection  # 卡片基础信息，保留稳定 wire shape。
    identity: IdentitySection  # 基础档案。
    background_context: BackgroundContextSection = Field(default_factory=BackgroundContextSection)  # 背景解释。
    inner_core: InnerCoreSection  # 灵魂内核。
    relationships: RelationshipsSection  # 社交与羁绊。
    voiceprint: VoiceprintSection = Field(default_factory=VoiceprintSection)  # 声纹与台词。
    model_constraints: ModelConstraintsSection = Field(default_factory=ModelConstraintsSection)  # 模型约束指令。


def load_character_card_definition(raw_payload: dict[str, Any]) -> CharacterCardDefinition:
    """把原始 YAML 对象归一成结构化角色卡。"""

    try:
        return CharacterCardDefinition.model_validate(raw_payload)
    except Exception as exc:  # noqa: BLE001
        raise CharacterCardStoreError("角色卡结构无效") from exc


def load_character_card_yaml_payload(path: Path) -> dict[str, Any]:
    """读取角色卡 YAML 原始对象。"""

    try:
        raw_payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise CharacterCardStoreError(f"角色卡不是合法 YAML：{path}") from exc

    if not isinstance(raw_payload, dict):
        raise CharacterCardStoreError(f"角色卡根节点必须是对象：{path}")

    return raw_payload


class CharacterCardStoreError(ValueError):
    """角色卡仓储错误。"""
