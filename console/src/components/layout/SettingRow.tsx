import * as React from "react";

import { cn } from "@/lib/utils";

type SettingRowProps = {
  title: string;
  /** 补充说明，纯文本或富内容均可。 */
  description?: React.ReactNode;
  /**
   * 布局模式：inline 控件靠右（开关、按钮）；
   * field 控件占固定宽度（输入框、下拉等宽控件）。
   */
  layout?: "inline" | "field";
  /** 控件上方的补充说明以外的自由内容，一般不使用。 */
  children: React.ReactNode;
  className?: string;
};

/**
 * 设置项行：左文右控的统一设置排版单元。
 *
 * Args:
 *   title: 设置项名称。
 *   description: 面向操作的补充说明。
 *   layout: 控件布局模式。
 *   children: 右侧（inline）或定宽（field）控件。
 *
 * Returns:
 *   一行设置项；控件与文案的可访问性关联由调用方在控件上保留
 *   （aria-label 或 Label htmlFor），本组件只负责排版。
 */
export function SettingRow({ title, description, layout = "inline", children, className }: SettingRowProps): JSX.Element {
  return (
    <div className={cn("flex flex-wrap items-center justify-between gap-x-6 gap-y-3 py-4", className)}>
      <div className="min-w-0 max-w-xl space-y-1">
        <h3 className="text-sm font-medium">{title}</h3>
        {description !== undefined ? (
          <div className="text-sm leading-relaxed text-muted-foreground">{description}</div>
        ) : null}
      </div>
      <div className={cn(layout === "field" ? "w-full sm:w-96" : "shrink-0")}>{children}</div>
    </div>
  );
}
