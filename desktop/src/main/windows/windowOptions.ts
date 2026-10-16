import type { BrowserWindowConstructorOptions } from "electron";
import { DEFAULT_AVATAR_PRESENTATION_PROFILE } from "../../shared/avatarPresentationProfile";
import { PANEL_VIEW_SIZES } from "../../shared/shellNavigation";

/**
 * SAFE_DEFAULT_AVATAR_WINDOW_SIZE 只描述 avatar window 创建瞬间可安全使用的保守尺寸。
 * 主进程在这个阶段还不知道最终人物 id，所以这里只能读取默认展示档案，后续再由 renderer 链路通知 main 调整真实人物尺寸。
 */
export const SAFE_DEFAULT_AVATAR_WINDOW_SIZE = {
  width: DEFAULT_AVATAR_PRESENTATION_PROFILE.windowWidth,
  height: DEFAULT_AVATAR_PRESENTATION_PROFILE.windowHeight
} as const;

/**
 * 聊天窗口默认尺寸贴近桌面 IM 窗口。
 * 它是可聚焦、可缩放的普通窗口，因此只把初始尺寸集中成常量，后续不参与侧边板定位。
 */
export const CHAT_WINDOW_SIZE = {
  width: 900,
  height: 640
} as const;

/**
 * buildAvatarWindowOptions 负责生成角色窗的 BrowserWindow 配置。
 * 核心入参是角色窗 preload 路径，输出为透明、无边框、跳过任务栏的窗口参数。
 * 边界异常：如果调用方传入的 preloadPath 无效，真正的加载失败会在 Electron 创建窗口后暴露。
 */
export function buildAvatarWindowOptions(
  preloadPath: string,
  bootstrapArgument: string
): BrowserWindowConstructorOptions {
  return {
    width: SAFE_DEFAULT_AVATAR_WINDOW_SIZE.width,
    height: SAFE_DEFAULT_AVATAR_WINDOW_SIZE.height,
    show: false,
    transparent: true,
    frame: false,
    hasShadow: false,
    skipTaskbar: true,
    alwaysOnTop: false,
    backgroundColor: "#00000000",
    resizable: false,
    webPreferences: {
      preload: preloadPath,
      additionalArguments: [bootstrapArgument],
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true
    }
  };
}

/**
 * buildPanelWindowOptions 负责生成独立面板窗的 BrowserWindow 配置。
 * 核心入参是面板窗 preload 路径，输出为默认隐藏、可聚焦、非透明的面板参数。
 * 边界异常：面板实际展示位置不在这里决定，而是由更外层 bootstrap 与 controller 协同计算。
 */
export function buildPanelWindowOptions(
  preloadPath: string,
  bootstrapArgument: string
): BrowserWindowConstructorOptions {
  return {
    width: PANEL_VIEW_SIZES.models.width,
    height: PANEL_VIEW_SIZES.models.height,
    show: false,
    transparent: false,
    frame: false,
    resizable: false,
    focusable: true,
    skipTaskbar: true,
    backgroundColor: "#f6ecde",
    webPreferences: {
      preload: preloadPath,
      additionalArguments: [bootstrapArgument],
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true
    }
  };
}

/**
 * buildChatWindowOptions 负责生成独立聊天窗口的 BrowserWindow 配置。
 * 核心入参是聊天窗 preload 路径，输出为默认隐藏、可聚焦、透明无边框、跳过任务栏的浮层参数。
 * 边界异常：窗口展示时机和关闭策略由 bootstrap IPC 负责，这里只定义创建参数。
 */
export function buildChatWindowOptions(
  preloadPath: string,
  bootstrapArgument: string
): BrowserWindowConstructorOptions {
  return {
    width: CHAT_WINDOW_SIZE.width,
    height: CHAT_WINDOW_SIZE.height,
    show: false,
    transparent: true,
    frame: false,
    hasShadow: false,
    resizable: true,
    minWidth: 760,
    minHeight: 520,
    focusable: true,
    skipTaskbar: true,
    title: "聊天窗口",
    backgroundColor: "#00000000",
    webPreferences: {
      preload: preloadPath,
      additionalArguments: [bootstrapArgument],
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true
    }
  };
}
