import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import type { WindowKind } from "../contracts/windowKinds";

/**
 * 基于 ESM URL 计算当前目录，目的是让构建入口在测试与打包环境下都能稳定解析。
 * 这里不依赖进程工作目录，避免多工作树或脚本切换目录时出现相对路径漂移。
 */
const __dirname = dirname(fileURLToPath(import.meta.url));

/**
 * 入口路径映射类型显式绑定 WindowKind。
 * 这样任何窗口种类变更都会立刻反馈到 preload 与 renderer 两张表，避免漏配入口。
 */
type WindowEntryTable = Record<WindowKind, string>;

/**
 * electron-vite 入口路径在运行时可以接受正斜杠形式。
 * 这里统一把 Windows 反斜杠规范化，避免测试和跨平台字符串比较出现伪失败。
 */
function resolveWindowEntryPath(...segments: string[]): string {
  return resolve(__dirname, ...segments).replaceAll("\\", "/");
}

/**
 * 多窗口构建入口清单只负责提供路径映射，不混入任何窗口创建逻辑。
 * sandbox preload 使用一个自包含入口，再根据 main 注入的窗口类型安装对应 bridge；renderer 仍保持逐窗入口。
 */
export const windowEntryInputs = {
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
  } satisfies WindowEntryTable
};
