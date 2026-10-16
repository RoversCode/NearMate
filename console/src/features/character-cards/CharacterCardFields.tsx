import { type ChangeEvent, type ReactNode, useRef } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";

const MAX_VOICE_REFERENCE_DISPLAY_NAME_LENGTH = 34;

/**
 * 把角色字段的标题、说明和控件按阅读顺序竖向排列。
 *
 * Args:
 *   title: 字段名称；控件自身保留对应的可访问名称。
 *   description: 帮助填写该字段的说明。
 *   children: 占满可用宽度的输入框或复合控件。
 *
 * Returns:
 *   标签在上、控件在下的角色编辑字段。
 */
export function CharacterField({
  title,
  description,
  children
}: {
  title: string;
  description: ReactNode;
  children: ReactNode;
}): JSX.Element {
  return (
    <div className="min-w-0 space-y-2">
      <div className="space-y-1">
        <h3 className="text-sm font-medium">{title}</h3>
        <div className="text-sm leading-relaxed text-muted-foreground">{description}</div>
      </div>
      <div className="min-w-0 w-full">{children}</div>
    </div>
  );
}

/**
 * 渲染角色卡编辑字段分组。
 *
 * Args:
 *   title: 分组标题。
 *   children: 分组内表单内容。
 *
 * Returns:
 *   带 legend 的字段集合。
 */
export function FieldGroup({ title, children }: { title: string; children: React.ReactNode }): JSX.Element {
  return (
    <fieldset className="space-y-4 rounded-lg border p-4">
      <legend className="px-1 text-sm font-semibold">{title}</legend>
      {children}
    </fieldset>
  );
}

/**
 * 渲染单行文本字段。
 *
 * Args:
 *   label: 字段标签。
 *   description: 字段说明。
 *   value: 当前值。
 *   onChange: 值变化回调。
 *
 * Returns:
 *   单行输入框。
 */
export function TextField({
  label,
  description,
  value,
  onChange
}: {
  label: string;
  description: string;
  value: string;
  onChange: (value: string) => void;
}): JSX.Element {
  return (
    <CharacterField description={description} title={label}>
      <Input aria-label={label} onChange={(event) => onChange(event.target.value)} value={value} />
    </CharacterField>
  );
}

/**
 * 渲染多行列表字段。
 *
 * Args:
 *   label: 字段标签。
 *   description: 字段说明。
 *   value: 当前多行文本。
 *   itemLabelPrefix: 可选的首项可访问标签前缀。
 *   onChange: 值变化回调。
 *
 * Returns:
 *   多行文本框。
 */
export function ListField({
  label,
  description,
  value,
  itemLabelPrefix,
  onChange
}: {
  label: string;
  description: string;
  value: string;
  itemLabelPrefix?: string;
  onChange: (value: string) => void;
}): JSX.Element {
  const firstItemLabel = itemLabelPrefix === undefined ? label : `${itemLabelPrefix} 1`;
  return (
    <CharacterField description={description} title={label}>
      <Textarea aria-label={firstItemLabel} onChange={(event) => onChange(event.target.value)} rows={3} value={value} />
    </CharacterField>
  );
}

/**
 * 渲染长文本字段。
 *
 * Args:
 *   label: 字段标签。
 *   description: 字段说明。
 *   value: 当前文本。
 *   onChange: 值变化回调。
 *
 * Returns:
 *   多行文本输入框。
 */
export function TextAreaField({
  label,
  description,
  value,
  onChange
}: {
  label: string;
  description: string;
  value: string;
  onChange: (value: string) => void;
}): JSX.Element {
  return (
    <CharacterField description={description} title={label}>
      <Textarea aria-label={label} onChange={(event) => onChange(event.target.value)} rows={3} value={value} />
    </CharacterField>
  );
}

/**
 * 渲染角色卡参考音频文件选择字段。
 *
 * Args:
 *   label: 字段标签。
 *   description: 字段说明。
 *   currentPath: 当前资产路径。
 *   displayName: 当前展示文件名。
 *   accept: 浏览器文件类型过滤。
 *   disabled: 是否禁用选择。
 *   onChange: 文件选择回调。
 *
 * Returns:
 *   隐藏 input 与显式选择按钮组成的文件字段。
 */
export function FileField({
  label,
  description,
  currentPath,
  displayName,
  accept,
  disabled,
  onChange
}: {
  label: string;
  description: string;
  currentPath: string | null;
  displayName: string | null;
  accept: string;
  disabled: boolean;
  onChange: (event: ChangeEvent<HTMLInputElement>) => void;
}): JSX.Element {
  const rawDisplayText = displayName ?? currentPath ?? "未上传";
  const currentPathText = formatVoiceReferenceDisplayName(rawDisplayText);
  const inputRef = useRef<HTMLInputElement | null>(null);

  return (
    <CharacterField
      description={description}
      title={label}
    >
      <div className="space-y-2">
        <Button
          disabled={disabled}
          onClick={() => inputRef.current?.click()}
          type="button"
          variant="outline"
        >
          选择参考音频
        </Button>
        <input
          accept={accept}
          aria-label={label}
          className="hidden"
          disabled={disabled}
          onChange={onChange}
          ref={inputRef}
          type="file"
        />
        <p className="truncate text-xs text-muted-foreground" title={rawDisplayText}>
          当前参考音频：{currentPathText}
        </p>
      </div>
    </CharacterField>
  );
}

function formatVoiceReferenceDisplayName(value: string): string {
  const trimmedValue = value.trim();
  if (trimmedValue.length <= MAX_VOICE_REFERENCE_DISPLAY_NAME_LENGTH) {
    return trimmedValue;
  }

  return `${trimmedValue.slice(0, MAX_VOICE_REFERENCE_DISPLAY_NAME_LENGTH)}...`;
}
