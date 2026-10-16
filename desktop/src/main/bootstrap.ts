import { app, clipboard, dialog, globalShortcut, screen, shell, type BrowserWindow, type Tray } from "electron";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { registerAppLifecycleHandlers } from "./appLifecycle";
import { registerShellIpcHandlers } from "./ipc/shellIpcHandlers";
import { createWindowIdentityRegistry } from "./ipc/windowIdentityRegistry";
import { createPlatformBridge } from "./platform";
import { createAecSidecarSupervisor } from "./services/aec/aecSidecarSupervisor";
import { createBackendSupervisor, type BackendSupervisor, type BackendUnexpectedTermination } from "./services/backendSupervisor";
import { createManagedLiveKitSupervisor, type ManagedLiveKitSupervisor } from "./services/managedLiveKitSupervisor";
import { createBootstrapConsoleLauncher } from "./services/bootstrapConsoleLauncher";
import { createPackagedRuntimeEnvironment } from "./services/backendEnvironment";
import {
  resolveNearMateRuntimeLayout,
  type NearMateRuntimeLayout
} from "./services/runtimeLayout";
import { assertPackagedResourcesIntact } from "./services/packagedResourceVerifier";
import { createControlSettingsSyncService } from "./services/controlSettingsSync";
import { createEnvironmentCaptureService } from "./services/contextCapture";
import { createEnvironmentImageEncoder } from "./services/environmentImageEncoder";
import { cleanupLegacyEnvironmentSnapshots } from "./services/environmentLegacyCleanup";
import { createDesktopShortcutRegistryService } from "./services/shortcuts/desktopShortcutRegistry";
import { createEnvironmentSnapshotService } from "./services/environmentSnapshotService";
import { createEnvironmentContextClient } from "./services/environmentContextClient";
import { createFollowupControlClient } from "./services/followupControlClient";
import { createMaterialDeliveryFeedbackService } from "./services/materialDeliveryFeedbackService";
import { createMaterialCommandService } from "./services/materialCommandService";
import { createMaterialsClient } from "./services/materialsClient";
import { createManagedMaterialAssetStore } from "./services/managedMaterialAssetStore";
import { createRuntimeContextEventWatcher } from "./services/runtimeContextEventWatcher";
import { createActiveWindowTitleProvider } from "./services/windowsActiveWindow";
import { createTurnSubmissionService } from "./services/turnSubmissionService";
import { createTurnSubmissionsClient } from "./services/turnSubmissionsClient";
import { createWindowsSelectedContentFileDropPort } from "./services/selectedContentFileDropPort";
import { createWindowsSelectionCapturePort } from "./services/windowsSelectionCapturePort";
import { createShortcutStatusClient } from "./services/shortcuts/shortcutStatusClient";
import {
  createSelectedContentCaptureService
} from "./services/selectedContentCaptureService";
import { createDiskSelectedContentFileStore } from "./services/managedMaterialWriter";
import { createElectronSelectedContentClipboardPort } from "./services/selectionClipboardTransaction";
import {
  createShellRuntime,
  teardownShellRuntime,
  type ShellRuntime
} from "./shellRuntime";
import {
  createStartupProgressWindow,
  type StartupProgressWindow
} from "./windows/startupProgressWindow";
import { createAvatarModelLayoutStore } from "./stores/avatarModelLayoutStore";
import {
  createTrayRuntime,
  destroyTrayRuntime
} from "./trayRuntime";
import {
  createAvatarWindowInteractionController
} from "./windows/avatarWindowInteraction";
import { resolveControlHttpRootUrl } from "../shared/controlHttpUrl";
import { SHORTCUT_FEEDBACK_CHANNEL, type ShortcutFeedback } from "../shared/shortcutFeedback";

/**
 * 使用 ESM 时手动恢复 __dirname，保证 preload 与 renderer 路径在开发和构建后都可定位。
 */
const __dirname = dirname(fileURLToPath(import.meta.url));

/**
 * DesktopServices 是依赖安装路径与控制面地址的服务装配结果。
 * runtimeLayout 在 runtime-manifest.json 损坏时抛错、controlHttpBaseUrl 在环境变量非法时抛错，
 * 因此它们连同下游服务一律在 bootstrap 内惰性装配，绝不在模块顶层求值。
 */
