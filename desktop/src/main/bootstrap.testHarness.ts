import { createHash } from "node:crypto";
import { beforeEach, vi } from "vitest";
import type { AvatarSummonMode } from "./shell/shellTypes";

export class FakeElectronWindow {
  /**
   * listeners 保存窗口事件订阅表，作用域仅限单个假窗口实例。
   * 这里不引入通用 EventEmitter，目的是让测试只暴露当前场景真正需要的最小能力。
   */
  private listeners = new Map<string, Array<(...args: unknown[]) => void>>();

  /**
   * destroyed 标记窗口是否已经被销毁，用来模拟 Electron 避免重复 destroy 的约束。
   */
  private destroyed = false;

  /**
   * visible 标记当前窗口显隐状态，供平台桥 mock 在显隐切换时读取。
   */
  private visible = false;

  /**
   * focused 标记当前窗口是否处于聚焦状态，供 blur/focus 收口测试读取。
   */
  private focused = false;

  /**
   * bounds 保存当前假窗口的几何状态。
   * 这里让 setBounds 与 getBounds 共享同一份状态，是为了让缩放与展示档案测试能够像真实窗口一样连续演进。
   */
  private bounds: { x: number; y: number; width: number; height: number };

  /**
   * pendingLoadFilePromise 用于按测试需要延迟页面加载完成。
   * 这样可以稳定模拟 bootstrap 尚未结束时 renderer 已经开始发 IPC 的启动竞态。
   */
  private pendingLoadFilePromise: Promise<void> | null;

  constructor(
    bounds = { x: 120, y: 140, width: 420, height: 520 },
    input?: { loadFilePromise?: Promise<void>; autoReadyToShow?: boolean }
  ) {
    this.bounds = { ...bounds };
    this.pendingLoadFilePromise = input?.loadFilePromise ?? null;
    this.autoReadyToShow = input?.autoReadyToShow ?? true;
  }

  /**
   * hide 与 destroy 都是关键行为观测点，因此暴露为 spy 方便断言调用次数。
   */
  readonly hide = vi.fn(() => {
    this.visible = false;
    this.emit("hide");
  });

  readonly show = vi.fn(() => {
    this.visible = true;
  });

  readonly showInactive = vi.fn(() => {
    this.visible = true;
  });

  readonly webContents = {
    on: vi.fn(),
    once: vi.fn(),
    removeListener: vi.fn(),
    send: vi.fn(),
    setWindowOpenHandler: vi.fn(),
    isDestroyed: () => this.destroyed
  };

  readonly focus = vi.fn();
  readonly moveTop = vi.fn();
  readonly loadURL = vi.fn(async () => undefined);
  readonly loadFile = vi.fn(
    async () => {
      if (this.pendingLoadFilePromise) await this.pendingLoadFilePromise;
      if (this.autoReadyToShow) this.emit("ready-to-show");
    }
  );
  private readonly autoReadyToShow: boolean;
  readonly setBounds = vi.fn((nextBounds: {
    x: number;
    y: number;
    width: number;
    height: number;
  }) => {
    this.bounds = { ...nextBounds };
  });
  readonly setIgnoreMouseEvents = vi.fn();
  readonly setAlwaysOnTop = vi.fn();
  readonly getBounds = vi.fn(() => ({ ...this.bounds }));

  readonly destroy = vi.fn(() => {
    if (this.destroyed) {
      return;
    }

    this.destroyed = true;
    this.visible = false;
    this.focused = false;
    FakeBrowserWindowRegistry.unregister(this);
    this.emit("closed");
  });

  /**
   * requestFocus 模拟真实窗口获得焦点时的状态变化。
   * 这里会同步更新 focused 标记并派发 focus 事件，方便 bootstrap 的失焦防抖逻辑被测试驱动。
   */
  requestFocus(): void {
    this.focused = true;
    this.emit("focus");
  }

