import type { WakeResponseConfig, WakeResponseGeneratedTexts, WakeResponseItem } from "../api/contracts/wakeResponse";
import {
  deleteWakeResponseItem,
  generateWakeResponseTexts,
  synthesizeWakeResponseItem,
  saveWakeResponseConfig
} from "../api/services/wakeResponse";
import type { ConsoleActionContext } from "./consoleActionTypes";

export type WakeResponseActionsState = {
  handleDeleteWakeResponseItem: (cardId: string, itemId: string) => Promise<void>;
  handleGenerateWakeResponse: (cardId: string) => Promise<WakeResponseGeneratedTexts>;
  handleSynthesizeWakeResponse: (cardId: string, itemId: string, bucket: string, text: string) => Promise<WakeResponseItem>;
  handleSaveWakeResponse: (cardId: string, config: WakeResponseConfig) => Promise<WakeResponseConfig>;
};

/**
 * 管理唤醒回复配置的保存动作。
 *
 * Args:
 *   context: 控制台公共上下文。
 *
 * Returns:
 *   唤醒回复的删除、单行合成和保存接口。
 */
export function useWakeResponseActions({ baseUrl, setConsoleData }: ConsoleActionContext): WakeResponseActionsState {
  function updateWakeResponseConfig(nextConfig: WakeResponseConfig): void {
    setConsoleData((currentData) => ({
      ...currentData,
      wakeResponseConfig: nextConfig
    }));
  }

  async function handleDeleteWakeResponseItem(cardId: string, itemId: string): Promise<void> {
    await deleteWakeResponseItem(baseUrl, cardId, itemId);
  }

  async function handleSynthesizeWakeResponse(cardId: string, itemId: string, bucket: string, text: string): Promise<WakeResponseItem> {
    return await synthesizeWakeResponseItem(baseUrl, cardId, itemId, bucket, text);
  }

  async function handleSaveWakeResponse(cardId: string, config: WakeResponseConfig): Promise<WakeResponseConfig> {
    const nextConfig = await saveWakeResponseConfig(baseUrl, cardId, config);
    updateWakeResponseConfig(nextConfig);
    return nextConfig;
  }

  return {
    handleDeleteWakeResponseItem,
    handleGenerateWakeResponse: (cardId) => generateWakeResponseTexts(baseUrl, cardId),
    handleSynthesizeWakeResponse,
    handleSaveWakeResponse
  };
}
