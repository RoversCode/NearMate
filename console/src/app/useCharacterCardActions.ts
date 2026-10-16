import { useState } from "react";

import type {
  CharacterCard,
  CharacterCardClonePayload,
  CharacterCardPayload,
  CharacterCardStreamVoxExampleSet,
  CharacterCardVoiceReferenceUploadResult,
  CharacterVoiceStatusPayload
} from "../api/contracts/characterCards";
import type { StreamVoxRuntimeState } from "../api/contracts/streamvox";
import {
  activateCharacterCard,
  applyCharacterCardVoice,
  calibrateCharacterCardVoiceCaption,
  cloneCharacterCard,
  deleteCharacterCardCover,
  fetchCharacterCards,
  fetchCharacterCardVoiceStatus,
  generateCharacterCardStreamVoxExamples,
  updateCharacterCard,
  uploadCharacterCardCover,
  uploadCharacterCardVoiceReference
} from "../api/services/characterCards";
import { fetchStreamVoxRuntime } from "../api/services/streamvox";
import { fetchWakeResponseConfig } from "../api/services/wakeResponse";
import { formatSavedRefreshWarning } from "../lib/mutationFeedback";
import type { ConsoleActionContext } from "./consoleActionTypes";

export type CharacterCardActionsState = {
  isSavingCharacterCard: boolean;
  characterCardSaveErrorMessage: string | null;
  characterCardSaveWarningMessage: string | null;
  handleSaveCharacterCard: (card: CharacterCard) => Promise<CharacterCardPayload | null>;
  handleActivateCharacterCard: (cardId: string) => Promise<void>;
  handleUploadCharacterCardVoiceReference: (
    cardId: string,
    audioFile: File
  ) => Promise<CharacterCardVoiceReferenceUploadResult>;
  handleApplyCharacterCardVoice: (cardId: string) => Promise<void>;
  handleCalibrateCharacterCardVoiceCaption: (cardId: string) => Promise<CharacterCardPayload>;
  handleGenerateCharacterCardStreamVoxExamples: (
    cardId: string,
    targetModel: string,
    card: CharacterCard
  ) => Promise<CharacterCardStreamVoxExampleSet>;
  handleCloneCharacterCard: (payload: CharacterCardClonePayload) => Promise<void>;
  handleUploadCharacterCardCover: (cardId: string, imageFile: File) => Promise<void>;
  handleDeleteCharacterCardCover: (cardId: string) => Promise<void>;
};

/**
 * 管理角色卡编辑、激活和资产同步动作。
 *
 * Args:
 *   context: 控制台公共上下文。
 *
 * Returns:
 *   角色卡相关的保存状态与提交方法。
 */
