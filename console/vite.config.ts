import react from "@vitejs/plugin-react";
import { fileURLToPath, URL } from "node:url";
import { defineConfig } from "vite";
import devPorts from "../shared/dev-ports.json";

const backendHttpOrigin = `http://${devPorts.backend.host}:${devPorts.backend.port}`;
const backendWebSocketOrigin = `ws://${devPorts.backend.host}:${devPorts.backend.port}`;

export default defineConfig({
  base: "/console/",
  cacheDir: "../node_modules/.vite/console",
  plugins: [react()],
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url))
    }
  },
  server: {
    host: devPorts.console.host,
    port: devPorts.console.port,
    strictPort: true,
    proxy: {
      // 开发服务器将控制台 API 转发到本地后端，保持 React 代码与生产静态托管同源契约一致。
      "/api": backendHttpOrigin,
      // WebSocket 使用同一个控制面入口，后续实时刷新功能可以直接复用该代理配置。
      "/ws": {
        target: backendWebSocketOrigin,
        ws: true
      }
    }
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"]
  }
});
