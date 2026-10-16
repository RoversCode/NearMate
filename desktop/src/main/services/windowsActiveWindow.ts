import { createRequire } from "node:module";

const require = createRequire(import.meta.url);

export type ActiveWindowTitleProvider = {
  readTitle(): string | null;
};

/**
 * 创建进程内 Windows 活动窗口标题读取器。
 *
 * Args:
 *   platform: 当前 Node 平台，测试可注入非 Windows 值。
 *   loadKoffi: 首次读取标题时使用的 Koffi 加载入口。
 *
 * Returns:
 *   不启动外部进程的活动窗口标题读取端口。
 */
export function createActiveWindowTitleProvider(
  platform: NodeJS.Platform = process.platform,
  loadKoffi: () => Pick<typeof import("koffi"), "load"> = () => require("koffi") as typeof import("koffi")
): ActiveWindowTitleProvider {
  if (platform !== "win32") {
    return { readTitle: () => null };
  }

  let readWindowsTitle: (() => string | null) | null = null;

  return {
    readTitle(): string | null {
      if (readWindowsTitle === null) {
        const { load } = loadKoffi();
        const user32 = load("user32.dll");
        const getForegroundWindow = user32.func("void * __stdcall GetForegroundWindow()");
        const getWindowText = user32.func(
          "int __stdcall GetWindowTextW(void *window, _Out_ uint16_t *text, int maxCount)"
        );
        readWindowsTitle = () => {
          const windowHandle = getForegroundWindow();
          if (!windowHandle) {
            return null;
          }
          const titleBuffer = Buffer.alloc(4096);
          const characterCount = Number(getWindowText(windowHandle, titleBuffer, titleBuffer.length / 2));
          if (characterCount <= 0) {
            return null;
          }
          return titleBuffer.subarray(0, characterCount * 2).toString("utf16le").trim() || null;
        };
      }
      return readWindowsTitle();
    }
  };
}