  /**
   * requestBlur 模拟真实窗口丢失焦点时的状态变化。
   * 业务上 panelWindow 的 blur 收口正是这条链路的核心，因此测试需要一个明确的触发入口。
   */
  requestBlur(): void {
    this.focused = false;
    this.emit("blur");
  }

  /**
   * requestClose 模拟用户点击系统标题栏关闭按钮的真实流程。
   * 它先派发可被 preventDefault 的 close 事件，只有在没有监听器阻止时才继续销毁窗口，
   * 这样测试才能区分“关闭按钮只隐藏窗口”与“窗口被真正销毁”两种行为。
   */
  requestClose(): void {
    let defaultPrevented = false;
    const closeEvent = {
      preventDefault: () => {
        defaultPrevented = true;
      }
    };

    this.emit("close", closeEvent);

    if (!defaultPrevented) {
      this.destroy();
    }
  }

  on(eventName: string, listener: (...args: unknown[]) => void): this {
    const listeners = this.listeners.get(eventName) ?? [];
    listeners.push(listener);
    this.listeners.set(eventName, listeners);
    return this;
  }

  /**
   * 登记一次性窗口事件，让启动测试能区分页面加载与首帧完成。
   *
   * Args:
   *   eventName: Electron 事件名称。
   *   listener: 仅执行一次的监听函数。
   * Returns:
   *   当前假窗口，保持 Electron 链式接口。
   */
  once(eventName: string, listener: (...args: unknown[]) => void): this {
    const onceListener = (...args: unknown[]): void => {
      this.removeListener(eventName, onceListener);
      listener(...args);
    };
    return this.on(eventName, onceListener);
  }

  /**
   * 移除指定监听，避免首帧测试在收口后接收旧事件。
   *
   * Args:
   *   eventName: Electron 事件名称。
   *   listener: 要移除的监听函数。
   * Returns:
   *   当前假窗口。
   */
  removeListener(eventName: string, listener: (...args: unknown[]) => void): this {
    this.listeners.set(eventName, (this.listeners.get(eventName) ?? []).filter(candidate => candidate !== listener));
    return this;
  }

  emit(eventName: string, ...args: unknown[]): void {
    const listeners = this.listeners.get(eventName) ?? [];

    for (const listener of listeners) {
      listener(...args);
    }
  }

  isVisible(): boolean {
    return this.visible;
  }

  isFocused(): boolean {
    return this.focused;
  }

  isDestroyed(): boolean {
    return this.destroyed;
  }
}

/**
 * FakeBrowserWindowRegistry 模拟 Electron 的全局窗口列表。
 * bootstrap 的 activate 逻辑会读这个列表，因此测试需要一份可控的真相源。
 */
export const FakeBrowserWindowRegistry = {
  windows: [] as FakeElectronWindow[],

  register(window: FakeElectronWindow) {
    this.windows.push(window);
  },

  unregister(window: FakeElectronWindow) {
    this.windows = this.windows.filter(candidate => candidate !== window);
  },

  reset() {
    this.windows = [];
  },

  getAllWindows() {
    return [...this.windows];
  }
};

/**
 * FAKE_PRIMARY_DISPLAY 是 bootstrap 测试统一使用的主显示器。
 * 新聊天浮层默认锚定主屏左下角，因此所有 Electron screen mock 都需要提供这份 workArea。
 */
export const FAKE_PRIMARY_DISPLAY = {
  bounds: { x: 0, y: 0, width: 1440, height: 900 },
  workArea: { x: 0, y: 0, width: 1440, height: 860 }
};

/**
 * createAppMock 提供 bootstrap 依赖的最小 app 行为。
 * 这里同时支持事件监听与手动触发，便于测试 activate 和 window-all-closed 生命周期。
 */
