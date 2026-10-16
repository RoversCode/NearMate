export type UsageRangePreset = "today" | "last7Days" | "last30Days" | "custom";

export type UsageDateRange = {
  fromDate: string;
  toDate: string;
};

/**
 * 按 Backend 同机本地日期生成预设范围。
 *
 * Args:
 *   preset: 今天、近 7 天或近 30 天。
 *   now: 当前本地时间；测试可注入固定时间。
 *
 * Returns:
 *   包含首尾两天的日期输入值。
 */
export function presetUsageDateRange(
  preset: Exclude<UsageRangePreset, "custom">,
  now: Date = new Date()
): UsageDateRange {
  const dayCount = preset === "today" ? 1 : preset === "last7Days" ? 7 : 30;
  const start = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  start.setDate(start.getDate() - dayCount + 1);
  return {
    fromDate: formatLocalDate(start),
    toDate: formatLocalDate(now)
  };
}

/**
 * 校验日期输入并生成 API 直接使用的固化日期范围。
 *
 * Args:
 *   range: 用户选择的首日和末日。
 *
 * Returns:
 *   已校验且包含首尾两天的日期字符串。
 *
 * Throws:
 *   日期格式非法或结束日早于开始日时抛出错误。
 */
export function validateUsageDateRange(range: UsageDateRange): UsageDateRange {
  parseLocalDate(range.fromDate);
  parseLocalDate(range.toDate);
  if (range.toDate < range.fromDate) {
    throw new Error("结束日期不能早于开始日期");
  }
  return { ...range };
}

/** 将 Date 格式化为本地日期输入使用的 YYYY-MM-DD。 */
export function formatLocalDate(value: Date): string {
  const year = value.getFullYear();
  const month = String(value.getMonth() + 1).padStart(2, "0");
  const day = String(value.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

/** 返回当前时刻到下一个本地自然日的毫秒数。 */
export function millisecondsUntilNextLocalDate(now: Date = new Date()): number {
  const nextDate = new Date(now.getFullYear(), now.getMonth(), now.getDate() + 1);
  return Math.max(1, nextDate.getTime() - now.getTime());
}

/** 把日期转换成不受浏览器当前时区影响的日历序号。 */
export function usageDateOrdinal(value: string): number {
  const parsed = parseLocalDate(value);
  return Math.floor(
    Date.UTC(parsed.getFullYear(), parsed.getMonth(), parsed.getDate()) /
      (24 * 60 * 60 * 1000)
  );
}

/** 按本地时区严格解析 YYYY-MM-DD，避免浏览器把日期当成 UTC。 */
function parseLocalDate(value: string): Date {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
  if (match === null) {
    throw new Error("日期格式必须是 YYYY-MM-DD");
  }
  const parsed = new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3]));
  if (formatLocalDate(parsed) !== value) {
    throw new Error("日期不存在");
  }
  return parsed;
}
