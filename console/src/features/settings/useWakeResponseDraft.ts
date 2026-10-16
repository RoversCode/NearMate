import { useEffect, useRef, useState } from "react";

import type { WakeResponseConfig, WakeResponseItem } from "../../api/contracts/wakeResponse";

/**
 * 保存唤醒回复草稿，并在角色卡配置切换时重置草稿。
 *
 * Args:
 *   config: Backend 当前已保存的配置。
 *   identityKey: 当前角色卡编号，用于在切换角色卡时彻底重置草稿。
 *
 * Returns:
 *   草稿状态、保存基线编号和对应的状态更新器。
 */
export function useWakeResponseDraft(config: WakeResponseConfig, identityKey?: string) {
  const [draftConfig, setDraftConfig] = useState(config);
  const [savedItemIds, setSavedItemIds] = useState(() => collectSavedItemIds(config));
  const draftRef = useRef(config);
  const baselineRef = useRef(config);
  const initializedRef = useRef(false);
  const identityRef = useRef(identityKey);
  const deletedItemIdsRef = useRef<Set<string>>(new Set());

  useEffect(() => {
    if (!initializedRef.current) {
      initializedRef.current = true;
      return;
    }

    const previousBaseline = baselineRef.current;
    const previousDraft = draftRef.current;
    const incomingIds = collectSavedItemIds(config);
    const baselineIds = collectSavedItemIds(previousBaseline);
    const commonItemCount = [...incomingIds].filter((itemId) => baselineIds.has(itemId)).length;
    const isCharacterCardSwitch = identityKey !== undefined && identityRef.current !== identityKey
      ? true
      : baselineIds.size > 0 && incomingIds.size > 0 && commonItemCount === 0;
    const mergedConfig = isCharacterCardSwitch
      ? config
      : mergeRefreshedConfig(previousDraft, previousBaseline, config, deletedItemIdsRef.current);

    if (isCharacterCardSwitch) {
      // 切换角色卡后，旧角色的删除记录不能影响新角色。
      deletedItemIdsRef.current = new Set();
    } else {
      // 后端已经确认删除的编号可以从本地删除记录中清掉，避免集合无限增长。
      deletedItemIdsRef.current = new Set(
        [...deletedItemIdsRef.current].filter((itemId) => incomingIds.has(itemId))
      );
    }

    baselineRef.current = config;
    identityRef.current = identityKey;
    draftRef.current = mergedConfig;
    setDraftConfig(mergedConfig);
    setSavedItemIds((currentIds) => {
      const mergedIds = collectSavedItemIds(mergedConfig);
      return new Set(
        [...currentIds, ...incomingIds].filter(
          (itemId) => mergedIds.has(itemId) && !deletedItemIdsRef.current.has(itemId)
        )
      );
    });
  }, [config, identityKey]);

  function updateDraftConfig(nextConfig: WakeResponseConfig | ((currentConfig: WakeResponseConfig) => WakeResponseConfig)): void {
    setDraftConfig((currentConfig) => {
      const resolvedConfig = typeof nextConfig === "function" ? nextConfig(currentConfig) : nextConfig;
      draftRef.current = resolvedConfig;
      return resolvedConfig;
    });
  }

  function markItemDeleted(itemId: string): void {
    deletedItemIdsRef.current.add(itemId);
    setSavedItemIds((currentIds) => {
      const nextIds = new Set(currentIds);
      nextIds.delete(itemId);
      return nextIds;
    });
  }

  return { draftConfig, setDraftConfig: updateDraftConfig, savedItemIds, setSavedItemIds, markItemDeleted };
}

function collectSavedItemIds(config: WakeResponseConfig): Set<string> {
  /** 收集当前后端配置中的条目编号，作为单行删除的保存基线。 */
  return new Set(Object.values(config.buckets).flatMap((items) => items.map((item) => item.id)));
}

function mergeRefreshedConfig(
  previousDraft: WakeResponseConfig,
  previousBaseline: WakeResponseConfig,
  refreshedConfig: WakeResponseConfig,
  deletedItemIds: Set<string>
): WakeResponseConfig {
  return {
    buckets: {
      morning: mergeBucket(previousDraft.buckets.morning, previousBaseline.buckets.morning, refreshedConfig.buckets.morning, deletedItemIds),
      afternoon: mergeBucket(previousDraft.buckets.afternoon, previousBaseline.buckets.afternoon, refreshedConfig.buckets.afternoon, deletedItemIds),
      evening: mergeBucket(previousDraft.buckets.evening, previousBaseline.buckets.evening, refreshedConfig.buckets.evening, deletedItemIds),
      night: mergeBucket(previousDraft.buckets.night, previousBaseline.buckets.night, refreshedConfig.buckets.night, deletedItemIds)
    }
  };
}

function mergeBucket(
  previousDraft: WakeResponseItem[],
  previousBaseline: WakeResponseItem[],
  refreshedItems: WakeResponseItem[],
  deletedItemIds: Set<string>
): WakeResponseItem[] {
  const baselineById = new Map(previousBaseline.map((item) => [item.id, item]));
  const draftById = new Map(previousDraft.map((item) => [item.id, item]));
  const mergedItems = refreshedItems.filter((item) => !deletedItemIds.has(item.id)).map((refreshedItem) => {
    const draftItem = draftById.get(refreshedItem.id);
    const baselineItem = baselineById.get(refreshedItem.id);
    return draftItem && baselineItem && !isSameItem(draftItem, baselineItem) ? draftItem : refreshedItem;
  });
  const refreshedIds = new Set(refreshedItems.map((item) => item.id));
  // 新增草稿不在后端刷新快照中，继续留在编辑区；本地删除的旧条目则不会被补回。
  for (const draftItem of previousDraft) {
    if (!deletedItemIds.has(draftItem.id) && !baselineById.has(draftItem.id) && !refreshedIds.has(draftItem.id)) {
      mergedItems.push(draftItem);
    }
  }
  return mergedItems;
}

function isSameItem(left: WakeResponseItem, right: WakeResponseItem): boolean {
  return left.id === right.id && left.text === right.text && left.audio_path === right.audio_path &&
    left.audio_url === right.audio_url && left.synthesized_at === right.synthesized_at;
}