export function createAppMock() {
  const listeners = new Map<string, Array<(...args: unknown[]) => void>>();

  return {
    isPackaged: false,
    whenReady: vi.fn(async () => undefined),
    on: vi.fn((eventName: string, listener: (...args: unknown[]) => void) => {
      const eventListeners = listeners.get(eventName) ?? [];
      eventListeners.push(listener);
      listeners.set(eventName, eventListeners);
    }),
    emit(eventName: string, ...args: unknown[]) {
      const eventListeners = listeners.get(eventName) ?? [];

      for (const listener of eventListeners) {
        listener(...args);
      }
    },
    quit: vi.fn(),
    exit: vi.fn(),
    requestSingleInstanceLock: vi.fn(() => true),
    getPath: vi.fn((name: string) => {
      if (name === "userData") {
        return "G:\\Workspace\\projects\\virtual_companion\\desktop\\test-user-data";
      }

      return "G:\\Workspace\\projects\\virtual_companion";
    }),
    setAppUserModelId: vi.fn()
  };
}

/**
 * mockFollowupShortcutServices 屏蔽 bootstrap 对后端控制服务的真实访问。
 * bootstrap 单测只关心壳层生命周期，不需要穿透到快捷键服务的网络细节。
 */
function mockFollowupShortcutServices() {
  vi.doMock("./services/managedLiveKitSupervisor", () => ({
    createManagedLiveKitSupervisor: vi.fn(() => ({
      start: vi.fn(async () => ({ url: "ws://127.0.0.1:20001", apiKey: "test-key", apiSecret: "test-secret" })),
      stop: vi.fn(async () => undefined),
      forceStop: vi.fn()
    }))
  }));
  vi.doMock("./services/followupControlClient", () => ({
    createFollowupControlClient: vi.fn(() => ({}))
  }));

  vi.doMock("./services/controlSettingsSync", () => ({
    createControlSettingsSyncService: vi.fn(() => ({
      start: vi.fn(async () => undefined),
      stop: vi.fn(),
      subscribe: vi.fn(() => vi.fn())
    }))
  }));

  vi.doMock("./services/shortcuts/desktopShortcutRegistry", () => ({
    createDesktopShortcutRegistryService: vi.fn(() => ({
      start: vi.fn(async () => undefined),
      stop: vi.fn()
    }))
  }));

  vi.doMock("./services/shortcuts/shortcutStatusClient", () => ({
    createShortcutStatusClient: vi.fn(() => ({
      reportShortcutStatus: vi.fn(async () => undefined)
    }))
  }));

  vi.doMock("./services/contextCapture", () => ({
    createEnvironmentCaptureService: vi.fn(() => ({
      captureSnapshot: vi.fn(async () => ({
        capturedAt: "2026-06-26T10:00:00.000Z",
        activeWindowTitle: null,
        screenshotBytes: null,
        captureError: null,
        timingsMs: {}
      }))
    }))
  }));

  vi.doMock("./services/environmentImageEncoder", () => ({
    createEnvironmentImageEncoder: vi.fn(() => ({ encode: vi.fn(), stop: vi.fn() }))
  }));
  vi.doMock("./services/windowsActiveWindow", () => ({
    createActiveWindowTitleProvider: vi.fn(() => ({ readTitle: vi.fn(() => null) }))
  }));
  vi.doMock("./services/environmentLegacyCleanup", () => ({
    cleanupLegacyEnvironmentSnapshots: vi.fn(async () => undefined)
  }));

  vi.doMock("./services/environmentSnapshotService", () => ({
    createEnvironmentSnapshotService: vi.fn(() => ({
      stop: vi.fn(),
      prepareTextSnapshot: vi.fn(),
      uploadTextSnapshot: vi.fn(async () => null),
      captureVoiceSnapshot: vi.fn(async () => undefined)
    }))
  }));

  vi.doMock("./services/materialsClient", () => ({
    createMaterialsClient: vi.fn(() => ({
      appendStagedMaterials: vi.fn(async items => ({
        admission: {
          submittedCount: items.length,
          addedCount: items.length,
          unsupportedCount: 0,
          unreadableCount: 0,
          duplicateCount: 0
        },
        materialContext: {
          revision: 1,
          turn_material_context: { staged_materials: [], active: null },
        }
      })),
      deleteStagedMaterial: vi.fn(),
      clearStagedMaterials: vi.fn()
    }))
  }));

  vi.doMock("./services/environmentContextClient", () => ({
    createEnvironmentContextClient: vi.fn(() => ({
      upload: vi.fn(async () => "snapshot-1")
    }))
  }));

  vi.doMock("./services/managedMaterialAssetStore", () => ({
    createManagedMaterialAssetStore: vi.fn(() => ({
      rootDir: "G:\\Workspace\\projects\\virtual_companion\\desktop\\test-user-data\\runtime-material-assets",
      prepareRuntime: vi.fn(async () => undefined),
      registerGeneratedAsset: vi.fn(() => "managed-asset-1"),
      release: vi.fn(async () => true),
      releaseAll: vi.fn(async () => undefined),
      releaseAllSync: vi.fn()
    }))
  }));

  vi.doMock("./services/turnSubmissionService", () => ({
    createTurnSubmissionService: vi.fn(() => ({
      submitTurn: vi.fn(async () => ({ revision: 0, items: [] })),
      editTurn: vi.fn(async () => ({ revision: 0, items: [] })),
      deleteTurn: vi.fn(async () => ({ revision: 0, items: [] })),
      resumeTurn: vi.fn(async () => ({ revision: 0, items: [] })),
      retryTurn: vi.fn(async () => ({ revision: 0, items: [] }))
    }))
  }));

  vi.doMock("./services/selectedContentCaptureService", () => ({
    createSelectedContentCaptureService: vi.fn(() => ({
      captureSelectedContent: vi.fn(async () => [])
    }))
  }));

  vi.doMock("./services/windowsSelectionCapturePort", () => ({
    createWindowsSelectionCapturePort: vi.fn(() => ({
      captureSelection: vi.fn(async () => ({
        filePaths: [],
        clipboardChanged: false,
        elapsedMs: 1500
      }))
    }))
  }));

  vi.doMock("./services/runtimeContextEventWatcher", () => ({
    createRuntimeContextEventWatcher: vi.fn(() => ({
      start: vi.fn(async () => undefined),
      stop: vi.fn()
    }))
  }));
}

