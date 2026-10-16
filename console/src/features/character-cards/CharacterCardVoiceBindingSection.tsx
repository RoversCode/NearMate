import { type ChangeEvent } from "react";

import type { CharacterCard, CharacterVoiceMatchStatus } from "../../api/contracts/characterCards";
import { Button } from "@/components/ui/button";
import { Select } from "@/components/ui/select";

import { CharacterField, FieldGroup, FileField, TextAreaField, TextField } from "./CharacterCardFields";
import { formatVoiceCaptionTiming } from "./characterCardFormat";
import { streamVoxModelOptions } from "../streamvox/streamVoxModelOptions";
import type { TextFieldPath } from "./characterCardDraft";

/**
 * 渲染角色卡语音绑定编辑区。
 *
 * Args:
 *   card: 当前草稿角色卡。
 *   isBusy: 是否正在保存或应用。
 *   onTextChange: 字段更新回调。
 *   onVoiceReferenceSelected: 选择参考音频后的处理函数。
 *   onVoiceCaptionCalibration: 测算字幕语速动作。
 *   onInferenceOptionsChange: 推理参数 JSON 更新回调。
 *   uploadMessage: 参考音频上传提示。
 *   calibrationMessage: 字幕语速校准提示。
 *   voiceReferenceDisplayName: 当前参考音频展示名。
 *   voiceMatchStatus: 当前语音匹配状态。
 *
 * Returns:
 *   StreamVox 模型绑定、参考音频和推理参数编辑器。
 */
export function VoiceBindingSectionEditor({
  card,
  isBusy,
  onTextChange,
  onVoiceReferenceSelected,
  onVoiceCaptionCalibration,
  onInferenceOptionsChange,
  uploadMessage,
  calibrationMessage,
  voiceReferenceDisplayName,
  voiceMatchStatus
}: {
  card: CharacterCard;
  isBusy: boolean;
  onTextChange: (path: TextFieldPath, value: string) => void;
  onVoiceReferenceSelected?: (audioFile: File) => Promise<void>;
  onVoiceCaptionCalibration?: () => Promise<void>;
  onInferenceOptionsChange: (value: string) => void;
  uploadMessage: string | null;
  calibrationMessage: string | null;
  voiceReferenceDisplayName: string | null;
  voiceMatchStatus: CharacterVoiceMatchStatus | null;
}): JSX.Element {
  async function handleAudioFileChanged(event: ChangeEvent<HTMLInputElement>): Promise<void> {
    const input = event.currentTarget;
    const audioFile = input.files?.[0] ?? null;
    if (audioFile === null || onVoiceReferenceSelected === undefined) {
      return;
    }

    await onVoiceReferenceSelected(audioFile);
    input.value = "";
  }

  return (
    <FieldGroup title="StreamVox 语音绑定">
      <CharacterField
        description="角色期望使用的 StreamVox 模型。切换角色卡不会自动加载，需点击应用语音。"
        title="StreamVox 模型"
      >
        <Select
          aria-label="StreamVox 模型"
          disabled={isBusy}
          onChange={(event) => onTextChange("meta.voice_binding.streamvox_model", event.target.value)}
          value={card.meta.voice_binding.streamvox_model}
        >
          {streamVoxModelOptions.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </Select>
      </CharacterField>
      <TextField
        description="该模型下的音色名。不同模型下同名 role 互相独立。"
        label="音色 role name"
        onChange={(value) => onTextChange("meta.voice_binding.role_name", value)}
        value={card.meta.voice_binding.role_name}
      />
      <FileField
        accept="audio/*"
        currentPath={card.meta.voice_binding.reference_audio_path}
        description="上传新的参考音频后会先写入当前草稿，保存当前卡后生效。"
        disabled={isBusy || onVoiceReferenceSelected === undefined}
        displayName={voiceReferenceDisplayName}
        label="上传参考音频"
        onChange={handleAudioFileChanged}
      />
      <TextAreaField
        description="参考音频逐字文本；修改后需要重新应用角色语音。"
        label="Prompt 文本"
        onChange={(value) => onTextChange("meta.voice_binding.prompt_text", value)}
        value={card.meta.voice_binding.prompt_text}
      />
      <TextField
        description="StreamVox 推理随机种子，留空表示使用模型默认采样。"
        label="语音 seed"
        onChange={(value) => onTextChange("meta.voice_binding.seed", value)}
        value={card.meta.voice_binding.seed === null ? "" : String(card.meta.voice_binding.seed)}
      />
      <TextAreaField
        description={'模型族专属推理参数，使用 JSON 对象，例如 {"language":"chinese","icl":false}。'}
        label="推理参数 JSON"
        onChange={onInferenceOptionsChange}
        value={JSON.stringify(card.meta.voice_binding.inference_options, null, 2)}
      />
      <div className="flex flex-wrap items-center gap-3">
        <Button
          disabled={isBusy || onVoiceCaptionCalibration === undefined}
          onClick={() => void onVoiceCaptionCalibration?.()}
          type="button"
          variant="outline"
        >
          测算字幕语速
        </Button>
        <p className="text-sm text-muted-foreground">{formatVoiceCaptionTiming(card.meta.voice_binding.caption_timing)}</p>
      </div>
      <VoiceStatusBadge status={voiceMatchStatus} />
      {uploadMessage !== null ? <p className="text-sm font-medium text-success">{uploadMessage}</p> : null}
      {calibrationMessage !== null ? <p className="text-sm font-medium text-success">{calibrationMessage}</p> : null}
    </FieldGroup>
  );
}

/**
 * 渲染语音匹配状态文案。
 *
 * Args:
 *   status: 当前语音匹配状态；为 null 时不渲染。
 *
 * Returns:
 *   匹配为成功色文案，其余为破坏性色文案；无状态返回 null。
 */
function VoiceStatusBadge({ status }: { status: CharacterVoiceMatchStatus | null }): JSX.Element | null {
  if (status === null) {
    return null;
  }
  const labelByStatus: Record<CharacterVoiceMatchStatus, string> = {
    matched: "语音已匹配",
    mismatched: "当前语音不是此角色音色",
    missing_role: "当前模型缺少此角色音色",
    missing_assets: "缺少注册音色所需资产",
    runtime_unavailable: "StreamVox runtime 不可用"
  };
  return (
    <p className={status === "matched" ? "text-sm font-medium text-success" : "text-sm font-medium text-destructive"}>
      {labelByStatus[status]}
    </p>
  );
}
