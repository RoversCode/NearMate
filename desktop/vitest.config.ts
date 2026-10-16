import { resolve } from "node:path";
import { defineConfig } from "vitest/config";

export default defineConfig({
  // 测试缓存写入仓库根目录 node_modules，避免 desktop 子目录重新生成独立 node_modules 入口。
  cacheDir: resolve(__dirname, "../node_modules/.vite/desktop")
});