/**
 * createIpcMainMock 提供 handle/on 注册与手动调用能力。
 * 这样测试可以直接驱动 bootstrap 暴露给 preload 的 IPC 契约，而不依赖真实 Electron 进程。
 */
export function createIpcMainMock() {
  const eventListeners = new Map<string, Array<(...args: unknown[]) => unknown>>();
  const handlers = new Map<string, (...args: unknown[]) => unknown>();

  function createIpcEvent(channel: string, candidate: unknown) {
    const existingEvent = candidate && typeof candidate === "object"
      ? candidate as Record<string, unknown>
      : {};

    if (existingEvent.sender) {
      return existingEvent;
    }

    const [avatarWindow, panelWindow, chatWindow] = FakeBrowserWindowRegistry.getAllWindows();
    const sender = channel.startsWith("panel:")
      ? panelWindow?.webContents
      : channel.startsWith("chat:") ||
          channel.startsWith("turn-submission:") ||
          channel.startsWith("environment:") ||
          channel.startsWith("materials:")
        ? chatWindow?.webContents
        : avatarWindow?.webContents;

    return {
      ...existingEvent,
      sender
    };
  }

  return {
    on: vi.fn((channel: string, listener: (...args: unknown[]) => void) => {
      /**
       * 同一 channel 允许累积多个监听器，行为贴近真实 ipcMain。
       * 这样重复 bootstrap 时如果忘记 removeAllListeners，测试会暴露重复副作用，而不是被 Map 覆盖静默隐藏。
       */
      const listeners = eventListeners.get(channel) ?? [];
      listeners.push(listener);
      eventListeners.set(channel, listeners);
    }),
    handle: vi.fn((channel: string, handler: (...args: unknown[]) => unknown) => {
      /**
       * ipcMain.handle 在真实 Electron 中不允许同一 channel 重复注册。
       * 测试 fake 也必须保留这个约束，避免 bootstrap 忘记 removeHandler 时被 Map 覆盖静默放过。
       */
      if (handlers.has(channel)) {
        throw new Error(`duplicate ipcMain.handle registration: ${channel}`);
      }

      handlers.set(channel, handler);
    }),
    removeAllListeners: vi.fn((channel: string) => {
      eventListeners.delete(channel);
    }),
    removeHandler: vi.fn((channel: string) => {
      handlers.delete(channel);
    }),
    emit(channel: string, ...args: unknown[]) {
      /**
       * 返回所有监听器结果，便于测试 send 风格监听器是否把 rejected promise 泄漏给调用侧。
       */
      const [candidateEvent, ...payload] = args;
      return (eventListeners.get(channel) ?? []).map(listener => listener(
        createIpcEvent(channel, candidateEvent),
        ...payload
      ));
    },
    invoke(channel: string, ...args: unknown[]) {
      const [candidateEvent, ...payload] = args;
      return handlers.get(channel)?.(
        createIpcEvent(channel, candidateEvent),
        ...payload
      );
    }
  };
}

