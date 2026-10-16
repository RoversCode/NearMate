import type {
  CharacterCard
} from "../../api/contracts/characterCards";
import type {
  WakeResponseConfig,
  WakeResponseGeneratedTexts,
  WakeResponseItem
} from "../../api/contracts/wakeResponse";
import { WakeResponseSettingsPanel } from "../settings/WakeResponseSettingsPanel";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";

import { CharacterField, FieldGroup, ListField, TextAreaField, TextField } from "./CharacterCardFields";
import {
  type BackgroundContextRow,
  type EmotionGradingValue,
  type ExampleDialogueRow,
  type ListFieldPath,
  type RelationshipRow,
  type TextFieldPath,
} from "./characterCardDraft";
import { formatEmotionGrading } from "./characterCardFormat";
import { WakewordKeywordListField } from "./WakewordKeywordListField";

export type SectionId =
  | "identity"
  | "background_context"
  | "inner_core"
  | "relationships"
  | "voiceprint"
  | "streamvox_examples"
  | "model_constraints"
  | "wake_entry"
  | "voice_binding";

export const CHARACTER_CARD_SECTIONS: Array<{ id: SectionId; label: string; description: string }> = [
  { id: "identity", label: "基础档案", description: "先写她是谁、从哪里来、过什么日子。" },
  { id: "background_context", label: "背景解释", description: "写模型需要先懂、但不该主动背诵的专有名词。" },
  { id: "inner_core", label: "灵魂内核", description: "写她骨子里的驱动力、反差和情绪机制。" },
  { id: "relationships", label: "社交与羁绊", description: "写她怎么对待别人，尤其怎么对待用户。" },
  { id: "voiceprint", label: "声纹与台词", description: "把性格变成具体说话方式和对白打样。" },
  { id: "streamvox_examples", label: "模型示例", description: "为不同 StreamVox 模型生成并微调角色专属运行示例。" },
  { id: "model_constraints", label: "模型约束指令", description: "直接约束模型不要跑偏、不要客服化。" },
  { id: "wake_entry", label: "唤醒入口", description: "配置这个角色自己的唤醒词和唤醒回复入口。" },
  { id: "voice_binding", label: "语音绑定", description: "绑定角色期望使用的 StreamVox 模型和音色。" }
];

/**
 * 渲染基础档案编辑区。
 *
 * Args:
 *   card: 当前草稿角色卡。
 *   onTextChange: 字符串字段变更回调。
 *
 * Returns:
 *   身份、来历与生活质感字段。
 */
export function IdentitySectionEditor({
  card,
  onTextChange
}: {
  card: CharacterCard;
  onTextChange: (path: TextFieldPath, value: string) => void;
}): JSX.Element {
  return (
    <FieldGroup title="卡片基础信息与人物来历">
      <TextField
        description="她叫什么，或者平时大家怎么叫她。"
        label="角色称呼"
        onChange={(value) => onTextChange("meta.name", value)}
        value={card.meta.name}
      />
      <TextField
        description="一句话概括这张卡最直观的人物印象，例如：普通的现实女孩。"
        label="卡片标题 / 人物画像"
        onChange={(value) => onTextChange("meta.title", value)}
        value={card.meta.title}
      />
      <TextField
        description="这个角色日常怎么称呼主人，例如哥哥、小家伙、小可爱。"
        label="主人称呼"
        onChange={(value) => onTextChange("meta.user_call_name", value)}
        value={card.meta.user_call_name}
      />
      <TextAreaField
        description="写她的家庭背景、老家、现在所在城市、职业和阶层底色。"
        label="身世与社会轨迹"
        onChange={(value) => onTextChange("identity.social_trajectory", value)}
        value={card.identity.social_trajectory}
      />
      <TextAreaField
        description="写她怎样走到今天，以及这些经历如何塑造她。"
        label="起源故事"
        onChange={(value) => onTextChange("identity.origin_story", value)}
        value={card.identity.origin_story}
      />
      <TextAreaField
        description="写她吃什么、用什么 App、有什么爱好、会为什么日常琐事发愁。"
        label="市井与生活质感"
        onChange={(value) => onTextChange("identity.life_texture", value)}
        value={card.identity.life_texture}
      />
      <TextAreaField
        description="用一句话压住角色的真实感，避免她像悬浮设定。"
        label="落地感总结"
        onChange={(value) => onTextChange("identity.grounded_impression", value)}
        value={card.identity.grounded_impression}
      />
    </FieldGroup>
  );
}

