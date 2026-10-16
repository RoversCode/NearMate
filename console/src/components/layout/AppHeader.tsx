import { StatusBadge } from "./StatusBadge";
import { ThemeToggle } from "./ThemeToggle";
import type { ConsoleLoadState } from "@/app/consoleTypes";

type AppHeaderProps = {
  title: string;
  description: string;
  loadState: ConsoleLoadState;
};

/**
 * 控制台顶部栏：当前页面标题与描述 + 连接状态 + 主题切换。
 *
 * Args:
 *   title: 当前页面名称。
 *   description: 当前页面的操作说明。
 *   loadState: 首屏接口加载状态。
 *
 * Returns:
 *   工作台顶栏；移动端自动换行堆叠。
 */
export function AppHeader({ title, description, loadState }: AppHeaderProps): JSX.Element {
  return (
    <header className="flex min-h-14 flex-wrap items-center justify-between gap-x-6 gap-y-2 border-b bg-card px-4 py-2 sm:px-6 lg:px-8">
      <div className="flex min-w-0 items-center gap-3">
        <span className="shrink-0 text-sm font-medium">{title}</span>
        <span aria-hidden="true" className="hidden h-4 border-l xl:block" />
        <p className="hidden truncate text-xs text-muted-foreground xl:block">{description}</p>
      </div>
      <div className="flex items-center gap-2">
        <StatusBadge loadState={loadState} />
        <ThemeToggle />
      </div>
    </header>
  );
}
