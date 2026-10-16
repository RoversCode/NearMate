import { BrowserWindow } from "electron";
import { buildChatWindowOptions } from "./windowOptions";

/**
 * createChatWindow 负责创建独立聊天窗口。
 * 核心入参是聊天窗 preload 路径，输出为尚未展示的普通 BrowserWindow 实例。
 * 边界：窗口加载、显示、聚焦和销毁策略由 bootstrap 统一处理。
 */
export function createChatWindow(
  preloadPath: string,
  bootstrapArgument: string
): BrowserWindow {
  return new BrowserWindow(buildChatWindowOptions(preloadPath, bootstrapArgument));
}