export function BackgroundContextSectionEditor({
  card,
  onTermsChange
}: {
  card: CharacterCard;
  onTermsChange: (terms: BackgroundContextRow[]) => void;
}): JSX.Element {
  return (
    <FieldGroup title="背景暗知识">
      <BackgroundContextTermEditor value={card.background_context.terms} onChange={onTermsChange} />
    </FieldGroup>
  );
}

export function InnerCoreSectionEditor({
  card,
  onTextChange,
  onListChange
}: {
  card: CharacterCard;
  onTextChange: (path: TextFieldPath, value: string) => void;
  onListChange: (path: ListFieldPath, value: string) => void;
}): JSX.Element {
  return (
    <FieldGroup title="内部驱动力">
      <TextAreaField
        description="写她骨子里的脾气，以及别人第一眼会怎么感到她。"
        label="性格底色"
        onChange={(value) => onTextChange("inner_core.temperament", value)}
        value={card.inner_core.temperament}
      />
      <TextAreaField
        description="写她身上最大的两面性，例如对外有刺、对内护短。"
        label="核心矛盾感"
        onChange={(value) => onTextChange("inner_core.core_contradiction", value)}
        value={card.inner_core.core_contradiction}
      />
      <ListField
        description="每行写一个她长期看重、相信、会维护的东西。"
        label="价值感"
        onChange={(value) => onListChange("inner_core.values", value)}
        value={card.inner_core.values.join("\n")}
      />
      <ListField
        description="每行写一个她喜欢、放松、会自然靠近的事物。"
        label="舒适区"
        onChange={(value) => onListChange("inner_core.comfort_zones", value)}
        value={card.inner_core.comfort_zones.join("\n")}
      />
      <ListField
        description="每行写一个绝对不能踩的雷区或本能排斥点。"
        label="红线"
        onChange={(value) => onListChange("inner_core.red_lines", value)}
        value={card.inner_core.red_lines.join("\n")}
      />
      <ListField
        description="每行写一个稳定反差，最好是“X 但 Y”。"
        label="反差"
        onChange={(value) => onListChange("inner_core.contrasts", value)}
        value={card.inner_core.contrasts.join("\n")}
      />
      <TextAreaField
        description="写她开心、难过、生气时怎么反应，来得快不快，会不会记仇。"
        label="情绪代谢机制"
        onChange={(value) => onTextChange("inner_core.emotional_metabolism", value)}
        value={card.inner_core.emotional_metabolism}
      />
      <TextAreaField
        description="写她面对分歧、被冒犯或争执时的处理方式。"
        label="冲突风格"
        onChange={(value) => onTextChange("inner_core.conflict_style", value)}
        value={card.inner_core.conflict_style}
      />
    </FieldGroup>
  );
}

