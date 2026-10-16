import { Inbox, type LucideIcon } from "lucide-react";
import * as React from "react";

import { cn } from "@/lib/utils";

type EmptyStateProps = {
  /** 自定义图标，缺省为 Inbox。 */
  icon?: LucideIcon;
  children: React.ReactNode;
  className?: string;
};

/**
 * 空态/占位提示：虚线容器 + 图标 + 说明文字。
 *
 * Returns:
 *   居中的空态区块，用于"暂无数据""等待加载"等场景。
 */
export function EmptyState({ icon: Icon = Inbox, children, className }: EmptyStateProps): JSX.Element {
  return (
    <div
      className={cn(
        "flex flex-col items-center justify-center gap-2 rounded-lg border border-dashed px-6 py-10 text-center",
        className
      )}
    >
      <Icon aria-hidden="true" className="size-5 text-muted-foreground" />
      <div className="text-sm leading-relaxed text-muted-foreground">{children}</div>
    </div>
  );
}
