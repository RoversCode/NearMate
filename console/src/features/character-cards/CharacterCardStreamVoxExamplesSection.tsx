import {
  CHARACTER_CARD_EXPECTED_TOOLS,
  type CharacterCard,
  isCharacterCardExpectedTool
} from "../../api/contracts/characterCards";
import { type StreamVoxExampleRow, resolveStreamVoxExampleSet } from "./characterCardDraft";
import { Button } from "@/components/ui/button";
import { Select } from "@/components/ui/select";

import { CharacterField, FieldGroup, TextAreaField, TextField } from "./CharacterCardFields";

const STREAMVOX_EXAMPLE_MODEL_OPTIONS = [
  { value: "generic", label: "通用" },
  { value: "voxcpm2-gguf", label: "voxcpm2-gguf" },
  { value: "s2-pro-4b-gguf", label: "s2-pro-4b-gguf" }
];

/**
 * 渲染 StreamVox 模型示例编辑区。
 *
 * Args:
 *   card: 当前草稿角色卡。
 *   targetModel: 当前编辑的目标模型。
 *   disabled: 是否禁用交互。
 *   generationMessage: 生成完成后的提示。
 *   onTargetModelChange: 切换模型槽位。
 *   onGenerate: 生成模型示例。
 *   onExamplesChange: 写回当前模型槽位的示例集。
 *
 * Returns:
 *   模型示例选择器和示例编辑器。
 */
export function StreamVoxExamplesSectionEditor({
  card,
  targetModel,
  disabled,
  generationMessage,
  onTargetModelChange,
  onGenerate,
  onExamplesChange
}: {
  card: CharacterCard;
  targetModel: string;
  disabled: boolean;
  generationMessage: string | null;
  onTargetModelChange: (targetModel: string) => void;
  onGenerate: () => void;
  onExamplesChange: (targetModel: string, value: StreamVoxExampleRow[]) => void;
}): JSX.Element {
  const exampleSet = resolveStreamVoxExampleSet(card, targetModel);

  return (
    <FieldGroup title="StreamVox 模型示例">
      <CharacterField description="选择当前要生成或编辑的模型槽位。" title="示例目标模型">
        <Select aria-label="示例目标模型" onChange={(event) => onTargetModelChange(event.target.value)} value={targetModel}>
          {STREAMVOX_EXAMPLE_MODEL_OPTIONS.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </Select>
      </CharacterField>
      <div>
        <Button disabled={disabled} onClick={onGenerate} type="button" variant="outline">
          生成模型示例
        </Button>
      </div>
      {generationMessage !== null ? <p className="text-sm font-medium text-success">{generationMessage}</p> : null}
      <StreamVoxExampleEditor value={exampleSet.items} onChange={(value) => onExamplesChange(targetModel, value)} />
    </FieldGroup>
  );
}

/**
 * 渲染单个模型槽位下的示例列表编辑器。
 *
 * Args:
 *   value: 当前槽位的示例行。
 *   onChange: 示例列表变化回调。
 *
 * Returns:
 *   可增删的示例行编辑区。
 */
function StreamVoxExampleEditor({
  value,
  onChange
}: {
  value: StreamVoxExampleRow[];
  onChange: (value: StreamVoxExampleRow[]) => void;
}): JSX.Element {
  const resolvedValue = value.length > 0 ? value : [emptyStreamVoxExampleRow()];

  return (
    <div className="space-y-4">
      {/* 列表仅在末尾增删，使用行号保留输入焦点，不能把可编辑文本放进 key。 */}
      {resolvedValue.map((scenario, index) => (
        <div className="space-y-3 rounded-lg border bg-muted/30 p-4" key={index}>
          <TextField
            description="写这条示例演示的产品情境。"
            label={`示例标题 ${index + 1}`}
            onChange={(nextTitle) =>
              onChange(resolvedValue.map((item, itemIndex) => (itemIndex === index ? { ...item, title: nextTitle } : item)))
            }
            value={scenario.title}
          />
          <TextAreaField
            description="写用户会怎么触发这个场景。"
            label={`示例用户 ${index + 1}`}
            onChange={(nextUser) =>
              onChange(resolvedValue.map((item, itemIndex) => (itemIndex === index ? { ...item, user: nextUser } : item)))
            }
            value={scenario.user}
          />
          <TextAreaField
            description="写角色在该模型能力下会说出口的话。"
            label={`示例角色 ${index + 1}`}
            onChange={(nextChar) =>
              onChange(resolvedValue.map((item, itemIndex) => (itemIndex === index ? { ...item, char: nextChar } : item)))
            }
            value={scenario.char}
          />
          <CharacterField
            description="选择这条示例预期触发的现役工具；不需要工具时选择无。"
            title={`预期工具 ${index + 1}`}
          >
            <Select
              aria-label={`预期工具 ${index + 1}`}
              onChange={(event) => {
                const nextExpectedTool = event.target.value;
                if (nextExpectedTool !== "" && !isCharacterCardExpectedTool(nextExpectedTool)) {
                  return;
                }
                onChange(
                  resolvedValue.map((item, itemIndex) =>
                    itemIndex === index
                      ? { ...item, expected_tool: nextExpectedTool === "" ? null : nextExpectedTool }
                      : item
                  )
                );
              }}
              value={scenario.expected_tool ?? ""}
            >
              <option value="">无</option>
              {CHARACTER_CARD_EXPECTED_TOOLS.map((expectedTool) => (
                <option key={expectedTool} value={expectedTool}>{expectedTool}</option>
              ))}
            </Select>
          </CharacterField>
        </div>
      ))}
      <div className="flex flex-wrap gap-2">
        <Button onClick={() => onChange([...resolvedValue, emptyStreamVoxExampleRow()])} type="button" variant="outline">
          添加模型示例
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

function emptyStreamVoxExampleRow(): StreamVoxExampleRow {
  return { title: "", user: "", char: "", expected_tool: null };
}
