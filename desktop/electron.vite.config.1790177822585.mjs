// electron.vite.config.ts
import { resolve as resolve2 } from "node:path";
import { defineConfig } from "electron-vite";
import react from "@vitejs/plugin-react";

// src/main/assets/windowEntries.ts
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
var __electron_vite_injected_import_meta_url = "file:///G:/Workspace/projects/virtual_companion/desktop/src/main/assets/windowEntries.ts";
var __dirname2 = dirname(fileURLToPath(__electron_vite_injected_import_meta_url));
function resolveWindowEntryPath(...segments) {
  return resolve(__dirname2, ...segments).replaceAll("\\", "/");
}
var windowEntryInputs = {
  /** sandbox preload 不能依赖 ESM 或运行时共享 chunk，因此只构建一个 CommonJS 文件。 */
  preload: {
    sandbox: resolveWindowEntryPath("../../preload/index.ts")
  },
  /**
   * renderer 入口先保持为最薄 HTML 壳。
   * 这样可以先完成多窗口打包拆分，再逐步接入真实 UI。
   */
  renderer: {
    avatar: resolveWindowEntryPath("../../renderer/avatar.html"),
    panel: resolveWindowEntryPath("../../renderer/panel.html"),
    chat: resolveWindowEntryPath("../../renderer/chat.html")
  }
};

// ../shared/dev-ports.json
var dev_ports_default = {
  backend: {
    host: "127.0.0.1",
    port: 8765
  },
  console: {
    host: "127.0.0.1",
    port: 5173
  },
  desktop: {
    host: "127.0.0.1",
    port: 5174
  },
  livekit: {
    host: "127.0.0.1",
    port: 7880
  }
};

// electron.vite.config.ts
var __electron_vite_injected_dirname = "G:\\Workspace\\projects\\virtual_companion\\desktop";
var electron_vite_config_default = defineConfig({
  main: {
    build: {
      outDir: "dist-electron/main"
    }
  },
  preload: {
    build: {
      outDir: "dist-electron/preload",
      /**
       * preload 只构建一个自包含 CommonJS 入口，确保 Electron sandbox 能直接执行。
       * 各窗口桥接仍会按 main 注入的窗口配置自行判断，不会向错误窗口暴露 API。
       */
      rollupOptions: {
        input: windowEntryInputs.preload,
        output: {
          entryFileNames: "[name].cjs",
          format: "cjs",
          inlineDynamicImports: true
        }
      }
    }
  },
  renderer: {
    root: resolve2(__electron_vite_injected_dirname, "src/renderer"),
    publicDir: resolve2(__electron_vite_injected_dirname, "src/renderer/public"),
    cacheDir: resolve2(__electron_vite_injected_dirname, "../node_modules/.vite/desktop-renderer"),
    /**
     * 将开发服务器固定到 IPv4 回环地址，
     * 目的是避开部分机器或沙箱环境对 IPv6 `::1` 监听的兼容性问题。
     * 这样 Electron 在开发态回连 renderer 时也更稳定。
     */
    server: {
      host: dev_ports_default.desktop.host,
      port: dev_ports_default.desktop.port,
      strictPort: true
    },
    build: {
      outDir: resolve2(__electron_vite_injected_dirname, "dist/renderer"),
      /**
       * renderer 构建入口与 preload 保持同一组窗口 key。
       * 这样可以降低配置漂移风险，并让测试直接校验关键构建契约。
       */
      rollupOptions: {
        input: windowEntryInputs.renderer
      }
    },
    plugins: [react()]
  }
});
export {
  electron_vite_config_default as default
};
