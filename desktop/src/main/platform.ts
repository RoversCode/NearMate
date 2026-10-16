import {
  app,
  Menu,
  Tray,
  nativeImage
} from "electron";
import { join } from "node:path";

/**
 * 支持的平台快照只暴露 UI 真正需要的能力，不把 Electron 细节散到 renderer。
 */
type PlatformSnapshot = {
  id: NodeJS.Platform;
  label: string;
  supportsTray: boolean;
  supportsVibrancy: boolean;
};

/**
 * 平台桥负责收敛所有和操作系统有关的差异。
 * 这样主进程的启动流程就只关心“什么时候调用能力”，不关心“每个平台怎么做”。
 */
export type PlatformBridge = {
  snapshot: PlatformSnapshot;
  attachTray(input: {
    toggleAvatarVisibility(): void;
    openConsole(): void;
  }): Tray | null;
};

/**
 * 根据当前运行平台构建统一能力对象。
 * 如果未来新增 Linux 或者更细的窗口策略，只需要在这一处扩展。
 */
export function createPlatformBridge(input: { appResourceRoot: string }): PlatformBridge {
  /**
   * 平台信息在应用生命周期内稳定不变，因此直接初始化成常量即可。
   */
  const snapshot = createPlatformSnapshot();

  /**
   * 托盘图标需要在不同平台下尽量保持可见。
   * 这里统一从正式图标资源生成托盘尺寸图像，避免各平台直接缩放原图造成显示发虚。
   */
  function createTrayIcon(): Electron.NativeImage {
    const trayIconPath = join(input.appResourceRoot, "icon.png");
    const trayIcon = nativeImage.createFromPath(trayIconPath);
    if (trayIcon.isEmpty()) {
      throw new Error(`NearMate 托盘图标资源无效：${trayIconPath}`);
    }

    return trayIcon.resize({
      width: 16,
      height: 16
    });
  }

  return {
    snapshot,

    /**
     * 托盘是桌面伴侣常驻的关键入口。
     * 这里提供极小但完整的交互：显示/隐藏窗口与退出应用。
     */
    attachTray(input) {
      /**
       * 托盘图标单独收口，后续如果替换为正式品牌资源，只需要改这一处生成逻辑。
       */
      const trayIcon = createTrayIcon();
      const tray = new Tray(trayIcon);

      /**
       * 提示文案用于鼠标悬停时确认应用身份，避免用户只能凭空点一个几乎无语义的图块。
       */
      tray.setToolTip("NearMate");

      /**
       * macOS 在个别开发环境下即便图标素材可用，也可能因为系统渲染策略显得过淡。
       * 这里给开发阶段补一个极短标题兜底，确保用户至少能一眼识别菜单栏里有应用常驻。
       */
      if (process.platform === "darwin" && process.env["NODE_ENV"] !== "production") {
        tray.setTitle("伴");
      }

      tray.setContextMenu(
        Menu.buildFromTemplate([
          {
            label: "显示 / 收起桌宠",
            click: () => input.toggleAvatarVisibility()
          },
          {
            label: "打开 Console",
            click: () => input.openConsole()
          },
          {
            label: "退出应用",
            click: () => app.quit()
          }
        ])
      );
      tray.on("click", () => input.toggleAvatarVisibility());

      return tray;
    }
  };
}

/**
 * 平台快照只暴露跨层真正需要的信息。
 * renderer 只应该看到“这是什么平台、支持什么能力”，而不该直接依赖 Electron API。
 */
function createPlatformSnapshot(): PlatformSnapshot {
  if (process.platform === "darwin") {
    return {
      id: process.platform,
      label: "macOS",
      supportsTray: true,
      supportsVibrancy: true
    };
  }

  if (process.platform === "win32") {
    return {
      id: process.platform,
      label: "Windows",
      supportsTray: true,
      supportsVibrancy: false
    };
  }

  return {
    id: process.platform,
    label: process.platform,
    supportsTray: true,
    supportsVibrancy: false
  };
}
