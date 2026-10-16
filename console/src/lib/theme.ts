import { useCallback, useEffect, useState } from "react";

/**
 * 控制台主题（浅色/深色/跟随系统）的存取与应用。
 *
 * 主题以 class 形式挂在 documentElement 上（darkMode: "class"），
 * 选择持久化在 localStorage；jsdom 测试环境没有 matchMedia，全部做了降级保护。
 */

export type ConsoleTheme = "light" | "dark" | "system";

const THEME_STORAGE_KEY = "vc-console-theme";

/**
 * 读取持久化的主题选择。
 *
 * Returns:
 *   用户上次选择的主题；无记录或环境不支持 localStorage 时返回 "system"。
 */
function getStoredTheme(): ConsoleTheme {
  try {
    const stored = window.localStorage.getItem(THEME_STORAGE_KEY);
    if (stored === "light" || stored === "dark" || stored === "system") {
      return stored;
    }
  } catch {
    // localStorage 在隐私模式或测试环境可能不可用，静默降级为跟随系统。
  }
  return "system";
}

/**
 * 根据系统偏好推断实际明暗结果。
 *
 * Returns:
 *   系统偏好深色时返回 "dark"，环境不支持 matchMedia 时降级为 "light"。
 */
function resolveSystemTheme(): "light" | "dark" {
  if (typeof window.matchMedia !== "function") {
    return "light";
  }
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

/**
 * 把主题应用到根元素 class 上，Tailwind 的 dark: 变体随即生效。
 *
 * Args:
 *   theme: 用户选择的主题模式。
 */
function applyThemeClass(theme: ConsoleTheme): void {
  const resolved = theme === "system" ? resolveSystemTheme() : theme;
  document.documentElement.classList.toggle("dark", resolved === "dark");
}

/**
 * 控制台主题状态 Hook。
 *
 * Returns:
 *   theme 为当前选择（含 "system"），setTheme 负责持久化并应用。
 */
export function useConsoleTheme(): {
  theme: ConsoleTheme;
  setTheme: (theme: ConsoleTheme) => void;
} {
  const [theme, setThemeState] = useState<ConsoleTheme>(() => getStoredTheme());

  useEffect(() => {
    applyThemeClass(theme);
  }, [theme]);

  useEffect(() => {
    // 仅在跟随系统时监听系统偏好变化，手动选择时不覆盖用户决定。
    if (theme !== "system" || typeof window.matchMedia !== "function") {
      return;
    }
    const media = window.matchMedia("(prefers-color-scheme: dark)");
    const handleChange = (): void => applyThemeClass("system");
    media.addEventListener("change", handleChange);
    return () => media.removeEventListener("change", handleChange);
  }, [theme]);

  const setTheme = useCallback((nextTheme: ConsoleTheme): void => {
    setThemeState(nextTheme);
    try {
      window.localStorage.setItem(THEME_STORAGE_KEY, nextTheme);
    } catch {
      // 写入失败时主题仍然生效，只是不持久化，无需打扰用户。
    }
  }, []);

  return { theme, setTheme };
}
