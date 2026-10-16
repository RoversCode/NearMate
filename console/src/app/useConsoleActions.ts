import { useCallback, useRef } from "react";

import type { BootstrapInitializePayload, BootstrapStatus, QWeatherApiKeyPayload } from "../api/contracts/bootstrap";
import type {
  CharacterCard,
  CharacterCardClonePayload,
  CharacterCardPayload,
  CharacterCardStreamVoxExampleSet,
  CharacterCardVoiceReferenceUploadResult
} from "../api/contracts/characterCards";
import { normalizeConsoleSettings, type ConsoleSettings } from "../api/contracts/settings";
import type { LlmConfigUpdatePayload } from "../api/contracts/llm";
import type { StreamVoxConfigPayload, StreamVoxCreateRolePayload, StreamVoxPreviewRolePayload } from "../api/contracts/streamvox";
import type { WakeResponseConfig, WakeResponseGeneratedTexts, WakeResponseItem } from "../api/contracts/wakeResponse";
import type { ConsoleData } from "./consoleTypes";
import { fetchSettings } from "../api/services/settings";
import { useBootstrapActions } from "./useBootstrapActions";
import type { ConsoleActionContext } from "./consoleActionTypes";
import { useCharacterCardActions } from "./useCharacterCardActions";
import { useStreamVoxActions } from "./useStreamVoxActions";
import { useWakeResponseActions } from "./useWakeResponseActions";

export type ConsoleActionsController = {
  isSavingBootstrap: boolean;
  bootstrapSaveErrorMessage: string | null;
  bootstrapSaveWarningMessage: string | null;
  isSavingSettings: boolean;
  settingsSaveErrorMessage: string | null;
  isSavingCharacterCard: boolean;
  characterCardSaveErrorMessage: string | null;
  characterCardSaveWarningMessage: string | null;
  isSavingStreamVox: boolean;
  streamVoxSaveErrorMessage: string | null;
  streamVoxSaveWarningMessage: string | null;
  handleInitializeBootstrap: (payload: BootstrapInitializePayload) => Promise<boolean>;
  handleSaveLlmConfig: (payload: LlmConfigUpdatePayload) => Promise<boolean>;
  handleSaveQWeatherApiKey: (payload: QWeatherApiKeyPayload) => Promise<boolean>;
  handleSaveSettings: (nextSettings: ConsoleSettings) => Promise<boolean>;
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
  handleDeleteWakeResponseItem: (cardId: string, itemId: string) => Promise<void>;
  handleGenerateWakeResponse: (cardId: string) => Promise<WakeResponseGeneratedTexts>;
  handleSynthesizeWakeResponse: (cardId: string, itemId: string, bucket: string, text: string) => Promise<WakeResponseItem>;
  handleSaveWakeResponse: (cardId: string, config: WakeResponseConfig) => Promise<WakeResponseConfig>;
  handleCloneCharacterCard: (payload: CharacterCardClonePayload) => Promise<void>;
  handleUploadCharacterCardCover: (cardId: string, imageFile: File) => Promise<void>;
  handleDeleteCharacterCardCover: (cardId: string) => Promise<void>;
  handleSaveStreamVoxConfig: (payload: StreamVoxConfigPayload) => Promise<void>;
  handleCreateStreamVoxRole: (payload: StreamVoxCreateRolePayload) => Promise<boolean>;
  handlePreviewStreamVoxRole: (payload: StreamVoxPreviewRolePayload) => Promise<Blob | null>;
  handleDeleteStreamVoxRole: (roleName: string) => Promise<void>;
  handleCaptureStreamVoxVoiceToCharacterCard: () => Promise<void>;
  refreshSettingsSnapshot: (candidateSettings?: ConsoleSettings) => Promise<ConsoleSettings | null>;
};

type ConsoleActionsProps = {
  baseUrl: string;
  consoleData: ConsoleData;
  setConsoleData: import("react").Dispatch<import("react").SetStateAction<ConsoleData>>;
  setBootstrapStatus: import("react").Dispatch<import("react").SetStateAction<BootstrapStatus | null>>;
  loadConsoleData: (requestBaseUrl: string, shouldApplyResult?: () => boolean) => Promise<void>;
};

