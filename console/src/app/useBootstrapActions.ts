import { useRef, useState } from "react";

import type { BootstrapInitializePayload, QWeatherApiKeyPayload } from "../api/contracts/bootstrap";
import type { LlmConfigUpdatePayload } from "../api/contracts/llm";
import type { ConsoleSettings } from "../api/contracts/settings";
import { initializeBootstrap, updateQWeatherApiKey } from "../api/services/bootstrap";
import { fetchBootstrapStatus } from "../api/services/bootstrap";
import { updateLlmConfig } from "../api/services/llm";
import { HttpApiError } from "../api/httpClient";
import { fetchSettings, updateSettings } from "../api/services/settings";
import { formatSavedRefreshWarning } from "../lib/mutationFeedback";
import type { ConsoleActionContext } from "./consoleActionTypes";

export type BootstrapActionsState = {
  isSavingBootstrap: boolean;
  bootstrapSaveErrorMessage: string | null;
  bootstrapSaveWarningMessage: string | null;
  isSavingSettings: boolean;
  settingsSaveErrorMessage: string | null;
  handleInitializeBootstrap: (payload: BootstrapInitializePayload) => Promise<boolean>;
  handleSaveLlmConfig: (payload: LlmConfigUpdatePayload) => Promise<boolean>;
  handleSaveQWeatherApiKey: (payload: QWeatherApiKeyPayload) => Promise<boolean>;
  handleSaveSettings: (nextSettings: ConsoleSettings) => Promise<boolean>;
};

/**
 * 管理 bootstrap、天气密钥和控制台设置保存动作。
 *
 * Args:
 *   context: 控制台公共上下文。
 *
 * Returns:
 *   bootstrap 和设置相关的保存状态及提交方法。
 */