type DesktopServices = {
  runtimeLayout: NearMateRuntimeLayout;
  controlHttpBaseUrl: string;
  aecSidecarSupervisor: ReturnType<typeof createAecSidecarSupervisor>;
  platformBridge: ReturnType<typeof createPlatformBridge>;
  environmentSnapshotService: ReturnType<typeof createEnvironmentSnapshotService>;
  materialCommandService: ReturnType<typeof createMaterialCommandService>;
  turnSubmissionService: ReturnType<typeof createTurnSubmissionService>;
  runtimeContextEventWatcher: ReturnType<typeof createRuntimeContextEventWatcher>;
  controlSettingsSyncService: ReturnType<typeof createControlSettingsSyncService>;
  desktopShortcutRegistryService: ReturnType<typeof createDesktopShortcutRegistryService>;
};

/**
 * desktopServices 是 bootstrap 装配完成后的服务快照。
 * 生命周期回调与窗口重建只读这份快照；装配失败时保持 null，退出清理按空跳过。
 */
let desktopServices: DesktopServices | null = null;

/**
 * packaged 模式下由 Desktop 持有 Backend；开发态仍由根编排器启动 Backend。
 */
let backendSupervisor: BackendSupervisor | null = null;
let liveKitSupervisor: ManagedLiveKitSupervisor | null = null;
// 壳层装配期间的后台退出先暂存，启动失败统一收口后不再重复弹出运行期提示。
let hasFinishedBootstrap = false;
let pendingRuntimeTermination: { termination: BackendUnexpectedTermination; service: "backend" | "livekit" } | null = null;
let isRuntimeFailureNoticeVisible = false;

/**
 * windowIdentities 在窗口创建时记录可信 WebContents 身份，供所有 renderer 上行 IPC 授权。
 */
const windowIdentities = createWindowIdentityRegistry();

/**
 * runtime 是当前主进程唯一持有的窗口壳引用。
 * Electron 窗口需要强引用，否则会被回收导致行为异常。
 */
let runtime: ShellRuntime | null = null;

/**
 * startupProgressWindow 只在安装版启动前置阶段存在。
 * 它不属于三窗口 shell runtime，避免启动慢时用户面对空白桌面；启动完成或失败收口时都会主动关闭。
 */
let startupProgressWindow: StartupProgressWindow | null = null;

/**
 * environmentCaptureService 只负责执行一次真实系统现场采集。
 * 调度、缓存与防抖由 environmentSnapshotService 承担，避免交互链路同步等待重采集。
 */
const environmentImageEncoder = createEnvironmentImageEncoder();
const environmentCaptureService = createEnvironmentCaptureService({
  activeWindow: createActiveWindowTitleProvider(),
  imageEncoder: environmentImageEncoder
});

/**
 * 受管资产仓储只拥有项目生成的材料副本，专用目录不会放入用户原文件。
 */
const managedMaterialAssetStore = createManagedMaterialAssetStore({
  rootDir: join(app.getPath("userData"), "runtime-material-assets")
});

/**
 * materialDeliveryFeedbackService 是材料成功入池后的唯一桌面反馈出口。
 * 它只负责通知头像窗口播放本地反馈，不参与材料写入、读取或路由判断。
 */
const materialDeliveryFeedbackService = createMaterialDeliveryFeedbackService({
  getAvatarWindow: () => runtime?.avatarWindow ?? null,
  getChatWindow: () => runtime?.chatWindow ?? null
});

/**
 * selectedContentCaptureService 只负责把当前选区整理为 staged 材料项。
 */
const selectedContentCaptureService = createSelectedContentCaptureService({
  platform: process.platform,
  attachmentDir: managedMaterialAssetStore.rootDir,
  managedAssetStore: managedMaterialAssetStore,
  clipboard: createElectronSelectedContentClipboardPort(clipboard, createWindowsSelectedContentFileDropPort()),
  selectionCapture: createWindowsSelectionCapturePort(),
  fileStore: createDiskSelectedContentFileStore(),
  logger: console
});

/**
 * avatarInteraction 持有角色窗拖拽、缩放、可见区、首显档案与按人物布局记忆状态。
 * bootstrap 只把真实窗口交给它，不直接操作这些细节状态。
 */
const avatarModelLayoutStore = createAvatarModelLayoutStore(
  join(app.getPath("userData"), "avatar-model-layouts.json")
);
const avatarInteraction = createAvatarWindowInteractionController(
  screen,
  avatarModelLayoutStore
);

/**
 * tray 作为系统级常驻入口，必须由 bootstrap 显式持有并在重建前主动销毁。
 */
let tray: Tray | null = null;

