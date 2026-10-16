import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { CharacterField } from "./CharacterCardFields";

type WakewordKeywordListFieldProps = {
  keywords: string[];
  onChange: (keywords: string[]) => void;
};

/**
 * 渲染角色卡唤醒词的结构化列表编辑器。
 *
 * Args:
 *   keywords: 当前角色卡草稿中的唤醒词列表。
 *   onChange: 唤醒词列表变化回调。
 *
 * Returns:
 *   每条唤醒词独立输入、可新增和删除的 React 节点。
 */
export function WakewordKeywordListField({ keywords, onChange }: WakewordKeywordListFieldProps): JSX.Element {
  const rows = keywords.length > 0 ? keywords : [""];

  function updateKeyword(index: number, value: string): void {
    onChange(rows.map((keyword, keywordIndex) => (keywordIndex === index ? value : keyword)));
  }

  function addKeyword(): void {
    onChange([...rows, ""]);
  }

  function removeKeyword(index: number): void {
    if (rows.length <= 1) {
      onChange([""]);
      return;
    }
    onChange(rows.filter((_, keywordIndex) => keywordIndex !== index));
  }

  return (
    <CharacterField
      description="可添加多个叫法，例如小练、小练同学。激活角色卡后，实时语音入口会使用这里的词。"
      title="角色唤醒词"
    >
      <div className="space-y-2">
        {rows.map((keyword, index) => (
          <div className="flex items-center gap-2" key={index}>
            <Input
              aria-label={`唤醒词 ${index + 1}`}
              onChange={(event) => updateKeyword(index, event.target.value)}
              value={keyword}
            />
            <Button
              disabled={rows.length <= 1}
              onClick={() => removeKeyword(index)}
              type="button"
              variant="outline"
            >
              删除唤醒词
            </Button>
          </div>
        ))}
        <div>
          <Button onClick={addKeyword} type="button" variant="outline">
            新增唤醒词
          </Button>
        </div>
      </div>
    </CharacterField>
  );
}