/**
 * mockBootstrapFileSystem 提供 Electron bootstrap 测试需要的最小文件系统能力。
 *
 * Args:
 *   input.isMissing: 可选的缺失判定谓词，返回 true 的路径视为不存在；
 *     用于模拟随包资源损坏或 renderer 产物缺失场景。
 *
 * Returns:
 *   无返回值；该函数只为当前测试注册 node:fs mock。
 */
export function mockBootstrapFileSystem(input?: { isMissing?: (path: string) => boolean }): void {
  const packagedFileContent = Buffer.from("bootstrap-packaged-file", "utf8");
  const packagedFileHash = createHash("sha256").update(packagedFileContent).digest("hex");
  const manifestContent = JSON.stringify({
    schema_version: 3,
    product: "NearMate",
    platform: "windows-x64",
    python_version: "3.12.13",
    profile: "cuda",
    paths: {
      python: "python/Scripts/python.exe",
      backend: "backend",
      native: "native",
      shared: "shared",
      console: "console/dist",
      aec_package: "python/Lib/site-packages/audio_aec_sidecar",
      model_catalog: "model-catalog.json",
      seed_character_cards: "backend/seed-character-cards",
      assets: "assets"
    },
    required_files: ["python/Scripts/python.exe", "assets/icon.png"],
    files: [
      {
        path: "python/Scripts/python.exe",
        size: packagedFileContent.byteLength,
        sha256: packagedFileHash
      },
      {
        path: "assets/icon.png",
        size: packagedFileContent.byteLength,
        sha256: packagedFileHash
      }
    ]
  });
  const realpath = vi.fn((path: unknown) => String(path));

  vi.doMock("node:fs", () => ({
    existsSync: vi.fn((path: unknown) => !(input?.isMissing?.(String(path)) ?? false)),
    lstatSync: vi.fn((path: unknown) => {
      const resolvedPath = String(path);
      if (input?.isMissing?.(resolvedPath) ?? false) {
        throw Object.assign(new Error(`ENOENT: ${resolvedPath}`), { code: "ENOENT" });
      }
      return {
        size: packagedFileContent.byteLength,
        isFile: () => true,
        isSymbolicLink: () => false
      };
    }),
    readFileSync: vi.fn((path: unknown) => {
      const resolvedPath = String(path);
      if (input?.isMissing?.(resolvedPath) ?? false) {
        throw Object.assign(new Error(`ENOENT: ${resolvedPath}`), { code: "ENOENT" });
      }
      return resolvedPath.endsWith("runtime-manifest.json")
        ? manifestContent
        : packagedFileContent;
    }),
    realpathSync: Object.assign(realpath, { native: realpath }),
    mkdirSync: vi.fn(),
    readdirSync: vi.fn(() => []),
    rmSync: vi.fn()
  }));
}