const appLifecycle = registerAppLifecycleHandlers({
  getRuntime: () => runtime,
  recreateRuntime,
  teardownBeforeQuit
});

/**
 * bootstrap 负责接通 Electron 生命周期、长期服务、IPC 与 shell runtime。
 * 它不承载 IPC 细节、窗口几何状态或资源路径扫描算法。
 * 启动期任何一步失败都会走统一收口：停 Backend、销毁已建窗口、弹可操作错误并退出，
 * 绝不留下无窗口无托盘的幽灵进程。
 */
export async function bootstrap(): Promise<void> {
  await app.whenReady();
  // 第二次点击安装目录中的程序时，复用已经运行的实例，避免两个 Backend 抢同一个控制端口。
  if (typeof app.requestSingleInstanceLock === "function" && !app.requestSingleInstanceLock()) {
    app.quit();
    return;
  }
  app.on("second-instance", () => {
    // 启动交接前仍由进度窗承载反馈，重复启动不能绕过首次显示门闩。
    if (!hasFinishedBootstrap || appLifecycle.isAppQuitting()) return;
    const avatarWindow = runtime?.avatarWindow;
    if (!avatarWindow || avatarWindow.isDestroyed()) return;
    avatarWindow.showInactive();
    avatarWindow.moveTop();
  });
  try {
    if (app.isPackaged) {
      avatarInteraction.holdFirstShow();
      startupProgressWindow = createStartupProgressWindow();
      updateStartupProgress(4, "正在启动 NearMate", "正在检查安装资源和本机运行环境。", 12);
    }
    if (app.isPackaged) {
      // 创建任何窗口之前先验证随包资源，缺失时直接给用户重装指引，而不是启动到一半才崩。
      assertPackagedResourcesIntact({
        resourcesPath: process.resourcesPath,
        mainModuleDir: __dirname
      });
      updateStartupProgress(12, "安装资源检查完成", "正在装配 NearMate 的本地服务。", 25);
    }
    const services = ensureDesktopServices();
    updateStartupProgress(20, "正在准备桌面服务", "正在读取控制面和本机语音配置。", 25);
    if (app.isPackaged) {
      // 普通安装不再要求用户部署媒体服务器；完整外部配置仍保留其独立生命周期。
      updateStartupProgress(25, "正在启动本地媒体服务", "正在准备本机语音连接。", 40);
      liveKitSupervisor = createManagedLiveKitSupervisor({
        executablePath: services.runtimeLayout.liveKitServerExe,
        userDataDir: app.getPath("userData"),
        onUnexpectedTermination: termination => notifyRuntimeServiceTermination(termination, "livekit")
      });
      const liveKitConnection = await liveKitSupervisor.start();
      if (appLifecycle.isAppQuitting()) return;
      updateStartupProgress(40, "正在启动 NearMate 后台服务", "本地媒体服务已就绪，正在等待后台初始化完成。", 85);
      // 统一向 Backend、AEC sidecar 和其它受管子进程注入本次安装的资源边界。
      backendSupervisor = createBackendSupervisor({
        pythonExe: services.runtimeLayout.pythonExe,
        cwd: services.runtimeLayout.backendCwd,
        runtimeLayout: services.runtimeLayout,
        userDataDir: app.getPath("userData"),
        controlUrl: services.controlHttpBaseUrl,
        liveKitConnection,
        onUnexpectedTermination: notifyRuntimeServiceTermination
      });
      await backendSupervisor.start();
      if (appLifecycle.isAppQuitting()) return;
    }
    updateStartupProgress(85, "正在准备运行环境", "正在整理本次启动需要的本地材料。", 90);
    await managedMaterialAssetStore.prepareRuntime();
    void cleanupLegacyEnvironmentSnapshots(app.getPath("userData")).catch(error => {
      console.warn("[companion-main] legacy environment snapshot cleanup failed", error);
    });

    /**
     * Windows 通知依赖 App User Model ID，提前设置可以保持系统集成稳定。
     */
    if (process.platform === "win32") {
      app.setAppUserModelId("desktop.virtual.companion");
    }

    registerShellIpcHandlers({
      windowIdentities,
      getRuntime: () => runtime,
      avatarInteraction,
      aecSidecarSupervisor: services.aecSidecarSupervisor,
      turnSubmission: services.turnSubmissionService,
      environmentSnapshot: services.environmentSnapshotService,
      materialCommands: services.materialCommandService,
      materialFeedback: materialDeliveryFeedbackService,
      managedMaterialAssetStore
    });
    updateStartupProgress(90, "正在读取桌面设置", "正在同步设置和运行期状态。", 94);
    await services.controlSettingsSyncService.start();
    updateStartupProgress(94, "正在注册快捷键", "正在接通桌面入口。", 97);
    await services.desktopShortcutRegistryService.start();
    updateStartupProgress(97, "正在准备桌面窗口", "正在后台加载 NearMate 的桌面入口。", 99);
    await services.runtimeContextEventWatcher.start();

    if (appLifecycle.isAppQuitting()) return;
    if (!runtime) {
      runtime = await createRuntime();
    }
    // 健康检查成功后服务仍可能在窗口装配期间退出，已知的退出不能被成功动画掩盖。
    if (pendingRuntimeTermination) throw new Error("NearMate 本地服务在启动交接期间停止，请重新打开 NearMate。");
    await startupProgressWindow?.complete();
    if (appLifecycle.isAppQuitting()) return;
    if (pendingRuntimeTermination) throw new Error("NearMate 本地服务在启动交接期间停止，请重新打开 NearMate。");
    closeStartupProgressWindow();
    avatarInteraction.releaseFirstShow();
    hasFinishedBootstrap = true;
    // 首次配置需要用户操作，100% 只表示桌面入口装配完成，不能在启动窗里等待配置门禁。
    if (app.isPackaged) {
      await createBootstrapConsoleLauncher({
        controlUrl: services.controlHttpBaseUrl,
        consoleSection: "settings",
        openExternal: shell.openExternal,
        isAppQuitting: appLifecycle.isAppQuitting,
        onFailure: error => console.warn("[companion-main] bootstrap console open skipped", error)
      })();
    }
  } catch (error) {
    if (appLifecycle.isAppQuitting()) return;
    await abortStartup(error);
  }
}

