import { BrowserWindow } from "electron";
import { applyAvatarWindowPresentationMode } from "./avatarWindowPresentation";
import { buildAvatarWindowOptions } from "./windowOptions";

/**
 * createAvatarWindow 负责创建真实角色窗，并补齐角色窗专属的交互行为。
 * 核心入参是角色窗 preload 路径，输出为默认被动展示的 BrowserWindow 实例。
 * 边界异常：页面加载、状态编排与面板联动不在这里处理，统一留给 bootstrap 和 controller。
 */
export function createAvatarWindow(
  preloadPath: string,
  bootstrapArgument: string
): BrowserWindow {
  const window = new BrowserWindow(
    buildAvatarWindowOptions(preloadPath, bootstrapArgument)
  );

  /**
   * 启动默认进入被动展示：不置顶、鼠标穿透，避免透明窗口矩形挡住桌面图标。
   */
  applyAvatarWindowPresentationMode(window, "passive");

  return window;
}