export function RelationshipsSectionEditor({
  card,
  onTextChange,
  onListChange,
  onRelationshipsChange
}: {
  card: CharacterCard;
  onTextChange: (path: TextFieldPath, value: string) => void;
  onListChange: (path: ListFieldPath, value: string) => void;
  onRelationshipsChange: (value: RelationshipRow[]) => void;
}): JSX.Element {
  return (
    <FieldGroup title="外部张力与用户连接">
      <TextAreaField
        description="写她所处的家庭、职业或社交圈子，以及这个圈子如何影响她。"
        label="社交圈子"
        onChange={(value) => onTextChange("relationships.social_circle", value)}
        value={card.relationships.social_circle}
      />
      <RelationshipEditor value={card.relationships.social_relationships} onChange={onRelationshipsChange} />
      <TextField
        description="她和用户是什么连接：朋友、伴侣、命定陪伴、上下级等。"
        label="主控羁绊类型"
        onChange={(value) => onTextChange("relationships.user_bond.bond_type", value)}
        value={card.relationships.user_bond.bond_type}
      />
      <TextField
        description="她默认跟用户有多熟，是疏离、普通熟人、天然熟稔还是非常亲密。"
        label="默认熟悉度"
        onChange={(value) => onTextChange("relationships.user_bond.default_familiarity", value)}
        value={card.relationships.user_bond.default_familiarity}
      />
      <TextAreaField
        description="写她为什么对用户忠诚，以及这种忠诚怎么表达。"
        label="忠诚风格"
        onChange={(value) => onTextChange("relationships.user_bond.loyalty_style", value)}
        value={card.relationships.user_bond.loyalty_style}
      />
      <TextAreaField
        description="写她护短但不盲从的边界，哪些事她会拒绝或顶嘴。"
        label="互动边界感"
        onChange={(value) => onTextChange("relationships.user_bond.obedience_boundary", value)}
        value={card.relationships.user_bond.obedience_boundary}
      />
      <TextAreaField
        description="写她如何自然使用用户资料，避免像背档案。"
        label="用户资料使用"
        onChange={(value) => onTextChange("relationships.user_bond.user_profile_usage", value)}
        value={card.relationships.user_bond.user_profile_usage}
      />
      <ListField
        description="每行写一个测试情境和第一反应，例如“用户被欺负时 → 先站到用户这边”。"
        itemLabelPrefix="应激反应"
        label="应激反应预设"
        onChange={(value) => onListChange("relationships.stress_reactions", value)}
        value={card.relationships.stress_reactions.join("\n")}
      />
    </FieldGroup>
  );
}

export function VoiceprintSectionEditor({
  card,
  onTextChange,
  onListChange,
  onEmotionGradingChange,
  onExamplesChange
}: {
  card: CharacterCard;
  onTextChange: (path: TextFieldPath, value: string) => void;
  onListChange: (path: ListFieldPath, value: string) => void;
  onEmotionGradingChange: (value: string) => void;
  onExamplesChange: (value: ExampleDialogueRow[]) => void;
}): JSX.Element {
  return (
    <FieldGroup title="表现层">
      <TextAreaField
        description="写她说话节奏、长短句、标点、语气和整体文字声纹。"
        label="发声与句式习惯"
        onChange={(value) => onTextChange("voiceprint.speech_style", value)}
        value={card.voiceprint.speech_style}
      />
      <ListField
        description="每行写一个她常挂嘴边的词或语气词。"
        label="标志性口语词"
        onChange={(value) => onListChange("voiceprint.catchphrases", value)}
        value={card.voiceprint.catchphrases.join("\n")}
      />
      <EmotionGradingField value={card.voiceprint.emotion_grading} onChange={onEmotionGradingChange} />
      <ListField
        description="每行写一个断句、标点、语气词或 TTS 友好的表达习惯。"
        label="句式细节"
        onChange={(value) => onListChange("voiceprint.sentence_habits", value)}
        value={card.voiceprint.sentence_habits.join("\n")}
      />
      <ExampleDialogueEditor value={card.voiceprint.example_dialogues.items} onChange={onExamplesChange} />
    </FieldGroup>
  );
}

export function ModelConstraintsSectionEditor({
  card,
  onTextChange
}: {
  card: CharacterCard;
  onTextChange: (path: TextFieldPath, value: string) => void;
}): JSX.Element {
  return (
    <FieldGroup title="底层控制指令">
      <TextAreaField
        description="写绝不能做的事，例如不能当客服、不能输出鸡汤、不能机械复述资料。"
        label="人设绝对禁忌"
        onChange={(value) => onTextChange("model_constraints.persona_forbidden_rules", value)}
        value={card.model_constraints.persona_forbidden_rules}
      />
      <TextAreaField
        description="写每次生成回复必须遵守的规则，例如短句、只回应最后一句、保持锋利真实感。"
        label="执行深度指令"
        onChange={(value) => onTextChange("model_constraints.response_execution_rules", value)}
        value={card.model_constraints.response_execution_rules}
      />
    </FieldGroup>
  );
}