/**
 * 后台意外结束时告诉用户如何恢复，并提供本地日志入口。
 *
 * Args:
 *   termination: 监督器提供的退出摘要，不包含原始输出或凭据。
 *   service: 区分后台与本地媒体服务，以便展示准确恢复说明。
 * Returns:
 *   提示处理完成；不会自动重启后台或重放用户任务。
 */
async function notifyRuntimeServiceTermination(termination: BackendUnexpectedTermination, service: "backend" | "livekit" = "backend"): Promise<void> {
  if (appLifecycle.isAppQuitting()) return;
  if (!hasFinishedBootstrap) {
    pendingRuntimeTermination = { termination, service };
    return;
  }
  if (isRuntimeFailureNoticeVisible) return;
  isRuntimeFailureNoticeVisible = true;
  const exitCode = termination.code === null
    ? "未提供退出码"
    : `${termination.code}（0x${(termination.code >>> 0).toString(16).toUpperCase().padStart(8, "0")}）`;
  try {
    const result = await dialog.showMessageBox({
      type: "error",
      title: service === "livekit" ? "NearMate 语音服务已停止" : "NearMate 后台服务已停止",
      message: service === "livekit" ? "本地语音服务意外停止" : "NearMate 后台服务意外停止",
      detail: `聊天和初始化暂时无法继续。请退出并重新打开 NearMate；如果再次出现，请查看诊断日志。\n\n退出码：${exitCode}`,
      buttons: ["查看诊断日志", "退出 NearMate", "稍后处理"],
      defaultId: 0,
      cancelId: 2,
      noLink: true
    });
    if (appLifecycle.isAppQuitting()) return;
    if (result.response === 0) shell.showItemInFolder(termination.logPath);
    if (result.response === 1) app.quit();
  } catch {
    // 原生对话框不可用时保留监督器已写入的日志，不能再制造未处理异常。
    console.error("[companion-main] 无法显示运行组件退出提示");
  } finally {
    isRuntimeFailureNoticeVisible = false;
  }
}

/**
 * ensureDesktopServices 惰性装配依赖安装路径与控制面地址的全部服务。
 * 装配只在首次 bootstrap 时执行一次；路径或地址非法抛出的错误由 bootstrap 统一收口。
 *
 * Returns:
 *   装配完成的服务快照；重复调用返回同一份缓存。
 */
