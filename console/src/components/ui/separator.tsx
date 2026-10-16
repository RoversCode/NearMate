import * as React from "react";

import { cn } from "@/lib/utils";

type SeparatorProps = React.HTMLAttributes<HTMLDivElement> & {
  /** 分割线方向，默认水平。 */
  orientation?: "horizontal" | "vertical";
};

/**
 * 视觉分割线。
 *
 * Args:
 *   orientation: horizontal 时横向通栏，vertical 时纵向拉伸占满父级高度。
 *
 * Returns:
 *   带 separator 角色的分割线元素。
 */
function Separator({ className, orientation = "horizontal", ...props }: SeparatorProps): JSX.Element {
  return (
    <div
      aria-orientation={orientation}
      className={cn(
        "shrink-0 bg-border",
        orientation === "horizontal" ? "h-px w-full" : "h-full w-px",
        className
      )}
      role="separator"
      {...props}
    />
  );
}

export { Separator };
