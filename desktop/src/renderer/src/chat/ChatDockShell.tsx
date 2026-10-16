import type { ReactNode } from "react";

export type ChatDockMode = "collapsed" | "expanded";

type ChatDockShellProps = {
  mode: ChatDockMode;
  children: ReactNode;
};

/**
 * ChatDockShell 渲染聊天窗口外壳。
 *
 * Args:
 *   props: 当前折叠状态与展开后的聊天内容。
 *
 * Returns:
 *   展开态聊天面板内容；折叠态返回空节点，由主进程保持窗口隐藏。
 */
export function ChatDockShell({
  mode,
  children
}: ChatDockShellProps) {
  if (mode === "collapsed") {
    // collapsed 是隐藏态。入口只存在于 AvatarActionRail，避免桌面出现第二个聊天入口。
    return null;
  }

  return <>{children}</>;
}
