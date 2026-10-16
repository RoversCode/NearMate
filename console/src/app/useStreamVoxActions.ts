import { useState } from "react";

import type { StreamVoxConfigPayload, StreamVoxCreateRolePayload, StreamVoxPreviewRolePayload, StreamVoxRuntimeState } from "../api/contracts/streamvox";
import {
  createStreamVoxRole,
  deleteStreamVoxRole,
  fetchStreamVoxRuntime,
  previewStreamVoxRole,
  updateStreamVoxConfig
} from "../api/services/streamvox";
import {
  captureCharacterCardVoiceFromRuntime,
  fetchActiveCharacterCard,
  fetchCharacterCards
} from "../api/services/characterCards";
import { formatSavedRefreshWarning } from "../lib/mutationFeedback";
import type { ConsoleActionContext } from "./consoleActionTypes";

export type StreamVoxActionsState = {
  isSavingStreamVox: boolean;
  streamVoxSaveErrorMessage: string | null;
  streamVoxSaveWarningMessage: string | null;
  handleSaveStreamVoxConfig: (payload: StreamVoxConfigPayload) => Promise<void>;
  handleCreateStreamVoxRole: (payload: StreamVoxCreateRolePayload) => Promise<boolean>;
  handlePreviewStreamVoxRole: (payload: StreamVoxPreviewRolePayload) => Promise<Blob | null>;
  handleDeleteStreamVoxRole: (roleName: string) => Promise<void>;
  handleCaptureStreamVoxVoiceToCharacterCard: () => Promise<void>;
};

/**
 * 管理 StreamVox 运行配置和音色操作。
 *
 * Args:
 *   context: 控制台公共上下文。
 *
 * Returns:
 *   StreamVox 保存状态及其提交动作。
 */
