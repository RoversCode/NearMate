import type { DesktopRuntimeSettingsSnapshot } from "../controlSettingsSync";
import type {
  ShortcutId,
  ShortcutStatus,
  ShortcutStatusClient,
  ShortcutStatusItemPayload
} from "./shortcutTypes";

type GlobalShortcutLike = {
  register(accelerator: string, callback: () => void): boolean;
  unregister(accelerator: string): void;
};

type ShortcutDefinition = {
  id: ShortcutId;
  enabled: boolean;
  accelerator: string;
  run(): void | Promise<void>;
};

type CreateDesktopShortcutRegistryServiceOptions = {
  globalShortcut: GlobalShortcutLike;
  settingsSync: {
    subscribe(listener: (settings: DesktopRuntimeSettingsSnapshot) => void): () => void;
  };
  statusClient: Pick<ShortcutStatusClient, "reportShortcutStatus">;
  actions: {
    toggleFollowup(): void | Promise<void>;
    toggleAvatarVisibility(): void | Promise<void>;
    captureSelection(): void | Promise<void>;
  };
  onShortcutPressed?(shortcutId: ShortcutId): void;
  now?: () => string;
  logger?: Pick<Console, "info" | "warn">;
};

export type DesktopShortcutRegistryService = {
  start(): Promise<void>;
  stop(): void;
};

/**
 * 创建桌面全局快捷键统一注册器。
 *
 * @param options Electron 全局快捷键接口、设置源、状态上报客户端和业务动作。
 * @returns 可随主进程生命周期启动和停止的服务。
 */
export function createDesktopShortcutRegistryService(
  options: CreateDesktopShortcutRegistryServiceOptions
): DesktopShortcutRegistryService {
  const logger = options.logger ?? console;
  const now = options.now ?? (() => new Date().toISOString());
  const registeredAccelerators = new Set<string>();
  let appliedDefinitions: ShortcutDefinition[] | null = null;
  let latestStatusById = new Map<ShortcutId, ShortcutStatusItemPayload>();
  let unsubscribeSettings: (() => void) | null = null;
  let started = false;

  function unregisterAll(): void {
    for (const accelerator of registeredAccelerators) {
      options.globalShortcut.unregister(accelerator);
      logger.info?.("[companion-main] desktop shortcut unregistered", accelerator);
    }
    registeredAccelerators.clear();
  }

  function applyShortcutSettings(settings: DesktopRuntimeSettingsSnapshot): void {
    const definitions = shortcutDefinitionsFromSettings(settings, options.actions);
    const shouldReRegister = appliedDefinitions === null
      || !sameShortcutDefinitions(appliedDefinitions, definitions);
    let items: ShortcutStatusItemPayload[];

    if (shouldReRegister) {
      unregisterAll();
      const duplicatedAccelerators = duplicatedEnabledAccelerators(definitions);
      items = definitions.map((definition) => {
        if (!definition.enabled) {
          return statusItem(settings.revision, definition, "disabled", "快捷键已关闭", now());
        }

        if (duplicatedAccelerators.has(normalizeAcceleratorKey(definition.accelerator))) {
          return statusItem(settings.revision, definition, "internal_conflict", "与 NearMate 其它快捷键重复", now());
        }

        const registered = options.globalShortcut.register(definition.accelerator, () => {
          logger.info?.("[companion-main] desktop shortcut pressed", definition.id, definition.accelerator);
          // 反馈必须先于 HTTP、选区读取等慢动作，声音只表示系统接到了这次按键。
          try {
            options.onShortcutPressed?.(definition.id);
          } catch (error) {
            logger.warn("[companion-main] desktop shortcut feedback failed", definition.id, error);
          }
          try {
            void Promise.resolve(definition.run()).catch((error) => {
              logger.warn("[companion-main] desktop shortcut action failed", definition.id, error);
            });
          } catch (error) {
            logger.warn("[companion-main] desktop shortcut action failed", definition.id, error);
          }
        });

        if (!registered) {
          return statusItem(settings.revision, definition, "unavailable", "可能被系统或其他应用占用", now());
        }

        registeredAccelerators.add(definition.accelerator);
        return statusItem(settings.revision, definition, "registered", "快捷键可用", now());
      });
    } else {
      // 普通资料或音频设置也会推进全局 revision，但组合键没有变化；
      // 复用已有注册结果，避免每次保存都出现短暂的全局快捷键空窗。
      items = definitions.map((definition) => {
        const previousStatus = latestStatusById.get(definition.id);
        if (previousStatus === undefined) {
          return statusItem(settings.revision, definition, "unknown", "等待桌面端检测", now());
        }
        return {
          ...previousStatus,
          accelerator: definition.accelerator,
          enabled: definition.enabled,
          settings_revision: settings.revision,
          checked_at: now()
        };
      });
    }

    appliedDefinitions = definitions;
    latestStatusById = new Map(items.map((item) => [item.shortcut_id, item]));

    const payload = {
      settings_revision: settings.revision,
      items
    };
    void options.statusClient.reportShortcutStatus(payload).catch((error) => {
      logger.warn("[companion-main] shortcut status report failed", error);
    });
  }

  return {
    async start(): Promise<void> {
      if (started) {
        return;
      }

      started = true;
      unsubscribeSettings = options.settingsSync.subscribe((settings) => {
        applyShortcutSettings(settings);
      });
    },

    stop(): void {
      unregisterAll();
      unsubscribeSettings?.();
      unsubscribeSettings = null;
      appliedDefinitions = null;
      latestStatusById = new Map();
      started = false;
    }
  };
}