export function useCharacterCardActions({
  baseUrl,
  consoleData,
  setConsoleData,
  refreshSettingsSnapshot
}: ConsoleActionContext): CharacterCardActionsState {
  const [isSavingCharacterCard, setIsSavingCharacterCard] = useState(false);
  const [characterCardSaveErrorMessage, setCharacterCardSaveErrorMessage] = useState<string | null>(null);
  const [characterCardSaveWarningMessage, setCharacterCardSaveWarningMessage] = useState<string | null>(null);

  /**
   * 保存角色卡并返回权威结果，供编辑页保留请求期间新增的草稿。
   *
   * Args:
   *   card: 本次明确提交的完整角色卡。
   *
   * Returns:
   *   保存成功的角色卡快照；失败或已有保存时返回 null。
   */
  async function handleSaveCharacterCard(card: CharacterCard): Promise<CharacterCardPayload | null> {
    if (isSavingCharacterCard) {
      return null;
    }

    setIsSavingCharacterCard(true);
    setCharacterCardSaveErrorMessage(null);
    setCharacterCardSaveWarningMessage(null);

    try {
      const nextPayload = await updateCharacterCard(baseUrl, card.meta.card_id, card);
      // PUT 已经是成功提交点；先落地它返回的角色卡，即使列表读取暂时失败，
      // 编辑器也能确认本次保存成功并继续使用权威的当前卡内容。
      setConsoleData((currentData) => ({
        ...currentData,
        activeCharacterCard: nextPayload
      }));

      try {
        const nextCards = await fetchCharacterCards(baseUrl);
        setConsoleData((currentData) => ({
          ...currentData,
          characterCards: nextCards,
          activeCharacterCard: nextPayload
        }));
      } catch (refreshError) {
        setCharacterCardSaveWarningMessage(formatSavedRefreshWarning(refreshError, "角色卡列表暂时不可用"));
      }
      return nextPayload;
    } catch (error) {
      setCharacterCardSaveErrorMessage(error instanceof Error ? error.message : "未知角色卡保存错误");
      return null;
    } finally {
      setIsSavingCharacterCard(false);
    }
  }

  async function handleActivateCharacterCard(cardId: string): Promise<void> {
    if (isSavingCharacterCard) {
      return;
    }

    setIsSavingCharacterCard(true);
    setCharacterCardSaveErrorMessage(null);
    setCharacterCardSaveWarningMessage(null);

    try {
      const refreshWarnings: string[] = [];
      let shouldApplyCharacterVoice = false;
      try {
        const voiceStatus = await fetchCharacterCardVoiceStatus(baseUrl, cardId);
        shouldApplyCharacterVoice =
          shouldPromptCharacterVoiceSync(voiceStatus) &&
          window.confirm("角色卡声明的语音与当前 StreamVox 未匹配，是否同时切换 StreamVox？");
      } catch (voiceStatusError) {
        // 语音状态只是激活前的提示信息；StreamVox 暂时不可用时仍应允许
        // 用户切换角色卡，避免一个附属查询把真正的保存动作一起拦截。
        refreshWarnings.push(
          `角色卡语音状态暂时不可用，已跳过自动同步：${toErrorMessage(voiceStatusError, "请稍后重试")}`
        );
      }
      const nextPayload = await activateCharacterCard(baseUrl, cardId);
      let nextActiveCard = nextPayload;
      let nextRuntime: StreamVoxRuntimeState | null = consoleData.streamVoxRuntime;

      // 激活接口已经完成持久化；先采用它返回的当前角色，避免后续附属读取失败
      // 时把一次成功激活误报成失败。
      setConsoleData((currentData) => ({
        ...currentData,
        activeCharacterCard: nextPayload
      }));
      // 激活本身已经提交了共享设置事务；先同步 revision，再读取角色卡附属
      // 视图。附属读取失败时也不能让下一次普通设置继续携带旧版本。
      try {
        const refreshedSettings = await refreshSettingsSnapshot(nextPayload.settings);
        if (refreshedSettings === null) {
          refreshWarnings.push(formatSavedRefreshWarning(null, "设置快照暂时不可用"));
        }
      } catch (refreshError) {
        refreshWarnings.push(formatSavedRefreshWarning(refreshError, "设置快照暂时不可用"));
      }

      if (shouldApplyCharacterVoice) {
        try {
          const nextVoiceStatus = await applyCharacterCardVoice(baseUrl, cardId);
          // 语音应用是第二个独立设置事务，必须再次接收它生成的 revision。
          try {
            const refreshedSettings = await refreshSettingsSnapshot(nextVoiceStatus.settings);
            if (refreshedSettings === null) {
              refreshWarnings.push(formatSavedRefreshWarning(null, "设置快照暂时不可用"));
            }
          } catch (refreshError) {
            refreshWarnings.push(formatSavedRefreshWarning(refreshError, "设置快照暂时不可用"));
          }
          nextActiveCard = {
            ...nextPayload,
            voice_match_status: nextVoiceStatus.voice_match_status,
            // 语音应用是第二个独立设置事务；把它返回的提交点一起留在
            // 当前角色响应中，避免后续调用方继续携带激活阶段的旧快照。
            settings: nextVoiceStatus.settings ?? nextPayload.settings,
            effect_message:
              nextVoiceStatus.voice_match_status === "matched" ? "角色卡已激活，角色语音已应用" : nextPayload.effect_message
          };
          setConsoleData((currentData) => ({
            ...currentData,
            activeCharacterCard: nextActiveCard
          }));
        } catch (voiceError) {
          // 角色卡激活和语音应用是两个提交点；前者成功后，后者失败不能把
          // 已经切换的当前角色回滚成“激活失败”。
          refreshWarnings.push(`角色卡已激活，但语音应用失败：${toErrorMessage(voiceError, "请稍后重试")}`);
        }
      }

      const [cardsResult, wakeResponseResult] = await Promise.allSettled([
        fetchCharacterCards(baseUrl),
        fetchWakeResponseConfig(baseUrl, cardId)
      ]);
      const runtimeResult = shouldApplyCharacterVoice
        ? (await Promise.allSettled([fetchStreamVoxRuntime(baseUrl)]))[0]
        : undefined;
      const nextCards = cardsResult.status === "fulfilled" ? cardsResult.value : undefined;
      const nextWakeResponseConfig = wakeResponseResult.status === "fulfilled" ? wakeResponseResult.value : undefined;
      if (cardsResult.status === "rejected") {
        refreshWarnings.push(formatSavedRefreshWarning(cardsResult.reason, "角色卡列表暂时不可用"));
      }
      if (wakeResponseResult.status === "rejected") {
        refreshWarnings.push(formatSavedRefreshWarning(wakeResponseResult.reason, "唤醒回复暂时不可用"));
      }
      if (runtimeResult?.status === "rejected") {
        refreshWarnings.push(formatSavedRefreshWarning(runtimeResult.reason, "StreamVox 状态暂时不可用"));
      }

      setConsoleData((currentData) => ({
        ...currentData,
        characterCards: nextCards ?? currentData.characterCards,
        activeCharacterCard: nextActiveCard,
        streamVoxRuntime: runtimeResult?.status === "fulfilled" ? runtimeResult.value : nextRuntime,
        wakeResponseConfig: nextWakeResponseConfig ?? currentData.wakeResponseConfig
      }));
      if (refreshWarnings.length > 0) {
        setCharacterCardSaveWarningMessage(refreshWarnings.join("；"));
      }
    } catch (error) {
      setCharacterCardSaveErrorMessage(error instanceof Error ? error.message : "未知角色卡激活错误");
    } finally {
      setIsSavingCharacterCard(false);
    }
  }

  async function handleUploadCharacterCardVoiceReference(
    cardId: string,
    audioFile: File
  ): Promise<CharacterCardVoiceReferenceUploadResult> {
    if (isSavingCharacterCard) {
      throw new Error("角色卡正在保存中，请稍后再上传参考音频");
    }

    setIsSavingCharacterCard(true);
    setCharacterCardSaveErrorMessage(null);
    setCharacterCardSaveWarningMessage(null);

    try {
      return await uploadCharacterCardVoiceReference(baseUrl, cardId, audioFile);
    } catch (error) {
      setCharacterCardSaveErrorMessage(error instanceof Error ? error.message : "未知角色卡参考音频上传错误");
      throw error;
    } finally {
      setIsSavingCharacterCard(false);
    }
  }

  async function handleApplyCharacterCardVoice(cardId: string): Promise<void> {
    if (isSavingCharacterCard) {
      return;
    }

    setIsSavingCharacterCard(true);
    setCharacterCardSaveErrorMessage(null);
    setCharacterCardSaveWarningMessage(null);

    try {
      const voiceStatus = await applyCharacterCardVoice(baseUrl, cardId);
      const nextActiveCard = mergeAppliedVoiceStatus(consoleData.activeCharacterCard, cardId, voiceStatus);
      // 语音应用响应本身已经是成功提交点；先写入当前卡的匹配状态，避免后续
      // 列表或 runtime 读取失败时把成功应用误报成失败。
      setConsoleData((currentData) => ({
        ...currentData,
        activeCharacterCard: mergeAppliedVoiceStatus(currentData.activeCharacterCard, cardId, voiceStatus)
      }));
      // 语音应用已经推进全局 settings_revision；附属角色卡或 runtime 查询
      // 即使随后失败，也不能丢掉这次提交点版本。
      let settingsRefreshWarning: string | null = null;
      try {
        const refreshedSettings = await refreshSettingsSnapshot(voiceStatus.settings);
        if (refreshedSettings === null) {
          settingsRefreshWarning = formatSavedRefreshWarning(null, "设置快照暂时不可用");
        }
      } catch (refreshError) {
        settingsRefreshWarning = formatSavedRefreshWarning(refreshError, "设置快照暂时不可用");
      }
      const [cardsResult, runtimeResult] = await Promise.allSettled([
        fetchCharacterCards(baseUrl),
        fetchStreamVoxRuntime(baseUrl)
      ]);
      const refreshWarnings: string[] = settingsRefreshWarning === null ? [] : [settingsRefreshWarning];
      if (cardsResult.status === "rejected") {
        refreshWarnings.push(formatSavedRefreshWarning(cardsResult.reason, "角色卡列表暂时不可用"));
      }
      if (runtimeResult.status === "rejected") {
        refreshWarnings.push(formatSavedRefreshWarning(runtimeResult.reason, "StreamVox 状态暂时不可用"));
      }

      setConsoleData((currentData) => ({
        ...currentData,
        characterCards: cardsResult.status === "fulfilled" ? cardsResult.value : currentData.characterCards,
        activeCharacterCard: nextActiveCard ?? currentData.activeCharacterCard,
        streamVoxRuntime: runtimeResult.status === "fulfilled" ? runtimeResult.value : currentData.streamVoxRuntime
      }));
      if (refreshWarnings.length > 0) {
        setCharacterCardSaveWarningMessage(refreshWarnings.join("；"));
      }
    } catch (error) {
      setCharacterCardSaveErrorMessage(error instanceof Error ? error.message : "未知角色语音应用错误");
    } finally {
      setIsSavingCharacterCard(false);
    }
  }

  async function handleCalibrateCharacterCardVoiceCaption(cardId: string): Promise<CharacterCardPayload> {
    if (isSavingCharacterCard) {
      throw new Error("角色卡正在保存中，请稍后再测算字幕语速");
    }

    setIsSavingCharacterCard(true);
    setCharacterCardSaveErrorMessage(null);
    setCharacterCardSaveWarningMessage(null);

    try {
      const nextPayload = await calibrateCharacterCardVoiceCaption(baseUrl, cardId);
      // 校准接口已经返回包含新字幕节奏的角色卡；先落地这份响应，附属列表
      // 刷新失败时仍应让用户看到本次校准已经成功。
      setConsoleData((currentData) => ({
        ...currentData,
        activeCharacterCard: nextPayload
      }));
      // 字幕校准与普通设置共享 revision，先同步权威快照再读取角色卡列表。
      let settingsRefreshWarning: string | null = null;
      try {
        const refreshedSettings = await refreshSettingsSnapshot(nextPayload.settings);
        if (refreshedSettings === null) {
          settingsRefreshWarning = formatSavedRefreshWarning(null, "设置快照暂时不可用");
        }
      } catch (refreshError) {
        settingsRefreshWarning = formatSavedRefreshWarning(refreshError, "设置快照暂时不可用");
      }
      const refreshWarnings: string[] = settingsRefreshWarning === null ? [] : [settingsRefreshWarning];
      try {
        const nextCards = await fetchCharacterCards(baseUrl);
        setConsoleData((currentData) => ({
          ...currentData,
          characterCards: nextCards,
          activeCharacterCard: nextPayload
        }));
      } catch (refreshError) {
        refreshWarnings.push(formatSavedRefreshWarning(refreshError, "角色卡列表暂时不可用"));
      }
      if (refreshWarnings.length > 0) {
        setCharacterCardSaveWarningMessage(refreshWarnings.join("；"));
      }
      return nextPayload;
    } catch (error) {
      setCharacterCardSaveErrorMessage(error instanceof Error ? error.message : "未知角色字幕语速测算错误");
      throw error;
    } finally {
      setIsSavingCharacterCard(false);
    }
  }

  async function handleCloneCharacterCard(payload: CharacterCardClonePayload): Promise<void> {
    if (isSavingCharacterCard) {
      return;
    }

    setIsSavingCharacterCard(true);
    setCharacterCardSaveErrorMessage(null);
    setCharacterCardSaveWarningMessage(null);

    try {
      const nextPayload = await cloneCharacterCard(baseUrl, payload);
      setConsoleData((currentData) => ({
        ...currentData,
        activeCharacterCard: nextPayload
      }));
      try {
        const nextCards = await fetchCharacterCards(baseUrl);
        setConsoleData((currentData) => ({
          ...currentData,
          characterCards: nextCards,
          activeCharacterCard: nextPayload
        }));
      } catch (refreshError) {
        setCharacterCardSaveWarningMessage(formatSavedRefreshWarning(refreshError, "角色卡列表暂时不可用"));
      }
    } catch (error) {
      setCharacterCardSaveErrorMessage(error instanceof Error ? error.message : "未知角色卡复制错误");
    } finally {
      setIsSavingCharacterCard(false);
    }
  }

  async function handleUploadCharacterCardCover(cardId: string, imageFile: File): Promise<void> {
    if (isSavingCharacterCard) {
      return;
    }

    setIsSavingCharacterCard(true);
    setCharacterCardSaveErrorMessage(null);
    setCharacterCardSaveWarningMessage(null);

    try {
      const nextPayload = await uploadCharacterCardCover(baseUrl, cardId, imageFile);
      setConsoleData((currentData) => ({
        ...currentData,
        activeCharacterCard: nextPayload
      }));
      try {
        const nextCards = await fetchCharacterCards(baseUrl);
        setConsoleData((currentData) => ({
          ...currentData,
          characterCards: nextCards,
          activeCharacterCard: nextPayload
        }));
      } catch (refreshError) {
        setCharacterCardSaveWarningMessage(formatSavedRefreshWarning(refreshError, "角色卡列表暂时不可用"));
      }
    } catch (error) {
      setCharacterCardSaveErrorMessage(error instanceof Error ? error.message : "未知角色卡封面上传错误");
    } finally {
      setIsSavingCharacterCard(false);
    }
  }

  async function handleDeleteCharacterCardCover(cardId: string): Promise<void> {
    if (isSavingCharacterCard) {
      return;
    }

    setIsSavingCharacterCard(true);
    setCharacterCardSaveErrorMessage(null);
    setCharacterCardSaveWarningMessage(null);

    try {
      const nextPayload = await deleteCharacterCardCover(baseUrl, cardId);
      setConsoleData((currentData) => ({
        ...currentData,
        activeCharacterCard: nextPayload
      }));
      try {
        const nextCards = await fetchCharacterCards(baseUrl);
        setConsoleData((currentData) => ({
          ...currentData,
          characterCards: nextCards,
          activeCharacterCard: nextPayload
        }));
      } catch (refreshError) {
        setCharacterCardSaveWarningMessage(formatSavedRefreshWarning(refreshError, "角色卡列表暂时不可用"));
      }
    } catch (error) {
      setCharacterCardSaveErrorMessage(error instanceof Error ? error.message : "未知角色卡封面删除错误");
    } finally {
      setIsSavingCharacterCard(false);
    }
  }

  return {
    isSavingCharacterCard,
    characterCardSaveErrorMessage,
    characterCardSaveWarningMessage,
    handleSaveCharacterCard,
    handleActivateCharacterCard,
    handleUploadCharacterCardVoiceReference,
    handleApplyCharacterCardVoice,
    handleCalibrateCharacterCardVoiceCaption,
    handleGenerateCharacterCardStreamVoxExamples: (cardId, targetModel, card) =>
      generateCharacterCardStreamVoxExamples(baseUrl, cardId, targetModel, card),
    handleCloneCharacterCard,
    handleUploadCharacterCardCover,
    handleDeleteCharacterCardCover
  };
}

