import { useEffect, useState } from "react";

import { formatLocalDate, millisecondsUntilNextLocalDate } from "./usageDateRange";

/**
 * 跟踪当前本地日期，并在跨午夜或窗口恢复时重新校准。
 *
 * Returns:
 *   当前设备本地日期的 YYYY-MM-DD 字符串。
 */
export function useCurrentLocalDate(): string {
  const [currentDate, setCurrentDate] = useState(() => formatLocalDate(new Date()));

  useEffect(() => {
    let timeoutId: number | null = null;

    const refreshAndSchedule = (): void => {
      const now = new Date();
      setCurrentDate(formatLocalDate(now));
      if (timeoutId !== null) {
        window.clearTimeout(timeoutId);
      }
      timeoutId = window.setTimeout(refreshAndSchedule, millisecondsUntilNextLocalDate(now));
    };
    const refreshWhenVisible = (): void => {
      if (document.visibilityState === "visible") {
        refreshAndSchedule();
      }
    };

    refreshAndSchedule();
    window.addEventListener("focus", refreshAndSchedule);
    document.addEventListener("visibilitychange", refreshWhenVisible);
    return () => {
      if (timeoutId !== null) {
        window.clearTimeout(timeoutId);
      }
      window.removeEventListener("focus", refreshAndSchedule);
      document.removeEventListener("visibilitychange", refreshWhenVisible);
    };
  }, []);

  return currentDate;
}
