import type {
  Live2dModelCatalogEntry,
  Live2dModelSwitchState
} from "../../../shared/live2dModelTypes";
import type { ConsoleTarget } from "../../../shared/shellNavigation";

/**
 * panel bridge 只声明当前面板窗口真正会消费的 preload 契约。
 * 这样可以在不额外扩散全局类型文件的前提下，把窗口边界收在本任务内。
 */
export type CompanionPanelBridge = {
  reportDiagnostic(level: "info" | "warn" | "error", message: string, details?: unknown): void;
  closePanel(): void;
  openConsole?(target: ConsoleTarget): void;
  resetAvatarLayout?(): void;
  /**
   * 提交人物切换意图，编号用于识别同一人物的重试。
   * @param modelId 目标人物的稳定目录名。
   * @param requestId 当前点击的标准 UUID；旧调用方可省略。
   * @returns 不返回值；Avatar 通过状态订阅回传结果。
   */
  requestModelSwitch(modelId: string, requestId?: string): void;
  onModelSwitchStateChanged(listener: (state: Live2dModelSwitchState) => void): () => void;
  runtime: {
    controlGateway: {
      websocketUrl: string;
      httpBaseUrl: string;
    };
    live2d: {
      catalog: Live2dModelCatalogEntry[];
    };
  };
};

declare global {
  interface Window {
    companionPanel?: CompanionPanelBridge;
  }
}

/**
 * 读取 panel preload bridge。
 *
 * Returns:
 *   当前窗口注入的 panel bridge；浏览器预览或测试缺失时返回 null。
 */
export function getPanelBridge(): CompanionPanelBridge | null {
  return window.companionPanel ?? null;
}

/**
 * 读取 panel 可用 Live2D 模型目录。
 *
 * Args:
 *   panelBridge: 当前 panel preload bridge。
 *
 * Returns:
 *   bridge 缺失时返回空数组，避免 UI 层到处做可空判断。
 */
export function getPanelModelCatalog(
  panelBridge: CompanionPanelBridge | null
): readonly Live2dModelCatalogEntry[] {
  return panelBridge?.runtime.live2d.catalog ?? [];
}