function shouldPromptCharacterVoiceSync(voiceStatus: CharacterVoiceStatusPayload): boolean {
  const expectedVoice = voiceStatus.expected;
  return (
    voiceStatus.voice_match_status !== "matched" &&
    expectedVoice.reference_audio_path !== null &&
    expectedVoice.prompt_text.trim().length > 0
  );
}

function mergeAppliedVoiceStatus(
  activeCard: CharacterCardPayload | null,
  appliedCardId: string,
  voiceStatus: CharacterVoiceStatusPayload
): CharacterCardPayload | null {
  if (activeCard?.card?.meta.card_id !== appliedCardId) {
    return activeCard;
  }

  return {
    ...activeCard,
    voice_match_status: voiceStatus.voice_match_status,
    effect_message: voiceStatus.voice_match_status === "matched" ? "角色语音已应用" : activeCard.effect_message
  };
}

/**
 * 把附属操作异常收窄成可展示文字。
 *
 * Args:
 *   error: 异步角色卡操作抛出的未知异常。
 *   fallback: 异常不是 Error 时使用的稳定说明。
 *
 * Returns:
 *   一段适合拼接到保存提示中的错误文字。
 */
function toErrorMessage(error: unknown, fallback: string): string {
  return error instanceof Error && error.message.trim().length > 0 ? error.message : fallback;
}
