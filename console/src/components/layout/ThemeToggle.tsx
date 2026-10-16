import { Monitor, Moon, Sun } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { useConsoleTheme, type ConsoleTheme } from "@/lib/theme";

const themeMetaByTheme: Record<ConsoleTheme, { label: string; icon: typeof Sun }> = {
  light: { label: "浅色", icon: Sun },
  dark: { label: "深色", icon: Moon },
  system: { label: "跟随系统", icon: Monitor }
};

const nextThemeByTheme: Record<ConsoleTheme, ConsoleTheme> = {
  light: "dark",
  dark: "system",
  system: "light"
};

/**
 * 主题切换按钮：浅色 → 深色 → 跟随系统 循环。
 *
 * Returns:
 *   带悬停提示的图标按钮，展示并切换当前主题。
 */
export function ThemeToggle(): JSX.Element {
  const { theme, setTheme } = useConsoleTheme();
  const meta = themeMetaByTheme[theme];
  const Icon = meta.icon;

  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button
          aria-label={`切换主题（当前：${meta.label}）`}
          onClick={() => setTheme(nextThemeByTheme[theme])}
          size="icon"
          type="button"
          variant="ghost"
        >
          <Icon aria-hidden="true" />
        </Button>
      </TooltipTrigger>
      <TooltipContent>主题：{meta.label}</TooltipContent>
    </Tooltip>
  );
}
