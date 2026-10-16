import * as React from "react";

import { cn } from "@/lib/utils";

type PageHeaderProps = {
  title: string;
  description?: string;
  /** 标题右侧的操作区（按钮、徽标等）。 */
  actions?: React.ReactNode;
  className?: string;
};

/**
 * 页面级标题区：页面主标题 + 操作说明 + 可选右侧操作。
 *
 * Returns:
 *   带下边框分隔的页面头部，说明文字与标题左对齐。
 */
export function PageHeader({ title, description, actions, className }: PageHeaderProps): JSX.Element {
  return (
    <div className={cn("flex flex-wrap items-end justify-between gap-x-6 gap-y-3 border-b pb-4", className)}>
      <div className="min-w-0 space-y-1">
        <h2 className="text-xl font-semibold tracking-tight">{title}</h2>
        {description !== undefined ? (
          <p className="max-w-2xl text-sm leading-relaxed text-muted-foreground">{description}</p>
        ) : null}
      </div>
      {actions !== undefined ? <div className="flex flex-wrap items-center gap-2">{actions}</div> : null}
    </div>
  );
}