export function useBootstrapActions({ baseUrl, setConsoleData, setBootstrapStatus, loadConsoleData }: ConsoleActionContext): BootstrapActionsState {
  const [isSavingBootstrap, setIsSavingBootstrap] = useState(false);
  const [bootstrapSaveErrorMessage, setBootstrapSaveErrorMessage] = useState<string | null>(null);
  const [bootstrapSaveWarningMessage, setBootstrapSaveWarningMessage] = useState<string | null>(null);
  const [isSavingSettings, setIsSavingSettings] = useState(false);
  const [settingsSaveErrorMessage, setSettingsSaveErrorMessage] = useState<string | null>(null);
  const bootstrapSaveInFlightRef = useRef(false);
  const settingsSaveInFlightRef = useRef(false);

  /**
   * 提交启动密钥并恢复普通控制台加载。
   *
   * Args:
   *   payload: 已裁剪后的启动密钥表单。
   *
   * Returns:
   *   保存成功并触发数据刷新时返回真；重复提交或保存失败时返回假，调用方据此决定是否清空表单。
   */
  async function handleInitializeBootstrap(payload: BootstrapInitializePayload): Promise<boolean> {
    if (bootstrapSaveInFlightRef.current) {
      return false;
    }

    bootstrapSaveInFlightRef.current = true;
    setIsSavingBootstrap(true);
    setBootstrapSaveErrorMessage(null);
    setBootstrapSaveWarningMessage(null);

    try {
      await initializeBootstrap(baseUrl, payload);
      try {
        await loadConsoleData(baseUrl);
      } catch (refreshError) {
        // 初始化写入已提交；整页重载只是把新状态投影到 Console，失败时不能
        // 让引导页把一次成功初始化误报成失败。
        setBootstrapSaveWarningMessage(formatSavedRefreshWarning(refreshError, "控制台状态暂时不可用"));
      }
      return true;
    } catch (error) {
      setBootstrapSaveErrorMessage(error instanceof Error ? error.message : "未知初始化保存错误");
      return false;
    } finally {
      bootstrapSaveInFlightRef.current = false;
      setIsSavingBootstrap(false);
    }
  }

  /**
   * 验证并保存可选和风天气服务密钥。
   *
   * Args:
   *   payload: 和风天气 API Host 与 API Key。
   *
   * Returns:
   *   保存成功并更新 bootstrap 状态时返回真；重复提交或保存失败时返回假，调用方据此决定是否清空表单。
   */
  async function handleSaveQWeatherApiKey(payload: QWeatherApiKeyPayload): Promise<boolean> {
    if (bootstrapSaveInFlightRef.current) {
      return false;
    }

    bootstrapSaveInFlightRef.current = true;
    setIsSavingBootstrap(true);
    setBootstrapSaveErrorMessage(null);
    setBootstrapSaveWarningMessage(null);

    try {
      const nextBootstrapStatus = await updateQWeatherApiKey(baseUrl, payload);
      setBootstrapStatus(nextBootstrapStatus);
      return true;
    } catch (error) {
      setBootstrapSaveErrorMessage(error instanceof Error ? error.message : "未知天气密钥保存错误");
      return false;
    } finally {
      bootstrapSaveInFlightRef.current = false;
      setIsSavingBootstrap(false);
    }
  }

  /**
   * 保存 LLM 厂商凭据和用途模型分配。
   *
   * Args:
   *   payload: 已裁剪的厂商凭据和真实模型选择。
   *
   * Returns:
   *   保存成功并刷新状态时返回真；重复提交或保存失败时返回假。
   */
  async function handleSaveLlmConfig(payload: LlmConfigUpdatePayload): Promise<boolean> {
    if (bootstrapSaveInFlightRef.current) {
      return false;
    }

    bootstrapSaveInFlightRef.current = true;
    setIsSavingBootstrap(true);
    setBootstrapSaveErrorMessage(null);
    setBootstrapSaveWarningMessage(null);

    try {
      const nextLlmConfig = await updateLlmConfig(baseUrl, payload);
      setConsoleData((currentData) => ({
        ...currentData,
        llmConfig: nextLlmConfig
      }));
      try {
        const nextBootstrapStatus = await fetchBootstrapStatus(baseUrl);
        setBootstrapStatus(nextBootstrapStatus);
      } catch (refreshError) {
        // LLM 配置接口已经完成持久化；状态读取失败只影响页面提示，不应诱导
        // 用户再次提交同一份凭据和模型绑定。
        setBootstrapSaveWarningMessage(formatSavedRefreshWarning(refreshError, "初始化状态暂时不可用"));
      }
      return true;
    } catch (error) {
      setBootstrapSaveErrorMessage(error instanceof Error ? error.message : "未知 LLM 配置保存错误");
      return false;
    } finally {
      bootstrapSaveInFlightRef.current = false;
      setIsSavingBootstrap(false);
    }
  }

  /**
   * 保存控制台设置并同步本地缓存。
   *
   * Args:
   *   nextSettings: 用户提交的完整设置对象。
   *
   * Returns:
   *   成功时更新 settings 快照并返回 true，失败或重复提交返回 false，供录入表单保留草稿。
   */
  async function handleSaveSettings(nextSettings: ConsoleSettings): Promise<boolean> {
    if (settingsSaveInFlightRef.current) {
      return false;
    }

    settingsSaveInFlightRef.current = true;
    setIsSavingSettings(true);
    setSettingsSaveErrorMessage(null);

    try {
      const savedSettings = await updateSettings(baseUrl, nextSettings);
      setConsoleData((currentData) => ({
        ...currentData,
        // HTTP 响应和 settings.changed 走两条传输链，抵达顺序不等于提交顺序。
        // 已收到更新 revision 时必须保留它，不能被迟到的旧响应倒退。
        settings: currentData.settings !== null
          && currentData.settings.settings_revision > savedSettings.settings_revision
          ? currentData.settings
          : savedSettings
      }));
      return true;
    } catch (error) {
      if (error instanceof HttpApiError && error.status === 409) {
        // 设置版本冲突时先读取权威快照，再把失败交还给表单；绝不能把旧的完整
        // 快照自动重放，否则会悄悄覆盖刚刚由音频、桌宠或其它窗口提交的字段。
        try {
          const latestSettings = await fetchSettings(baseUrl);
          setConsoleData((currentData) => ({
            ...currentData,
            settings: currentData.settings !== null
              && currentData.settings.settings_revision > latestSettings.settings_revision
              ? currentData.settings
              : latestSettings
          }));
          setSettingsSaveErrorMessage("设置已被其他操作更新，已刷新当前设置；请再次提交你的修改。");
        } catch (refreshError) {
          setSettingsSaveErrorMessage(
            `${error.message}；刷新当前设置失败：${refreshError instanceof Error ? refreshError.message : "请手动刷新页面"}`
          );
        }
        return false;
      }
      setSettingsSaveErrorMessage(error instanceof Error ? error.message : "未知设置保存错误");
      return false;
    } finally {
      settingsSaveInFlightRef.current = false;
      setIsSavingSettings(false);
    }
  }

  return {
    isSavingBootstrap,
    bootstrapSaveErrorMessage,
    bootstrapSaveWarningMessage,
    isSavingSettings,
    settingsSaveErrorMessage,
    handleInitializeBootstrap,
    handleSaveLlmConfig,
    handleSaveQWeatherApiKey,
    handleSaveSettings
  };
}
