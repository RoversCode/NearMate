import { CircleCheck, Loader2, TriangleAlert } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import type { ConsoleLoadState } from "@/app/consoleTypes";

const statusMetaByStatus: Record<
  ConsoleLoadState["status"],
  { label: string; variant: "secondary" | "success" | "destructive"; icon: typeof Loader2; spin: boolean }
> = {
  loading: { label: "加载中", variant: "secondary", icon: Loader2, spin: true },
  ready: { label: "已连接", variant: "success", icon: CircleCheck, spin: false },
  failed: { label: "接口异常", variant: "destructive", icon: TriangleAlert, spin: false }
};

/**
 * 渲染控制台首屏接口的连接状态徽标。
 *
 * Args:
 *   loadState: 首屏接口加载状态。
 *
 * Returns:
 *   加载中、已连接或异常三种语义的徽标。
 */
export function StatusBadge({ loadState }: { loadState: ConsoleLoadState }): JSX.Element {
  const meta = statusMetaByStatus[loadState.status];
  const Icon = meta.icon;

  return (
    <Badge variant={meta.variant}>
      <Icon aria-hidden="true" className={meta.spin ? "size-3.5 animate-spin" : "size-3.5"} />
      {meta.label}
    </Badge>
  );
}
