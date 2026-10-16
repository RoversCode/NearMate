import { useCallback, useEffect, useState } from "react";
import type { ConsoleSectionId, TaskView } from "./consoleTypes";
import { isStandardUuid } from "../lib/uuid";

type ConsoleNavigationState = {
  sectionId: ConsoleSectionId;
  taskView: TaskView;
  activityId: string | null;
};

const TASK_VIEWS = new Set<TaskView>(["activities", "reminders"]);

const SECTION_IDS = new Set<ConsoleSectionId>([
  "settings",
  "tasks",
  "skills",
  "usage",
  "character-cards",
  "streamvox",
  "logs"
]);

/**
 * 解析 Console URL 导航状态。
 *
 * Args:
 *   search: 当前地址的查询字符串。
 *
 * Returns:
 *   已归一化的页面与可选 Activity UUID；非法值回退到设置页或任务列表。
 */
export function parseConsoleNavigation(search: string): ConsoleNavigationState {
  const params = new URLSearchParams(search);
  const section = params.get("section");
  const sectionId = SECTION_IDS.has(section as ConsoleSectionId)
    ? section as ConsoleSectionId
    : "settings";
  const activityId = params.get("activity");
  const requestedTaskView = params.get("taskView");
  const taskView = TASK_VIEWS.has(requestedTaskView as TaskView)
    ? requestedTaskView as TaskView
    : "activities";

  if (
    sectionId !== "tasks" ||
    taskView !== "activities" ||
    activityId === null ||
    !isStandardUuid(activityId)
  ) {
    return { sectionId, taskView, activityId: null };
  }

  return { sectionId, taskView, activityId: activityId.toLowerCase() };
}

/**
 * 将导航状态写成唯一规范查询字符串。
 *
 * Args:
 *   state: 已归一化的 Console 导航状态。
 *
 * Returns:
 *   只含 section 和合法 Activity UUID 的查询字符串。
 */
export function serializeConsoleNavigation(state: ConsoleNavigationState): string {
  const params = new URLSearchParams({ section: state.sectionId });
  if (state.sectionId === "tasks") {
    params.set("taskView", state.taskView);
  }
  if (
    state.sectionId === "tasks"
    && state.taskView === "activities"
    && state.activityId !== null
  ) {
    params.set("activity", state.activityId);
  }
  return `?${params.toString()}`;
}

/**
 * 管理 Console 的轻量 URL 导航，不引入完整 Router。
 *
 * Returns:
 *   当前导航状态，以及会写入 history 的页面和 Activity 导航命令。
 */
export function useConsoleNavigation() {
  const [state, setState] = useState<ConsoleNavigationState>(() =>
    parseConsoleNavigation(window.location.search)
  );

  useEffect(() => {
    const applyLocation = (): void => {
      const nextState = parseConsoleNavigation(window.location.search);
      const canonicalSearch = serializeConsoleNavigation(nextState);
      if (window.location.search !== canonicalSearch) {
        window.history.replaceState(null, "", `${window.location.pathname}${canonicalSearch}`);
      }
      setState(nextState);
    };

    applyLocation();
    window.addEventListener("popstate", applyLocation);
    return () => window.removeEventListener("popstate", applyLocation);
  }, []);

  const navigate = useCallback((
    sectionId: ConsoleSectionId,
    activityId: string | null = null,
    taskView: TaskView = "activities"
  ) => {
    const nextState = parseConsoleNavigation(
      serializeConsoleNavigation({ sectionId, taskView, activityId })
    );
    window.history.pushState(
      null,
      "",
      `${window.location.pathname}${serializeConsoleNavigation(nextState)}`
    );
    setState(nextState);
  }, []);

  return { ...state, navigate };
}