export function WakeEntrySectionEditor({
  card,
  characterAuthoringEnabled,
  isBusy,
  onKeywordsChange,
  wakeResponseConfig,
  onGenerateWakeResponse,
  onDeleteWakeResponseItem,
  onSynthesizeWakeResponse,
  onSaveWakeResponse
}: {
  card: CharacterCard;
  characterAuthoringEnabled: boolean;
  isBusy: boolean;
  onKeywordsChange: (keywords: string[]) => void;
  wakeResponseConfig?: WakeResponseConfig;
  onGenerateWakeResponse?: (cardId: string) => Promise<WakeResponseGeneratedTexts>;
  onDeleteWakeResponseItem?: (cardId: string, itemId: string) => Promise<void>;
  onSynthesizeWakeResponse?: (cardId: string, itemId: string, bucket: string, text: string) => Promise<WakeResponseItem>;
  onSaveWakeResponse?: (cardId: string, config: WakeResponseConfig) => Promise<WakeResponseConfig>;
}): JSX.Element {
  const canEditWakeResponse =
    wakeResponseConfig !== undefined &&
    onGenerateWakeResponse !== undefined &&
    onDeleteWakeResponseItem !== undefined &&
    onSynthesizeWakeResponse !== undefined &&
    onSaveWakeResponse !== undefined;
  return (
    <FieldGroup title="唤醒入口">
      <WakewordKeywordListField keywords={card.meta.wakeword.keywords} onChange={onKeywordsChange} />
      {canEditWakeResponse ? (
        <WakeResponseSettingsPanel
          config={wakeResponseConfig}
          cardId={card.meta.card_id}
          generationEnabled={characterAuthoringEnabled}
          isBusy={isBusy}
          onDeleteSavedItem={onDeleteWakeResponseItem}
          onGenerate={onGenerateWakeResponse}
          onSynthesize={onSynthesizeWakeResponse}
          onSave={onSaveWakeResponse}
        />
      ) : (
        <p className="text-sm text-muted-foreground">唤醒回复会跟随当前激活角色卡保存；当前控制台状态尚未载入完整配置。</p>
      )}
    </FieldGroup>
  );
}

function BackgroundContextTermEditor({
  value,
  onChange
}: {
  value: BackgroundContextRow[];
  onChange: (value: BackgroundContextRow[]) => void;
}): JSX.Element {
  const resolvedValue = value.length > 0 ? value : [{ name: "", aliases: [], explanation: "" }];

  return (
    <div className="space-y-4">
      {/* 列表仅在末尾增删，使用行号保留输入焦点，不能把可编辑文本放进 key。 */}
      {resolvedValue.map((term, index) => (
        <div className="space-y-3 rounded-lg border bg-muted/30 p-4" key={index}>
          <TextField
            description="写设定里会出现的核心名词，例如稻妻、鸣神大社、雷电将军。"
            label={`术语名称 ${index + 1}`}
            onChange={(nextName) =>
              onChange(resolvedValue.map((item, itemIndex) => (itemIndex === index ? { ...item, name: nextName } : item)))
            }
            value={term.name}
          />
          <ListField
            description="每行写一个别名或用户可能说出的叫法。"
            itemLabelPrefix="术语别名"
            label={`术语别名 ${index + 1}`}
            onChange={(nextAliases) =>
              onChange(
                resolvedValue.map((item, itemIndex) =>
                  itemIndex === index
                    ? {
                        ...item,
                        aliases: nextAliases.split(/\r?\n/)
                      }
                    : item
                )
              )
            }
            value={term.aliases.join("\n")}
          />
          <TextAreaField
            description="解释这个词在角色世界里的含义，只供模型理解，不作为角色主动台词。"
            label={`术语解释 ${index + 1}`}
            onChange={(nextExplanation) =>
              onChange(
                resolvedValue.map((item, itemIndex) =>
                  itemIndex === index ? { ...item, explanation: nextExplanation } : item
                )
              )
            }
            value={term.explanation}
          />
        </div>
      ))}
      <div className="flex flex-wrap gap-2">
        <Button
          onClick={() => onChange([...resolvedValue, { name: "", aliases: [], explanation: "" }])}
          type="button"
          variant="outline"
        >
          添加背景术语
        </Button>
        {resolvedValue.length > 1 ? (
          <Button onClick={() => onChange(resolvedValue.slice(0, -1))} type="button" variant="outline">
            删除最后一条
          </Button>
        ) : null}
      </div>
    </div>
  );
}

