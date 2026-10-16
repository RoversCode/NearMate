import { isStandardUuid } from "./uuid";

export type PanelView = "models";

export type ConsoleTarget =
  | { section: "settings" }
  | { section: "tasks"; activityId?: string };

export const PANEL_VIEW_SIZES: Record<PanelView, { width: number; height: number }> = {
  models: { width: 380, height: 560 }
};

/**
 * readPanelView 校验 renderer 请求打开的面板视图。
 *
 * @param payload 未受信任的 IPC 载荷。
 * @returns 合法视图；非法时返回空。
 */
export function readPanelView(payload: unknown): PanelView | null {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    return null;
  }

  const view = (payload as { view?: unknown }).view;
  return view === "models" ? view : null;
}

/**
 * readConsoleTarget 将 renderer 输入收敛为固定 Console 导航目标。
 *
 * @param payload 未受信任的 IPC 载荷。
 * @returns 合法 settings/tasks 目标；非法时返回空。
 */
export function readConsoleTarget(payload: unknown): ConsoleTarget | null {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    return null;
  }

  const candidate = payload as { section?: unknown; activityId?: unknown };
  if (candidate.section === "settings") {
    return { section: "settings" };
  }
  if (candidate.section !== "tasks") {
    return null;
  }
  if (candidate.activityId === undefined) {
    return { section: "tasks" };
  }
  if (
    typeof candidate.activityId !== "string" ||
    !isStandardUuid(candidate.activityId)
  ) {
    return null;
  }
  return { section: "tasks", activityId: candidate.activityId.toLowerCase() };
}

/**
 * buildConsoleTargetUrl 由主进程拼接固定本地 Console URL。
 *
 * @param consoleRootUrl 已归一化的 Console 页面地址。
 * @param target 经过校验的结构化导航目标。
 * @returns 只包含白名单 section 与合法 Activity UUID 的 URL。
 */
export function buildConsoleTargetUrl(
  consoleRootUrl: string,
  target: ConsoleTarget
): string {
  const url = new URL(consoleRootUrl);
  url.searchParams.set("section", target.section);
  if (target.section === "tasks" && target.activityId) {
    url.searchParams.set("activity", target.activityId);
  }
  return url.toString();
}