/**
 * 判断两次设置是否只改变了全局 revision。
 *
 * Args:
 *   previousDefinitions: 上一次已经应用的快捷键定义。
 *   nextDefinitions: 当前设置快照生成的快捷键定义。
 *
 * Returns:
 *   三个快捷键的启用状态和组合键都相同则返回 true。
 */
function sameShortcutDefinitions(
  previousDefinitions: ShortcutDefinition[],
  nextDefinitions: ShortcutDefinition[]
): boolean {
  return previousDefinitions.length === nextDefinitions.length
    && previousDefinitions.every((previousDefinition, index) => {
      const nextDefinition = nextDefinitions[index];
      return nextDefinition !== undefined
        && previousDefinition.id === nextDefinition.id
        && previousDefinition.enabled === nextDefinition.enabled
        && previousDefinition.accelerator === nextDefinition.accelerator;
    });
}

/**
 * 从设置快照构造三类桌面快捷键定义。
 */
function shortcutDefinitionsFromSettings(
  settings: DesktopRuntimeSettingsSnapshot,
  actions: CreateDesktopShortcutRegistryServiceOptions["actions"]
): ShortcutDefinition[] {
  return [
    {
      id: "followup_toggle",
      enabled: settings.followupShortcut.enabled,
      accelerator: settings.followupShortcut.accelerator,
      run: actions.toggleFollowup
    },
    {
      id: "avatar_visibility",
      enabled: settings.avatarVisibilityShortcut.enabled,
      accelerator: settings.avatarVisibilityShortcut.accelerator,
      run: actions.toggleAvatarVisibility
    },
    {
      id: "selection_capture",
      enabled: settings.selectionCaptureShortcut.enabled,
      accelerator: settings.selectionCaptureShortcut.accelerator,
      run: actions.captureSelection
    }
  ];
}

/**
 * 找出已启用快捷键中重复的组合键。
 */
function duplicatedEnabledAccelerators(definitions: ShortcutDefinition[]): Set<string> {
  const counts = new Map<string, number>();
  for (const definition of definitions) {
    if (!definition.enabled) {
      continue;
    }
    const key = normalizeAcceleratorKey(definition.accelerator);
    counts.set(key, (counts.get(key) ?? 0) + 1);
  }

  return new Set(
    [...counts.entries()]
      .filter(([, count]) => count > 1)
      .map(([accelerator]) => accelerator)
  );
}

/**
 * 归一化组合键用于 NearMate 内部重复判断。
 */
function normalizeAcceleratorKey(accelerator: string): string {
  return accelerator.trim().toLowerCase();
}

/**
 * 构造单个快捷键状态上报项。
 */
function statusItem(
  settingsRevision: number,
  definition: ShortcutDefinition,
  status: ShortcutStatus,
  message: string,
  checkedAt: string
): ShortcutStatusItemPayload {
  return {
    shortcut_id: definition.id,
    accelerator: definition.accelerator,
    enabled: definition.enabled,
    status,
    settings_revision: settingsRevision,
    checked_at: checkedAt,
    message
  };
}