function RelationshipEditor({
  value,
  onChange
}: {
  value: RelationshipRow[];
  onChange: (value: RelationshipRow[]) => void;
}): JSX.Element {
  const resolvedValue = value.length > 0 ? value : [{ type: "", pattern: "" }];

  return (
    <div className="space-y-4">
      {/* 列表仅在末尾增删，使用行号保留输入焦点，不能把可编辑文本放进 key。 */}
      {resolvedValue.map((relationship, index) => (
        <div className="space-y-3 rounded-lg border bg-muted/30 p-4" key={index}>
          <TextField
            description="写她对这类人的称呼或关系类型，例如妈妈、同事、闺蜜。"
            label={`关系类型 ${index + 1}`}
            onChange={(nextType) =>
              onChange(resolvedValue.map((item, itemIndex) => (itemIndex === index ? { ...item, type: nextType } : item)))
            }
            value={relationship.type}
          />
          <TextAreaField
            description="写她对这类人的态度、距离感和互动模式。"
            label={`关系模式 ${index + 1}`}
            onChange={(nextPattern) =>
              onChange(
                resolvedValue.map((item, itemIndex) => (itemIndex === index ? { ...item, pattern: nextPattern } : item))
              )
            }
            value={relationship.pattern}
          />
        </div>
      ))}
      <div className="flex flex-wrap gap-2">
        <Button onClick={() => onChange([...resolvedValue, { type: "", pattern: "" }])} type="button" variant="outline">
          添加关系
        </Button>
        {resolvedValue.length > 1 ? (
          <Button onClick={() => onChange(resolvedValue.slice(0, -1))} type="button" variant="outline">
            删除最后一条
          </Button>
        ) : null}
      </div>
    </div>
  );
}

function ExampleDialogueEditor({
  value,
  onChange
}: {
  value: ExampleDialogueRow[];
  onChange: (value: ExampleDialogueRow[]) => void;
}): JSX.Element {
  const resolvedValue = value.length > 0 ? value : [{ user: "", char: "" }];

  return (
    <div className="space-y-4">
      {/* 列表仅在末尾增删，使用行号保留输入焦点，不能把可编辑文本放进 key。 */}
      {resolvedValue.map((dialogue, index) => (
        <div className="space-y-3 rounded-lg border bg-muted/30 p-4" key={index}>
          <TextAreaField
            description="写用户会怎么问，选能触发角色特色的场景。"
            label={`示例用户 ${index + 1}`}
            onChange={(nextUser) =>
              onChange(resolvedValue.map((item, itemIndex) => (itemIndex === index ? { ...item, user: nextUser } : item)))
            }
            value={dialogue.user}
          />
          <TextAreaField
            description="写角色最有代表性的回应，少量但要定调。"
            label={`示例角色 ${index + 1}`}
            onChange={(nextChar) =>
              onChange(resolvedValue.map((item, itemIndex) => (itemIndex === index ? { ...item, char: nextChar } : item)))
            }
            value={dialogue.char}
          />
        </div>
      ))}
      <div className="flex flex-wrap gap-2">
        <Button onClick={() => onChange([...resolvedValue, { user: "", char: "" }])} type="button" variant="outline">
          添加示例
        </Button>
        {resolvedValue.length > 1 ? (
          <Button onClick={() => onChange(resolvedValue.slice(0, -1))} type="button" variant="outline">
            删除最后一条
          </Button>
        ) : null}
      </div>
    </div>
  );
}

function EmotionGradingField({
  value,
  onChange
}: {
  value: EmotionGradingValue;
  onChange: (value: string) => void;
}): JSX.Element {
  return (
    <CharacterField
      description="每行一条，格式为“情绪档: 原话”，用来控制不同烈度时怎么说。"
      title="情绪分档词典"
    >
      <Textarea
        aria-label="情绪分档词典"
        onChange={(event) => onChange(event.target.value)}
        rows={3}
        value={formatEmotionGrading(value)}
      />
    </CharacterField>
  );
}