function ensureDesktopServices(): DesktopServices {
  if (desktopServices) {
    return desktopServices;
  }

  /**
   * repoRoot 是当前开发态仓库根目录。
   * sidecar supervisor 依赖仓库根目录的统一 Python 虚拟环境和 AEC 源码。
   */
  const runtimeLayout = resolveNearMateRuntimeLayout({
    isPackaged: app.isPackaged,
    resourcesPath: process.resourcesPath,
    repoRoot: resolveRepoRoot(),
    profile: app.isPackaged ? null : process.env["NEARMATE_RUNTIME_PROFILE"]
  });

  /**
   * controlHttpBaseUrl 是桌面主进程访问控制面的统一 HTTP 根地址。
   */
  const controlHttpBaseUrl = resolveControlHttpRootUrl(
    process.env["COMPANION_CONTROL_HTTP_URL"]
  );

  /**
   * aecSidecarSupervisor 是 main 进程唯一持有的 AEC sidecar 生命周期管理器。
   */
  const aecSidecarSupervisor = createAecSidecarSupervisor({
    pythonExe: runtimeLayout.pythonExe,
    repoRoot: runtimeLayout.runtimeRoot,
    packaged: app.isPackaged,
    environment: app.isPackaged
      ? createPackagedRuntimeEnvironment(runtimeLayout, app.getPath("userData"))
      : undefined,
    parentPid: process.pid
  });

  /**
   * 平台桥在整个应用生命周期内保持稳定，提前初始化可以避免重复创建系统资源。
   */
  const platformBridge = createPlatformBridge({
    appResourceRoot: app.isPackaged ? runtimeLayout.assetsRoot : resolveRepoRoot("assets")
  });

  const environmentContextClient = createEnvironmentContextClient({
    httpBaseUrl: controlHttpBaseUrl
  });

  /**
   * environmentSnapshotService 是桌面主进程的现场环境缓存。
   * 聊天输入和语音事件只读取或触发它，不直接启动截图、剪贴板、活动窗口等重采集。
   */
  const environmentSnapshotService = createEnvironmentSnapshotService({
    capture: environmentCaptureService,
    client: environmentContextClient
  });

  const materialsClient = createMaterialsClient({
    httpBaseUrl: controlHttpBaseUrl
  });

  const materialCommandService = createMaterialCommandService({
    materialsClient,
    managedAssetStore: managedMaterialAssetStore,
    feedback: materialDeliveryFeedbackService
  });

  const turnSubmissionService = createTurnSubmissionService({
    environmentSnapshot: environmentSnapshotService,
    turnSubmissionsClient: createTurnSubmissionsClient({
      httpBaseUrl: controlHttpBaseUrl
    })
  });

  const followupControlClient = createFollowupControlClient({
    httpBaseUrl: controlHttpBaseUrl
  });

  /**
   * runtimeContextEventWatcher 处理环境刷新请求和项目副本释放事件。
   */
  const runtimeContextEventWatcher = createRuntimeContextEventWatcher({
    httpBaseUrl: controlHttpBaseUrl,
    environmentSnapshot: environmentSnapshotService,
    managedAssetStore: managedMaterialAssetStore,
    materialFeedback: materialDeliveryFeedbackService
  });

  /**
   * controlSettingsSyncService 以后端设置快照作为桌面主进程运行期设置真相源。
   */
  const controlSettingsSyncService = createControlSettingsSyncService({
    httpBaseUrl: controlHttpBaseUrl
  });

  /**
   * desktopShortcutRegistryService 统一注册所有桌面全局快捷键，并把注册状态回传给控制台。
   */
  let shortcutSequence = 0;
  const sendShortcutFeedback = (feedback: ShortcutFeedback): void => {
    const avatarWindow = runtime?.avatarWindow;
    if (!avatarWindow || avatarWindow.isDestroyed()) return;
    avatarWindow.webContents.send(SHORTCUT_FEEDBACK_CHANNEL, feedback);
  };
  const desktopShortcutRegistryService = createDesktopShortcutRegistryService({
    globalShortcut,
    settingsSync: controlSettingsSyncService,
    statusClient: createShortcutStatusClient({
      httpBaseUrl: controlHttpBaseUrl
    }),
    onShortcutPressed(shortcutId) {
      shortcutSequence += 1;
      sendShortcutFeedback({ phase: "pressed", shortcutId, sequence: shortcutSequence });
    },
    actions: {
      async toggleFollowup() {
        const sequence = shortcutSequence;
        try {
          const result = await followupControlClient.toggleFollowup();
          sendShortcutFeedback({ phase: "settled", shortcutId: "followup_toggle", sequence,
            interruptedRequestIds: result.interruptedRequestIds,
            ...(result.speechInterruptedAt === undefined ? {} : { speechInterruptedAt: result.speechInterruptedAt }) });
        } catch (error) {
          sendShortcutFeedback({ phase: "settled", shortcutId: "followup_toggle", sequence,
            interruptedRequestIds: null });
          throw error;
        }
      },
      toggleAvatarVisibility() {
        // 召回会直接显示窗口；启动交接和退出收口期间不能绕过首显门闩。
        if (!hasFinishedBootstrap || appLifecycle.isAppQuitting()) return;
        runtime?.companionSummonController.toggleCompanionEntryVisibility();
      },
      async captureSelection() {
        materialDeliveryFeedbackService.notifyMaterialProgress("正在读取所选内容…");
        try {
          const items = await selectedContentCaptureService.captureSelectedContent();
          if (items.length === 0) {
            materialDeliveryFeedbackService.notifyMaterialWarning("未检测到可添加的选中内容");
            return;
          }
          await materialCommandService.add(items);
        } catch (error) {
          materialDeliveryFeedbackService.notifyMaterialWarning("添加选中内容失败，请重试");
          throw error;
        }
      }
    }
  });

  desktopServices = {
    runtimeLayout,
    controlHttpBaseUrl,
    aecSidecarSupervisor,
    platformBridge,
    environmentSnapshotService,
    materialCommandService,
    turnSubmissionService,
    runtimeContextEventWatcher,
    controlSettingsSyncService,
    desktopShortcutRegistryService
  };
  return desktopServices;
}

