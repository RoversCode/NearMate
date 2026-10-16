import { screen, type BrowserWindow } from "electron";
import {
  createCompanionSummonController,
  type CompanionSummonController
} from "./controllers/companionSummonController";
import { createShellController } from "./controllers/shellController";
import { WINDOW_KINDS } from "./contracts/windowKinds";
import { attachRendererDiagnostics } from "./diagnostics/rendererDiagnostics";
import type { WindowIdentityRegistry } from "./ipc/windowIdentityRegistry";
import { createAvatarWindow } from "./windows/avatarWindow";
import { applyAvatarWindowPresentationMode } from "./windows/avatarWindowPresentation";
import type { AvatarWindowInteractionController } from "./windows/avatarWindowInteraction";
import { createChatWindow } from "./windows/chatWindow";
import { createChatWindowDragController, type ChatWindowDragController } from "./windows/chatWindowDrag";
import { createChatWindowMotion, type ChatWindowMotionController } from "./windows/chatWindowMotion";
import { createChatWindowResizeController, type ChatWindowResizeController } from "./windows/chatWindowResize";
import { createChatWindowLifecycleOwner, type ChatWindowLifecycleOwner } from "./windows/chatWindowLifecycle";
import { createPanelDismissGuard } from "./windows/panelDismissGuard";
import { createPanelWindow } from "./windows/panelWindow";
import { createPanelWindowMotion } from "./windows/panelWindowMotion";
import { watchWindowFirstPaint } from "./windows/windowFirstPaint";
import { createWindowBootstrapArguments } from "./windows/windowBootstrapPayloads";
import {
  loadWindowContent,
  resolveWindowPreloadPaths
} from "./windows/windowContent";

/**
 * PANEL_BLUR_CLOSE_CONFIRM_DELAY_MS 是侧边板失焦关闭的确认窗口。
 * 业务意图是吸收 Electron 在点击面板内部控件时可能出现的短暂 blur/focus 抖动。
 */
const PANEL_BLUR_CLOSE_CONFIRM_DELAY_MS = 80;

/**
 * ShellRuntime 是当前主进程持有的三窗口应用壳。
 */
export type ShellRuntime = {
  avatarWindow: BrowserWindow;
  panelWindow: BrowserWindow;
  chatWindow: BrowserWindow;
  chatWindowMotion: ChatWindowMotionController;
  chatWindowDrag: ChatWindowDragController;
  chatWindowResize: ChatWindowResizeController;
  chatLifecycle?: ChatWindowLifecycleOwner;
  controller: ReturnType<typeof createShellController>;
  companionSummonController: CompanionSummonController;
  stopPanelBlurCloseGuard: () => void;
};

type CreateShellRuntimeInput = {
  mainModuleDir: string;
  windowIdentities: WindowIdentityRegistry;
  avatarInteraction: AvatarWindowInteractionController;
  isAppQuitting(): boolean;
  waitForFirstPaint?: boolean;
  onRuntimeClosed(input: { closingWindow: BrowserWindow }): void;
};

type ShellRuntimeTeardownOptions = {
  closingWindow?: BrowserWindow;
  destroyTray(): void;
  stopAecSidecar(): void;
  resetAvatarInteraction(): void;
};

/**
 * createShellRuntime 创建并接线桌宠、侧边板与聊天窗组成的应用壳。
 *
 * Args:
 *   input: 主模块路径、avatar 交互控制器、生命周期回调与可选的安装态首帧等待。
 *
 * Returns:
 *   已完成页面加载和控制器接线的 shell runtime；安装态还确认隐藏 Avatar 已准备首帧。
 */