/**
 * flushMicrotasks 用少量事件循环轮次等待 bootstrap 内部异步链收敛。
 * activate 重建 runtime 会串行经过多次 await，因此测试里需要显式等待，而不是假设同步完成。
 */
export async function flushMicrotasks(rounds = 4): Promise<void> {
  for (let index = 0; index < rounds; index += 1) {
    await Promise.resolve();
  }
}

/**
 * setupBootstrapHarness 负责搭建本测试文件新增场景所需的最小 bootstrap 运行时。
 * 它把窗口、控制器、IPC 与平台桥 mock 收敛到一处，避免新增测试重复拼接同样的脚手架代码。
 */
export async function setupBootstrapHarness(input?: {
  avatarWindow?: FakeElectronWindow;
  panelWindow?: FakeElectronWindow;
  chatWindow?: FakeElectronWindow;
  controlHttpUrl?: string;
  controllerStartsByShowingAvatar?: boolean;
  toggleAvatarSummonedVisibilityResult?: AvatarSummonMode;
  awaitBootstrap?: boolean;
  cursorPoint?: { x: number; y: number };
  isPackaged?: boolean;
  isPathMissing?: (path: string) => boolean;
  attachTray?: (trayInput: { toggleAvatarVisibility(): void }) => { destroy: ReturnType<typeof vi.fn> } | null;
}) {
  const appMock = createAppMock();
  appMock.isPackaged = input?.isPackaged ?? false;
  const ipcMainMock = createIpcMainMock();
  const openExternal = vi.fn(async () => undefined);
  const showItemInFolder = vi.fn();
  const showMessageBox = vi.fn(async (_options: unknown) => ({ response: 0 }));
  const tray = { destroy: vi.fn() };
  const avatarWindow = input?.avatarWindow ?? new FakeElectronWindow();
  const panelWindow = input?.panelWindow ?? new FakeElectronWindow();
  const chatWindow = input?.chatWindow ?? new FakeElectronWindow();
  let trayRuntimeInput: { toggleAvatarVisibility(): void } | null = null;
  let controllerMock: {
    start: ReturnType<typeof vi.fn>;
    hoverEnter: ReturnType<typeof vi.fn>;
    hoverLeave: ReturnType<typeof vi.fn>;
    toggleAvatarSummonedVisibility: ReturnType<typeof vi.fn>;
    startDrag: ReturnType<typeof vi.fn>;
    endDrag: ReturnType<typeof vi.fn>;
    openPanel: ReturnType<typeof vi.fn>;
    closePanelFromUserRequest: ReturnType<typeof vi.fn>;
    closePanelFromExternalBlur: ReturnType<typeof vi.fn>;
    refreshPanelPlacement: ReturnType<typeof vi.fn>;
  } | null = null;

  /**
   * Console 地址由主进程读取环境变量生成。
   * 测试在导入 bootstrap 前设置或清空它，避免当前开发机环境影响默认地址断言。
   */
  if (input?.controlHttpUrl === undefined) {
    delete process.env["COMPANION_CONTROL_HTTP_URL"];
  } else {
    process.env["COMPANION_CONTROL_HTTP_URL"] = input.controlHttpUrl;
  }

  vi.doMock("electron", () => ({
    app: appMock,
    ipcMain: ipcMainMock,
    clipboard: {
      readText: vi.fn(() => ""),
      writeText: vi.fn(),
      readImage: vi.fn(() => ({
        isEmpty: () => true,
        toPNG: () => Buffer.from("")
      })),
      writeImage: vi.fn(),
      clear: vi.fn()
    },
    dialog: {
      showMessageBox,
      showErrorBox: vi.fn()
    },
    globalShortcut: {
      register: vi.fn(() => true),
      unregister: vi.fn()
    },
    screen: {
      getDisplayMatching: vi.fn(() => FAKE_PRIMARY_DISPLAY),
      getPrimaryDisplay: vi.fn(() => FAKE_PRIMARY_DISPLAY),
      getCursorScreenPoint: vi.fn(() => input?.cursorPoint ?? { x: 0, y: 0 })
    },
    shell: {
      openExternal,
      showItemInFolder
    },
    BrowserWindow: {
      getAllWindows: () => FakeBrowserWindowRegistry.getAllWindows()
    }
  }));

  mockBootstrapFileSystem({ isMissing: input?.isPathMissing });

  vi.doMock("./windows/avatarWindow", () => ({
    createAvatarWindow: vi.fn(() => {
      FakeBrowserWindowRegistry.register(avatarWindow);
      return avatarWindow;
    })
  }));

  vi.doMock("./windows/panelWindow", () => ({
    createPanelWindow: vi.fn(() => {
      FakeBrowserWindowRegistry.register(panelWindow);
      return panelWindow;
    })
  }));

  vi.doMock("./windows/chatWindow", () => ({
    createChatWindow: vi.fn(() => {
      FakeBrowserWindowRegistry.register(chatWindow);
      return chatWindow;
    })
  }));

  vi.doMock("./controllers/shellController", () => ({
    createShellController: vi.fn((controllerInput: {
      avatarWindow: {
        show(): void;
      };
    }) => {
      controllerMock = {
        start: vi.fn(() => {
          if (input?.controllerStartsByShowingAvatar) {
            controllerInput.avatarWindow.show();
          }
        }),
        hoverEnter: vi.fn(),
        hoverLeave: vi.fn(),
        toggleAvatarSummonedVisibility: vi.fn(
          () => input?.toggleAvatarSummonedVisibilityResult ?? "summoned_topmost"
        ),
        startDrag: vi.fn(),
        endDrag: vi.fn(),
        openPanel: vi.fn(),
        closePanelFromUserRequest: vi.fn(),
        closePanelFromExternalBlur: vi.fn(),
        refreshPanelPlacement: vi.fn()
      };

      return controllerMock;
    })
  }));

  vi.doMock("./platform", () => ({
    createPlatformBridge: vi.fn(() => ({
      attachTray: vi.fn((nextTrayRuntimeInput: { toggleAvatarVisibility(): void }) => {
        if (input?.attachTray) {
          return input.attachTray(nextTrayRuntimeInput);
        }
        trayRuntimeInput = nextTrayRuntimeInput;
        return tray;
      })
    }))
  }));

  const { bootstrap } = await import("./bootstrap");
  const bootstrapPromise = bootstrap();

  if (input?.awaitBootstrap === false) {
    return {
      appMock,
      ipcMainMock,
      openExternal,
      showItemInFolder,
      showMessageBox,
      tray,
      avatarWindow,
      panelWindow,
      chatWindow,
      controllerMock,
      trayRuntimeInput,
      bootstrap,
      bootstrapPromise
    };
  }

  await bootstrapPromise;

  return {
    appMock,
    ipcMainMock,
    openExternal,
    showItemInFolder,
    showMessageBox,
    tray,
    avatarWindow,
    panelWindow,
    chatWindow,
    controllerMock: controllerMock!,
    trayRuntimeInput,
    bootstrap,
    bootstrapPromise
  };
}

beforeEach(() => {
  vi.resetModules();
  vi.restoreAllMocks();
  delete process.env["COMPANION_CONTROL_HTTP_URL"];
  FakeBrowserWindowRegistry.reset();
  mockFollowupShortcutServices();
});