/**
 * requireDesktopServices 读取已完成装配的服务快照。
 *
 * Returns:
 *   装配完成的服务；bootstrap 尚未装配时抛错（正常流程不会到达）。
 */
function requireDesktopServices(): DesktopServices {
  if (!desktopServices) {
    throw new Error("桌面服务尚未完成装配");
  }
  return desktopServices;
}

/**
 * createRuntime 创建窗口壳并同步刷新托盘入口。
 * 中途失败时销毁已建窗口与相关状态，避免留下无收口的孤儿窗口。
 */
async function createRuntime(): Promise<ShellRuntime> {
  const services = requireDesktopServices();
  let nextRuntime: ShellRuntime | null = null;
  try {
    nextRuntime = await createShellRuntime({
      mainModuleDir: __dirname,
      windowIdentities,
      avatarInteraction,
      isAppQuitting: appLifecycle.isAppQuitting,
      onRuntimeClosed: input => {
        teardownRuntime(input);
      },
      waitForFirstPaint: app.isPackaged
    });

    recreateTray(nextRuntime);
    return nextRuntime;
  } catch (error) {
    teardownShellRuntime(nextRuntime, {
      destroyTray,
      stopAecSidecar: () => stopAecSidecarBestEffort(services.aecSidecarSupervisor),
      resetAvatarInteraction: () => avatarInteraction.reset()
    });
    throw error;
  }
}

/**
 * recreateRuntime 用于在全部窗口关闭后重新建立三窗口壳。
 */
async function recreateRuntime(): Promise<void> {
  teardownRuntime();
  runtime = await createRuntime();
}

/**
 * 窗口关闭回调本身是同步接口，优先启动可等待的 sidecar 收口，并把失败交给退出日志。
 * 真正的 app before-quit 链路会再次 await 同一个 stopAndWait Promise。
 *
 * Args:
 *   supervisor: 当前桌面服务持有的 AEC supervisor；服务尚未装配时可以为空。
 */
function stopAecSidecarBestEffort(
  supervisor: DesktopServices["aecSidecarSupervisor"] | undefined
): void {
  if (!supervisor) return;

  try {
    if (supervisor.stopAndWait) {
      void supervisor.stopAndWait().catch(error => {
        console.error("[companion-main] 窗口关闭时停止 AEC sidecar 失败", error);
      });
      return;
    }
    supervisor.stop();
  } catch (error) {
    console.error("[companion-main] 窗口关闭时请求停止 AEC sidecar 失败", error);
  }
}

/**
 * teardownRuntime 负责安全销毁当前 shell 壳，并收口 runtime 与 tray 引用。
 */
function teardownRuntime(input?: { closingWindow?: BrowserWindow }): void {
  const currentRuntime = runtime;
  runtime = null;
  teardownShellRuntime(currentRuntime, {
    closingWindow: input?.closingWindow,
    destroyTray,
    stopAecSidecar: () => stopAecSidecarBestEffort(desktopServices?.aecSidecarSupervisor),
    resetAvatarInteraction: () => avatarInteraction.reset()
  });
}

