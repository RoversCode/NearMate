import { ChevronDown } from "lucide-react";
import * as React from "react";

import { cn } from "@/lib/utils";

/**
 * 控制台统一下拉选择框。
 *
 * 刻意保留原生 select（而非无头自定义下拉）：控制台表单大量使用
 * label 关联与 change 事件，原生元素的可访问性语义与既有测试契约完全兼容。
 * className 作用于外层容器，用于控制整体宽度等布局属性。
 *
 * Returns:
 *   带自定义展开图标的原生 select 元素。
 */
const Select = React.forwardRef<HTMLSelectElement, React.SelectHTMLAttributes<HTMLSelectElement>>(
  ({ className, children, ...props }, ref) => (
    <div className={cn("relative w-full", className)}>
      <select
        className="h-9 w-full appearance-none rounded-md border border-input bg-card pl-3 pr-8 text-sm shadow-sm transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-1 focus-visible:ring-offset-background disabled:cursor-not-allowed disabled:opacity-50"
        ref={ref}
        {...props}
      >
        {children}
      </select>
      <ChevronDown aria-hidden="true" className="pointer-events-none absolute right-2.5 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
    </div>
  )
);
Select.displayName = "Select";

export { Select };