export function useStreamVoxActions({
  baseUrl,
  consoleData,
  setConsoleData,
  refreshSettingsSnapshot
}: ConsoleActionContext): StreamVoxActionsState {
  const [isSavingStreamVox, setIsSavingStreamVox] = useState(false);
  const [streamVoxSaveErrorMessage, setStreamVoxSaveErrorMessage] = useState<string | null>(null);
  const [streamVoxSaveWarningMessage, setStreamVoxSaveWarningMessage] = useState<string | null>(null);

  async function handleSaveStreamVoxConfig(payload: StreamVoxConfigPayload): Promise<void> {
    if (isSavingStreamVox) {
      return;
    }

    const currentRuntime = consoleData.streamVoxRuntime;
    if (
      currentRuntime !== null &&
      currentRuntime.model === payload.model &&
      currentRuntime.device === payload.device &&
      currentRuntime.llama_backend === payload.llama_backend &&
      currentRuntime.role_name === payload.role_name &&
      currentRuntime.streamvox_seed === payload.streamvox_seed &&
      shallowRecordEqual(currentRuntime.inference_options, payload.inference_options)
    ) {
      setStreamVoxSaveErrorMessage(null);
      setStreamVoxSaveWarningMessage(null);
      return;
    }

    setIsSavingStreamVox(true);
    setStreamVoxSaveErrorMessage(null);
    setStreamVoxSaveWarningMessage(null);

    try {
      const nextRuntime = await updateStreamVoxConfig(baseUrl, payload);

      setConsoleData((currentData) => ({
        ...currentData,
        streamVoxRuntime: nextRuntime
      }));
      // StreamVox 配置由 Backend 通过统一用户设置事务提交，会推进全局版本。
      // 必须读取提交点的完整快照；只把设备字段写回旧页面快照会让下一次普通设置
      // 携带旧 revision，刷新失败时还会把“新字段 + 旧版本”的假状态留在页面里。
      const refreshWarnings: string[] = [];
      try {
        const refreshedSettings = await refreshSettingsSnapshot(nextRuntime.settings);
        if (refreshedSettings === null) {
          refreshWarnings.push(formatSavedRefreshWarning(null, "设置快照暂时不可用"));
        }
      } catch (refreshError) {
        refreshWarnings.push(formatSavedRefreshWarning(refreshError, "设置快照暂时不可用"));
      }
      try {
        await refreshStreamVoxRuntime();
      } catch (refreshError) {
        refreshWarnings.push(formatSavedRefreshWarning(refreshError, "StreamVox 状态暂时不可用"));
      }
      if (refreshWarnings.length > 0) {
        setStreamVoxSaveWarningMessage(refreshWarnings.join("；"));
      }
    } catch (error) {
      setStreamVoxSaveErrorMessage(error instanceof Error ? error.message : "未知 StreamVox 保存错误");
    } finally {
      setIsSavingStreamVox(false);
    }
  }

  /**
   * 创建音色并刷新列表，让表单能依据真实保存结果决定是否清空草稿。
   *
   * Args:
   *   payload: 音色名称、参考文本与参考音频。
   *
   * Returns:
   *   创建提交成功返回 true；提交失败或重复提交返回 false。附属状态刷新失败
   *   只显示提醒，不改变创建成功结果。
   */
  async function handleCreateStreamVoxRole(payload: StreamVoxCreateRolePayload): Promise<boolean> {
    if (isSavingStreamVox) {
      return false;
    }

    setIsSavingStreamVox(true);
    setStreamVoxSaveErrorMessage(null);
    setStreamVoxSaveWarningMessage(null);

    try {
      const nextRuntime = await createStreamVoxRole(baseUrl, payload);
      // 创建接口返回的 runtime 已经包含新音色；先写入本地，后续列表刷新失败
      // 不能把已成功创建的音色误报成失败。
      setConsoleData((currentData) => ({
        ...currentData,
        streamVoxRuntime: nextRuntime
      }));
      try {
        await refreshStreamVoxRuntime();
      } catch (refreshError) {
        setStreamVoxSaveWarningMessage(formatSavedRefreshWarning(refreshError, "StreamVox 状态暂时不可用"));
      }
      return true;
    } catch (error) {
      setStreamVoxSaveErrorMessage(error instanceof Error ? error.message : "未知音色创建错误");
      return false;
    } finally {
      setIsSavingStreamVox(false);
    }
  }

  async function handlePreviewStreamVoxRole(payload: StreamVoxPreviewRolePayload): Promise<Blob | null> {
    if (isSavingStreamVox) {
      return null;
    }

    setIsSavingStreamVox(true);
    setStreamVoxSaveErrorMessage(null);
    setStreamVoxSaveWarningMessage(null);

    try {
      return await previewStreamVoxRole(baseUrl, payload);
    } catch (error) {
      setStreamVoxSaveErrorMessage(error instanceof Error ? error.message : "未知音色试听错误");
      return null;
    } finally {
      setIsSavingStreamVox(false);
    }
  }

  async function handleDeleteStreamVoxRole(roleName: string): Promise<void> {
    if (isSavingStreamVox) {
      return;
    }

    setIsSavingStreamVox(true);
    setStreamVoxSaveErrorMessage(null);
    setStreamVoxSaveWarningMessage(null);

    try {
      const nextRuntime = await deleteStreamVoxRole(baseUrl, roleName);
      // 删除接口返回删除后的完整 runtime；先采用它，避免附属 GET 失败触发重复删除。
      setConsoleData((currentData) => ({
        ...currentData,
        streamVoxRuntime: nextRuntime
      }));
      try {
        await refreshStreamVoxRuntime();
      } catch (refreshError) {
        setStreamVoxSaveWarningMessage(formatSavedRefreshWarning(refreshError, "StreamVox 状态暂时不可用"));
      }
    } catch (error) {
      setStreamVoxSaveErrorMessage(error instanceof Error ? error.message : "未知音色删除错误");
    } finally {
      setIsSavingStreamVox(false);
    }
  }

  async function handleCaptureStreamVoxVoiceToCharacterCard(): Promise<void> {
    if (isSavingStreamVox) {
      return;
    }

    setIsSavingStreamVox(true);
    setStreamVoxSaveErrorMessage(null);
    setStreamVoxSaveWarningMessage(null);

    try {
      const activeCard =
        consoleData.activeCharacterCard?.card === undefined || consoleData.activeCharacterCard.card === null
          ? await fetchActiveCharacterCard(baseUrl)
          : consoleData.activeCharacterCard;

      if (activeCard.card === null) {
        throw new Error("当前没有可更新的激活角色卡");
      }

      const cardId = activeCard.card.meta.card_id;
      const nextActiveCard = await captureCharacterCardVoiceFromRuntime(baseUrl, cardId);
      // 捕获接口已经返回更新后的角色卡；先写入当前卡，后续列表读取只负责补齐
      // 附属视图，不能决定这次写入是否成功。
      setConsoleData((currentData) => ({
        ...currentData,
        activeCharacterCard: nextActiveCard
      }));
      // 角色卡语音捕获已经提交设置事务；先同步 revision，再读取附属列表和
      // runtime，避免这些只读请求失败后留下旧版本。
      let settingsRefreshWarning: string | null = null;
      try {
        const refreshedSettings = await refreshSettingsSnapshot(nextActiveCard.settings);
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
        activeCharacterCard: nextActiveCard,
        streamVoxRuntime: runtimeResult.status === "fulfilled" ? runtimeResult.value : currentData.streamVoxRuntime
      }));
      if (refreshWarnings.length > 0) {
        setStreamVoxSaveWarningMessage(refreshWarnings.join("；"));
      }
    } catch (error) {
      setStreamVoxSaveErrorMessage(error instanceof Error ? error.message : "未知角色卡语音更新错误");
    } finally {
      setIsSavingStreamVox(false);
    }
  }

  async function refreshStreamVoxRuntime(): Promise<StreamVoxRuntimeState> {
    const nextRuntime = await fetchStreamVoxRuntime(baseUrl);
    setConsoleData((currentData) => ({
      ...currentData,
      streamVoxRuntime: nextRuntime
    }));
    return nextRuntime;
  }

  return {
    isSavingStreamVox,
    streamVoxSaveErrorMessage,
    streamVoxSaveWarningMessage,
    handleSaveStreamVoxConfig,
    handleCreateStreamVoxRole,
    handlePreviewStreamVoxRole,
    handleDeleteStreamVoxRole,
    handleCaptureStreamVoxVoiceToCharacterCard
  };
}

function shallowRecordEqual(left: Record<string, unknown>, right: Record<string, unknown>): boolean {
  const leftKeys = Object.keys(left);
  const rightKeys = Object.keys(right);
  if (leftKeys.length !== rightKeys.length) {
    return false;
  }
  return leftKeys.every((key) => left[key] === right[key]);
}