/**
 * teardownBeforeQuit 负责应用退出前的长期服务清理。
 * 先停 AEC sidecar 与 Backend，再停自有媒体服务，最后做文件清理：子进程可能正握着文件句柄，
 * 先清文件既容易撞上占用错误，也会把进程残留下来。
 * 每一步独立兜底，单个失败记日志后必须继续后续步骤。
 *
 * Returns:
 *   全部步骤完成或五秒总时限耗尽后结束。
 */
async function teardownBeforeQuit(): Promise<void> {
  avatarInteraction.holdFirstShow();
  avatarInteraction.reset();
  closeStartupProgressWindow();
  const deadline = Date.now() + 5_000;
  await runTeardownStep("停止 AEC sidecar", () => {
    const supervisor = desktopServices?.aecSidecarSupervisor;
    if (!supervisor) return;
    if (supervisor.stopAndWait) {
      return supervisor.stopAndWait();
    }
    supervisor.stop();
  }, deadline, () => {
    desktopServices?.aecSidecarSupervisor.forceStop();
  });
  const supervisorToStop = backendSupervisor;
  backendSupervisor = null;
  await runTeardownStep("停止 Backend", () => {
    return supervisorToStop?.stop();
  }, deadline, () => {
    supervisorToStop?.forceStop();
  });
  const mediaToStop = liveKitSupervisor;
  liveKitSupervisor = null;
  await runTeardownStep("停止本地语音服务", () => mediaToStop?.stop(), deadline, () => mediaToStop?.forceStop());
  await runTeardownStep("停止快捷键注册", () => {
    desktopServices?.desktopShortcutRegistryService.stop();
  }, deadline);
  await runTeardownStep("停止设置同步", () => {
    desktopServices?.controlSettingsSyncService.stop();
  }, deadline);
  await runTeardownStep("停止环境快照", () => {
    desktopServices?.environmentSnapshotService.stop();
  }, deadline);
  await runTeardownStep("停止图片编码器", () => {
    environmentImageEncoder.stop();
  }, deadline);
  await runTeardownStep("停止环境事件监听", () => {
    desktopServices?.runtimeContextEventWatcher.stop();
  }, deadline);
  await runTeardownStep("清理受管材料资产", () => {
    managedMaterialAssetStore.releaseAllSync();
  }, deadline);
}

/**
 * runTeardownStep 执行单步退出清理并把失败降级为日志。
 *
 * Args:
 *   label: 该步骤的中文名称，用于日志定位。
 *   step: 具体的清理动作。
 *   deadline: 整体退出清理的绝对截止时间。
 *
 * Returns:
 *   当前步骤完成、失败或用尽剩余时间后结束。
 */
async function runTeardownStep(
  label: string,
  step: () => void | Promise<void>,
  deadline: number,
  forceStop?: () => void | Promise<void>,
): Promise<void> {
  const remainingMs = deadline - Date.now();
  if (remainingMs <= 0) {
    console.error(`[companion-main] 退出清理超时：${label}`);
    await runTeardownRecovery(label, forceStop);
    return;
  }

  let timeoutHandle: ReturnType<typeof setTimeout> | undefined;
  let recoveryStarted = false;
  const recover = (): void => {
    if (recoveryStarted || !forceStop) return;
    recoveryStarted = true;
    void runTeardownRecovery(label, forceStop);
  };
  const operation = Promise.resolve().then(step);
  await new Promise<void>(resolve => {
    let settled = false;
    const finish = (): void => {
      if (settled) return;
      settled = true;
      if (timeoutHandle !== undefined) clearTimeout(timeoutHandle);
      resolve();
    };

    timeoutHandle = setTimeout(() => {
      console.error(`[companion-main] 退出清理超时：${label}`);
      recover();
      finish();
    }, remainingMs);
    operation.then(
      finish,
      error => {
        console.error(`[companion-main] 退出清理失败：${label}`, error);
        recover();
        finish();
      }
    );
  });
}

/**
 * 退出清理的强制收口只允许发出终止请求，不能再次把主进程卡在一个没有上限的 Promise 上。
 *
 * Args:
 *   label: 失败步骤名称，用于定位强制收口日志。
 *   forceStop: 对应 supervisor 的幂等强制终止动作。
 *
 * Returns:
 *   强制请求完成；异步请求失败只记日志，不覆盖原始退出路径。
 */
async function runTeardownRecovery(
  label: string,
  forceStop: (() => void | Promise<void>) | undefined
): Promise<void> {
  if (!forceStop) return;
  try {
    await forceStop();
  } catch (error) {
    console.error(`[companion-main] 强制收口失败：${label}`, error);
  }
}