export async function createShellRuntime(input: CreateShellRuntimeInput): Promise<ShellRuntime> {
  // 已创建窗口登记簿：装配中途抛错时 runtime 尚未成形，必须就地销毁它们，避免留下无收口的孤儿窗口。
  const createdWindows: BrowserWindow[] = [];
  let firstPaint: ReturnType<typeof watchWindowFirstPaint> | null = null;
  try {
    const preloadPaths = resolveWindowPreloadPaths(input.mainModuleDir);
    const bootstrapArguments = createWindowBootstrapArguments();

    const avatarWindow = createAvatarWindow(
      preloadPaths.avatar,
      bootstrapArguments.avatar
    );
    createdWindows.push(avatarWindow);
    input.windowIdentities.bind(avatarWindow.webContents, "avatar");
    const panelWindow = createPanelWindow(
      preloadPaths.panel,
      bootstrapArguments.panel
    );
    createdWindows.push(panelWindow);
    input.windowIdentities.bind(panelWindow.webContents, "panel");
    const chatWindow = createChatWindow(
      preloadPaths.chat,
      bootstrapArguments.chat
    );
    createdWindows.push(chatWindow);
    input.windowIdentities.bind(chatWindow.webContents, "chat");
    const panelWindowMotion = createPanelWindowMotion(panelWindow);
    const chatWindowMotion = createChatWindowMotion();
    const chatWindowDrag = createChatWindowDragController(bounds => {
      chatWindowMotion.rememberBounds(bounds);
    });
    const chatWindowResize = createChatWindowResizeController(
      bounds => chatWindowMotion.rememberBounds(bounds),
      bounds => screen.getDisplayMatching(bounds).workArea
    );
    const chatLifecycle = createChatWindowLifecycleOwner({
      chatWindow,
      chatWindowMotion,
      getWorkArea: () => screen.getPrimaryDisplay().workArea,
      sendAvatarMode: mode => avatarWindow.webContents.send("avatar:chat-mode-changed", { mode }),
      sendChatModeRequest: mode => chatWindow.webContents.send("chat:mode-requested", { mode }),
      getAvatarSummonMode: () => controller.getAvatarSummonMode()
    });

    attachRendererDiagnostics(avatarWindow, WINDOW_KINDS[0]);
    attachRendererDiagnostics(panelWindow, WINDOW_KINDS[1]);
    attachRendererDiagnostics(chatWindow, WINDOW_KINDS[2]);

    const controller = createShellController({
      avatarWindow: {
        show: () => input.avatarInteraction.scheduleFirstShow(avatarWindow),
        applyPresentationMode: mode =>
          applyAvatarWindowPresentationMode(avatarWindow, mode),
        getBounds: () => avatarWindow.getBounds(),
        getPetBounds: () => input.avatarInteraction.getPetBounds(avatarWindow)
      },
      panelWindow: {
        hide: () => panelWindow.hide(),
        openAt: (placement, panelSize) => {
          panelWindowMotion.openAt(placement, panelSize);
        },
        focus: () => panelWindow.focus()
      },
      getDisplayBounds: avatarBounds => screen.getDisplayMatching(avatarBounds).bounds
    });
    const companionSummonController = createCompanionSummonController({
      avatarVisibilityController: controller,
      chatWindow,
      chatLifecycle
    });

    // 先登记首帧监听，再开始加载；同一等待预算覆盖三页加载，任何一页卡住都能就地收口。
    const contentLoaded = Promise.resolve().then(async () => {
      await loadWindowContent(avatarWindow, WINDOW_KINDS[0], input.mainModuleDir);
      await loadWindowContent(panelWindow, WINDOW_KINDS[1], input.mainModuleDir);
      await loadWindowContent(chatWindow, WINDOW_KINDS[2], input.mainModuleDir);
    });
    if (input.waitForFirstPaint) {
      firstPaint = watchWindowFirstPaint(avatarWindow, contentLoaded);
      await firstPaint.ready;
      firstPaint.dispose();
    } else {
      await contentLoaded;
    }

    if (input.isAppQuitting()) throw new Error("桌面窗口装配已被应用退出取消");

    chatLifecycle.initializeHidden();

    /**
     * 角色窗隐藏时同步隐藏面板，避免只隐藏 avatar 后留下孤儿 panel。
     */
    avatarWindow.on("hide", () => {
      panelWindow.hide();
      chatLifecycle.hideChat();
    });

    controller.start();
    input.avatarInteraction.applyPendingPresentationProfile(avatarWindow);

    const stopPanelBlurCloseGuard = createPanelDismissGuard({
      panelWindow,
      screen,
      delayMs: PANEL_BLUR_CLOSE_CONFIRM_DELAY_MS,
      closePanelFromExternalBlur: () => controller.closePanelFromExternalBlur()
    });

    /**
     * 聊天窗关闭按钮只收起聊天窗；应用整体退出时才放行真实关闭。
     */
    chatWindow.on("close", event => {
      if (input.isAppQuitting()) {
        return;
      }

      event.preventDefault();
      chatLifecycle.hideChat();
    });

    // 系统最小化按隐藏处理，但不改变 renderer 生命周期，避免草稿丢失。
    const chatWindowMinimizeEvents = chatWindow as unknown as {
      on(event: "minimize", listener: (event: { preventDefault(): void }) => void): void;
    };
    chatWindowMinimizeEvents.on("minimize", event => {
      event.preventDefault();
      chatLifecycle.hideChat();
    });

    avatarWindow.on("closed", () => {
      input.onRuntimeClosed({ closingWindow: avatarWindow });
    });
    panelWindow.on("closed", () => {
      input.onRuntimeClosed({ closingWindow: panelWindow });
    });
    chatWindow.on("closed", () => {
      input.onRuntimeClosed({ closingWindow: chatWindow });
    });

    return {
      avatarWindow,
      panelWindow,
      chatWindow,
      chatWindowMotion,
      chatWindowDrag,
      chatWindowResize,
      chatLifecycle,
      controller,
      companionSummonController,
      stopPanelBlurCloseGuard
    };
  } catch (error) {
    firstPaint?.dispose();
    for (const window of createdWindows) {
      destroyWindow(window);
    }
    throw error;
  }
}

/**
 * teardownShellRuntime 安全销毁当前整套 shell 壳。
 *
 * Args:
 *   runtime: 当前 shell runtime；为空时只执行外部清理。
 *   options: 正在关闭的窗口、托盘销毁、AEC 停止与 avatar 状态重置命令。
 */
export function teardownShellRuntime(
  runtime: ShellRuntime | null,
  options: ShellRuntimeTeardownOptions
): void {
  options.stopAecSidecar();
  options.resetAvatarInteraction();

  if (!runtime) {
    options.destroyTray();
    return;
  }

  runtime.stopPanelBlurCloseGuard();

  if (runtime.avatarWindow !== options.closingWindow) {
    destroyWindow(runtime.avatarWindow);
  }

  if (runtime.panelWindow !== options.closingWindow) {
    destroyWindow(runtime.panelWindow);
  }

  if (runtime.chatWindow !== options.closingWindow) {
    destroyWindow(runtime.chatWindow);
  }

  options.destroyTray();
}

/**
 * destroyWindow 只在窗口仍然存活时调用 destroy。
 *
 * Args:
 *   window: 待销毁的 BrowserWindow。
 */
function destroyWindow(window: BrowserWindow): void {
  if ("isDestroyed" in window && typeof window.isDestroyed === "function" && window.isDestroyed()) {
    return;
  }

  window.destroy();
}
