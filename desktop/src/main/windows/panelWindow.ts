import { BrowserWindow } from "electron";
import { buildPanelWindowOptions } from "./windowOptions";

/**
 * createPanelWindow 负责创建真实面板窗。
 * 核心入参是面板窗 preload 路径，输出为尚未展示的可聚焦 BrowserWindow 实例。
 * 边界：面板打开坐标、聚焦时机和关闭逻辑由 controller 与 bootstrap 驱动，这里只负责实例化。
 */
export function createPanelWindow(
  preloadPath: string,
  bootstrapArgument: string
): BrowserWindow {
  return new BrowserWindow(buildPanelWindowOptions(preloadPath, bootstrapArgument));
}