/**
 * abortStartup 是启动失败的统一收口：停 Backend、销毁已建窗口、告知用户并退出。
 *
 * Args:
 *   error: 启动链路中任意一步抛出的原始错误。
 *
 * Returns:
 *   永不返回；收口完成后重新抛出原错误，供入口垫片记录。
 */
async function abortStartup(error: unknown): Promise<never> {
  avatarInteraction.holdFirstShow();
  const message = error instanceof Error ? error.message : String(error);
  const isStartupTimeout = error instanceof Error && "failureCode" in error && error.failureCode === "startup_timeout";
  // 超时有明确的模型加载阶段说明，不能再附加“重新安装”让用户误以为安装资源已损坏。
  const timeoutMessage = isStartupTimeout && error.cause instanceof Error
    ? error.cause.message
    : "本地模型未能在限定时间内完成加载。请重新打开 NearMate；若反复出现，请查看诊断日志。";
  console.error("[companion-main] NearMate 启动失败", error);
  closeStartupProgressWindow();
  try {
    teardownRuntime();
  } catch (teardownError) {
    console.error("[companion-main] 启动失败后销毁窗口出错", teardownError);
  }
  // 启动失败不能调用 app.exit 后把 before-quit 清理寄托给 Electron；app.exit
  // 不保证触发异步生命周期钩子，所以这里直接复用完整的有界收口流程。
  await teardownBeforeQuit();
  try {
    const result = await dialog.showMessageBox({
      type: "error",
      title: isStartupTimeout ? "NearMate 启动超时" : "NearMate 启动失败",
      message: isStartupTimeout ? "本地模型未能及时完成启动" : "NearMate 启动失败",
      detail: isStartupTimeout ? timeoutMessage : `${message}\n\n如反复出现，请关闭所有 NearMate 进程后重试，或重新安装 NearMate。`,
      buttons: isStartupTimeout ? ["查看诊断日志", "退出"] : ["退出"],
      cancelId: isStartupTimeout ? 1 : 0,
      noLink: true
    });
    if (isStartupTimeout && result.response === 0) {
      shell.showItemInFolder(join(app.getPath("userData"), "logs", "backend-startup.log"));
    }
  } catch {
    // 测试环境和极早期 Electron 生命周期可能没有可用的原生对话框。
  }
  app.exit(1);
  throw error;
}

/**
 * updateStartupProgress 把主进程当前已知的启动阶段投影到即时反馈窗口。
 * 真实完成节点推进阶段，等待动画只在该阶段边界内增长。
 *
 * @param progress 当前启动阶段的近似完成百分比。
 * @param title 面向用户的当前阶段标题。
 * @param detail 当前阶段的简短说明。
 * @param progressCeiling 下一阶段的真实边界，动画不能提前跨过。
 */
function updateStartupProgress(progress: number, title: string, detail: string, progressCeiling: number): void {
  startupProgressWindow?.update({ progress, progressCeiling, title, detail });
}

/**
 * closeStartupProgressWindow 关闭并释放启动反馈窗口。
 * 它是幂等的，启动成功、失败和用户退出都可以调用同一条收口路径。
 */
function closeStartupProgressWindow(): void {
  const currentWindow = startupProgressWindow;
  startupProgressWindow = null;
  currentWindow?.close();
}

/**
 * recreateTray 为当前 shell runtime 创建新的系统托盘入口。
 */
function recreateTray(currentRuntime: ShellRuntime): void {
  destroyTray();
  tray = createTrayRuntime({
    platformBridge: requireDesktopServices().platformBridge,
    controlHttpUrl: process.env["COMPANION_CONTROL_HTTP_URL"],
    toggleAvatarVisibility: () => {
      // 托盘与快捷键采用同一交接边界，避免提前显示或在退出中重新召回。
      if (!hasFinishedBootstrap || appLifecycle.isAppQuitting()) return;
      currentRuntime.companionSummonController.toggleCompanionEntryVisibility();
    }
  });
}

/**
 * destroyTray 销毁并清空当前托盘引用。
 */
function destroyTray(): void {
  destroyTrayRuntime(tray);
  tray = null;
}

/**
 * resolveRepoRoot 解析当前桌面 main 模块所在仓库根目录。
 */
function resolveRepoRoot(...segments: string[]): string {
  return resolve(__dirname, "../../..", ...segments);
}
