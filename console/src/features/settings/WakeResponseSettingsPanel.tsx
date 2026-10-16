import { useState } from "react";

import type { WakeResponseConfig, WakeResponseGeneratedTexts, WakeResponseItem } from "../../api/contracts/wakeResponse";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useWakeResponseDraft } from "./useWakeResponseDraft";

type WakeResponseSettingsPanelProps = {
  cardId: string;
  config: WakeResponseConfig;
  generationEnabled?: boolean;
  isBusy: boolean;
  onGenerate: (cardId: string) => Promise<WakeResponseGeneratedTexts>;
  onDeleteSavedItem: (cardId: string, itemId: string) => Promise<void>;
  onSynthesize: (cardId: string, itemId: string, bucket: string, text: string) => Promise<WakeResponseItem>;
  onSave: (cardId: string, config: WakeResponseConfig) => Promise<WakeResponseConfig>;
};

const bucketLabels = {
  morning: "早晨",
  afternoon: "午后",
  evening: "晚上",
  night: "深夜"
} as const;

const bucketIds = ["morning", "afternoon", "evening", "night"] as const;

/**
 * 渲染唤醒回复设置面板。
 *
 * Args:
 *   config: 后端当前唤醒回复配置。
 *   isBusy: 外层保存或生成是否进行中。
 *   onGenerate: 基于当前角色卡生成四时段短句。
 *   onDeleteSavedItem: 立即删除后端已保存短句。
 *   onSynthesize: 合成指定短句并立即保存该行。
 *   onSave: 保存草稿并自动合成缺失音频。
 *
 * Returns:
 *   可编辑四时段唤醒回复的 React 节点。
 */
