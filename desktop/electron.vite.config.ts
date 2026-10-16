import { resolve } from "node:path";
import { defineConfig } from "electron-vite";
import react from "@vitejs/plugin-react";
import { windowEntryInputs } from "./src/main/assets/windowEntries";
import devPorts from "../shared/dev-ports.json";

/**
 * 这一份配置只做三件事情：
 * 1. 明确 main、preload、renderer 三个构建出口，避免后续文件路径猜测。
 * 2. 给 renderer 打开 React 插件，保持和未来组件演进兼容。
 * 3. 为测试与跨目录 JSON 导入保留稳定的打包边界。
 */
export default defineConfig({
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
    root: resolve(__dirname, "src/renderer"),
    publicDir: resolve(__dirname, "src/renderer/public"),
    cacheDir: resolve(__dirname, "../node_modules/.vite/desktop-renderer"),
    /**
     * 将开发服务器固定到 IPv4 回环地址，
     * 目的是避开部分机器或沙箱环境对 IPv6 `::1` 监听的兼容性问题。
     * 这样 Electron 在开发态回连 renderer 时也更稳定。
     */
    server: {
      host: devPorts.desktop.host,
      port: devPorts.desktop.port,
      strictPort: true
    },
    build: {
      outDir: resolve(__dirname, "dist/renderer"),
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