/**
 * 统一管理控制台的提交动作与保存状态。
 *
 * Args:
 *   baseUrl: 控制面 HTTP 根地址。
 *   consoleData: 当前已加载的控制台数据快照。
 *   setConsoleData: 控制台数据写入器。
 *   setBootstrapStatus: 首次初始化状态写入器。
 *   loadConsoleData: bootstrap 成功后的整页重新加载入口。
 *
 * Returns:
 *   页面提交、激活、上传和试听动作，以及对应的保存状态与错误提示。
 */
export function useConsoleActions({
  baseUrl,
  consoleData,
  setConsoleData,
  setBootstrapStatus,
  loadConsoleData
}: ConsoleActionsProps): ConsoleActionsController {
  // 只用地址世代隔离不同 Backend。相同地址下的并发回读都要参与合并，
  // 因为较晚发出的请求可能失败，而较早发出的请求仍可能拿到最新快照。
  const settingsBaseUrlGenerationRef = useRef(0);
  const latestBaseUrlRef = useRef(baseUrl);
  if (latestBaseUrlRef.current !== baseUrl) {
    latestBaseUrlRef.current = baseUrl;
    // 地址可能在 A → B → A 间切换；世代号能避免第一代 A 的迟到响应
    // 被误认为当前这一代 A 的结果。
    settingsBaseUrlGenerationRef.current += 1;
  }

  /**
   * 在某个专用配置接口成功后重新读取普通设置快照。
   *
   * 各配置领域共享同一 settings_revision。专用接口成功后只更新本领域的
   * React 状态会留下旧版本，下一次普通设置提交就会被 Backend 以 409 拒绝。
   * 这里统一通过 GET 取得权威投影，并用函数式更新丢弃迟到的旧响应。
   * 同步失败不会把已经成功的原操作改报失败；下一个 WebSocket 事件或操作仍可重试。
   *
   * Args:
   *   candidateSettings: 专用配置接口随成功响应返回的提交点快照。传入时直接
   *     采用该快照；省略时兼容旧 Backend，回读 /api/settings。
   *
   * Returns:
   *   成功取得并应用的设置快照；读取失败时返回 null。
  */
  const refreshSettingsSnapshot = useCallback(async (
    candidateSettings?: ConsoleSettings
  ): Promise<ConsoleSettings | null> => {
    const requestBaseUrlGeneration = settingsBaseUrlGenerationRef.current;
    const requestBaseUrl = baseUrl;

    // 新版专用 mutation 已经把同一事务冻结的完整快照放进响应；直接写入
    // 可以避免一次额外 GET，也避免附属读取暂时失败时继续携带旧 revision。
    if (candidateSettings !== undefined) {
      if (
        requestBaseUrlGeneration !== settingsBaseUrlGenerationRef.current
        || requestBaseUrl !== latestBaseUrlRef.current
      ) {
        return null;
      }
      const normalizedSettings = normalizeConsoleSettings(candidateSettings);
      setConsoleData((currentData) => {
        if (
          currentData.settings !== null
          && currentData.settings.settings_revision > normalizedSettings.settings_revision
        ) {
          return currentData;
        }
        return {
          ...currentData,
          settings: normalizedSettings
        };
      });
      return normalizedSettings;
    }

    try {
      const nextSettings = await fetchSettings(requestBaseUrl);
      if (
        requestBaseUrlGeneration !== settingsBaseUrlGenerationRef.current
        || requestBaseUrl !== latestBaseUrlRef.current
      ) {
        return null;
      }
      setConsoleData((currentData) => {
        if (
          currentData.settings !== null
          && currentData.settings.settings_revision > nextSettings.settings_revision
        ) {
          return currentData;
        }
        return {
          ...currentData,
          settings: nextSettings
        };
      });
      return nextSettings;
    } catch {
      return null;
    }
  }, [baseUrl, setConsoleData]);

  const actionContext: ConsoleActionContext = {
    baseUrl,
    consoleData,
    setConsoleData,
    setBootstrapStatus,
    loadConsoleData,
    refreshSettingsSnapshot
  };
  const bootstrapActions = useBootstrapActions(actionContext);
  const characterCardActions = useCharacterCardActions(actionContext);
  const wakeResponseActions = useWakeResponseActions(actionContext);
  const streamVoxActions = useStreamVoxActions(actionContext);

  return {
    ...bootstrapActions,
    ...characterCardActions,
    ...wakeResponseActions,
    ...streamVoxActions,
    refreshSettingsSnapshot
  };
}