export function WakeResponseSettingsPanel({
  cardId,
  config,
  generationEnabled = true,
  isBusy,
  onGenerate,
  onDeleteSavedItem,
  onSynthesize,
  onSave
}: WakeResponseSettingsPanelProps): JSX.Element {
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [busyItemId, setBusyItemId] = useState<string | null>(null);
  const [isSavingDraft, setIsSavingDraft] = useState(false);
  const [isGenerating, setIsGenerating] = useState(false);
  const { draftConfig, setDraftConfig, savedItemIds, setSavedItemIds, markItemDeleted } = useWakeResponseDraft(config, cardId);

  async function handleGenerate(): Promise<void> {
    if (isGenerating) {
      return;
    }
    setErrorMessage(null);
    setIsGenerating(true);
    try {
      const generated = await onGenerate(cardId);
      setDraftConfig((currentConfig) => appendGeneratedWakeResponseTexts(currentConfig, generated));
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : "未知唤醒回复生成错误");
    } finally {
      setIsGenerating(false);
    }
  }

  function handlePreview(item: WakeResponseItem): void {
    if (!item.audio_url) {
      setErrorMessage("请先合成当前文本后再试听");
      return;
    }
    setErrorMessage(null);
    try {
      playAudioUrl(item.audio_url);
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : "未知唤醒回复试听错误");
    }
  }

  async function handleSynthesize(bucketId: keyof WakeResponseConfig["buckets"], item: WakeResponseItem): Promise<void> {
    const text = item.text.trim();
    if (!item.id || !text) {
      setErrorMessage("唤醒回复文本不能为空");
      return;
    }
    setBusyItemId(item.id);
    setErrorMessage(null);
    try {
      const synthesizedItem = await onSynthesize(cardId, item.id, bucketId, text);
      setDraftConfig((currentConfig) => updateItemInConfig(currentConfig, bucketId, synthesizedItem));
      setSavedItemIds((currentIds) => new Set(currentIds).add(item.id));
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : "未知唤醒回复合成错误");
    } finally {
      setBusyItemId(null);
    }
  }

  async function handleSave(): Promise<void> {
    if (isSavingDraft || isGenerating) {
      return;
    }
    setErrorMessage(null);
    setIsSavingDraft(true);
    const previousSavedItemIds = new Set(savedItemIds);
    try {
      const savedConfig = await onSave(cardId, trimWakeResponseConfig(draftConfig));
      setDraftConfig(savedConfig);
      setSavedItemIds(collectSavedItemIds(savedConfig));
      // 批量保存可能因空白过滤移除已保存条目；迟到的旧配置不能把它们补回草稿。
      const nextSavedItemIds = collectSavedItemIds(savedConfig);
      for (const itemId of previousSavedItemIds) {
        if (!nextSavedItemIds.has(itemId)) {
          markItemDeleted(itemId);
        }
      }
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : "未知唤醒回复保存错误");
    } finally {
      setIsSavingDraft(false);
    }
  }

  async function handleRemoveItem(bucketId: keyof WakeResponseConfig["buckets"], item: WakeResponseItem): Promise<void> {
    if (!savedItemIds.has(item.id)) {
      removeItem(bucketId, item.id);
      return;
    }
    setBusyItemId(item.id);
    setErrorMessage(null);
    try {
      await onDeleteSavedItem(cardId, item.id);
      removeItem(bucketId, item.id);
      markItemDeleted(item.id);
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : "未知唤醒回复删除错误");
    } finally {
      setBusyItemId(null);
    }
  }

  return (
    <section className="space-y-4 rounded-lg border p-4">
      <div className="space-y-1">
        <h3 className="text-sm font-semibold">唤醒回复</h3>
        <p className="text-sm leading-relaxed text-muted-foreground">
          管理唤醒后立刻播放的短语音；保存时会复用仍有效的音频，只补齐缺失或已变化的短句。
        </p>
      </div>
      {errorMessage !== null ? (
        <p className="text-sm font-medium text-destructive">唤醒回复操作失败：{errorMessage}</p>
      ) : null}
      <div className="flex flex-wrap gap-2">
        <Button
          disabled={isBusy || isSavingDraft || isGenerating || busyItemId !== null || !generationEnabled}
          onClick={() => void handleGenerate()}
          type="button"
          variant="outline"
        >
          生成唤醒回复
        </Button>
        <Button disabled={isBusy || isSavingDraft || isGenerating || busyItemId !== null} onClick={() => void handleSave()} type="button">
          保存
        </Button>
      </div>
      <div className="space-y-4 divide-y">
        {bucketIds.map((bucketId) => (
          <div className="space-y-3 pt-4 first:pt-0" key={bucketId}>
            <div className="space-y-1">
              <h4 className="text-sm font-medium">{bucketLabels[bucketId]}</h4>
              <p className="text-sm text-muted-foreground">唤醒时会从这个时段的已合成短句中随机选择一条。</p>
            </div>
            <div className="space-y-2">
              {draftConfig.buckets[bucketId].map((item, index) => {
                const isPendingSynthesis = !item.audio_url;
                const isItemBusy = busyItemId === item.id;
                return (
                  <div className="flex flex-wrap items-center gap-2" key={item.id}>
                    <Input
                      aria-label={`${bucketLabels[bucketId]}回复 ${index + 1}`}
                      className="min-w-0 flex-1"
                      disabled={isBusy || isSavingDraft || isItemBusy}
                      onChange={(event) => updateItemText(bucketId, item.id, event.target.value)}
                      value={item.text}
                    />
                    {isPendingSynthesis ? <Badge variant="warning">待合成</Badge> : null}
                    <Button
                      disabled={isBusy || isSavingDraft || isItemBusy || isPendingSynthesis}
                      onClick={() => handlePreview(item)}
                      size="sm"
                      type="button"
                      variant="outline"
                    >
                      {isPendingSynthesis ? "合成后可试听" : "试听"}
                    </Button>
                    <Button
                      disabled={isBusy || isSavingDraft || busyItemId !== null || !item.text.trim()}
                      onClick={() => void handleSynthesize(bucketId, item)}
                      size="sm"
                      type="button"
                      variant="outline"
                    >
                      合成
                    </Button>
                    <Button
                      disabled={isBusy || isSavingDraft || busyItemId !== null}
                      onClick={() => void handleRemoveItem(bucketId, item)}
                      size="sm"
                      type="button"
                      variant="outline"
                    >
                      删除
                    </Button>
                  </div>
                );
              })}
              <div>
                <Button disabled={isBusy || isSavingDraft || busyItemId !== null} onClick={() => addItem(bucketId)} size="sm" type="button" variant="outline">
                  新增回复
                </Button>
              </div>
            </div>
          </div>
        ))}
      </div>
    </section>
  );

  function updateItemText(bucketId: keyof WakeResponseConfig["buckets"], itemId: string, text: string): void {
    setDraftConfig((currentConfig) => ({
      buckets: {
        ...currentConfig.buckets,
        [bucketId]: currentConfig.buckets[bucketId].map((item) =>
          item.id === itemId ? updateDraftItemText(item, text) : item
        )
      }
    }));
  }

  function removeItem(bucketId: keyof WakeResponseConfig["buckets"], itemId: string): void {
    setDraftConfig((currentConfig) => ({
      buckets: {
        ...currentConfig.buckets,
        [bucketId]: currentConfig.buckets[bucketId].filter((item) => item.id !== itemId)
      }
    }));
  }

  function addItem(bucketId: keyof WakeResponseConfig["buckets"]): void {
    setDraftConfig((currentConfig) => ({
      buckets: {
        ...currentConfig.buckets,
        [bucketId]: [...currentConfig.buckets[bucketId], createDraftItem("")]
      }
    }));
  }
}

function createDraftItem(text: string): WakeResponseItem {
  return {
    id: createDraftId(),
    text
  };
}

function appendGeneratedWakeResponseTexts(
  currentConfig: WakeResponseConfig,
  generated: WakeResponseGeneratedTexts
): WakeResponseConfig {
  // 生成结果只是候选短句，不能覆盖用户已经保存或正在编辑的唤醒回复。
  return {
    buckets: {
      morning: [...currentConfig.buckets.morning, ...generated.buckets.morning.map(createDraftItem)],
      afternoon: [...currentConfig.buckets.afternoon, ...generated.buckets.afternoon.map(createDraftItem)],
      evening: [...currentConfig.buckets.evening, ...generated.buckets.evening.map(createDraftItem)],
      night: [...currentConfig.buckets.night, ...generated.buckets.night.map(createDraftItem)]
    }
  };
}

function trimWakeResponseConfig(config: WakeResponseConfig): WakeResponseConfig {
  return {
    buckets: {
      morning: trimItems(config.buckets.morning),
      afternoon: trimItems(config.buckets.afternoon),
      evening: trimItems(config.buckets.evening),
      night: trimItems(config.buckets.night)
    }
  };
}

function trimItems(items: WakeResponseItem[]): WakeResponseItem[] {
  return items
    .map((item) => trimItem(item))
    .filter((item) => item.text.length > 0);
}

function trimItem(item: WakeResponseItem): WakeResponseItem {
  const trimmedItem: WakeResponseItem = {
    id: item.id,
    text: item.text.trim()
  };
  if (item.audio_path) {
    trimmedItem.audio_path = item.audio_path;
  }
  if (item.audio_url) {
    trimmedItem.audio_url = item.audio_url;
  }
  if (typeof item.synthesized_at === "number") {
    trimmedItem.synthesized_at = item.synthesized_at;
  }
  return trimmedItem;
}

function updateDraftItemText(item: WakeResponseItem, text: string): WakeResponseItem {
  if (item.text === text) {
    return item;
  }
  return {
    ...item,
    text,
    audio_path: null,
    audio_url: null,
    synthesized_at: null
  };
}

function updateItemInConfig(
  config: WakeResponseConfig,
  bucketId: keyof WakeResponseConfig["buckets"],
  synthesizedItem: WakeResponseItem
): WakeResponseConfig {
  const bucketItems = config.buckets[bucketId];
  const hasItem = bucketItems.some((item) => item.id === synthesizedItem.id);
  return {
    buckets: {
      ...config.buckets,
      [bucketId]: hasItem
        ? bucketItems.map((item) => (item.id === synthesizedItem.id ? synthesizedItem : item))
        : [...bucketItems, synthesizedItem]
    }
  };
}

function collectSavedItemIds(config: WakeResponseConfig): Set<string> {
  return new Set(Object.values(config.buckets).flatMap((items) => items.map((item) => item.id)));
}

function playAudioUrl(audioUrl: string): void {
  if (typeof Audio === "undefined") {
    return;
  }
  const audio = new Audio(audioUrl);
  void audio.play();
}

function createDraftId(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return `draft-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}
