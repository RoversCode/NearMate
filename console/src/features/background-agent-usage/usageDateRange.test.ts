import { describe, expect, it } from "vitest";

import {
  millisecondsUntilNextLocalDate,
  presetUsageDateRange,
  usageDateOrdinal,
  validateUsageDateRange
} from "./usageDateRange";

describe("background agent usage date range", () => {
  it("默认今天并直接生成包含首尾的日期范围", () => {
    const now = new Date(2026, 7, 2, 15, 30);

    const range = presetUsageDateRange("today", now);

    expect(range).toEqual({ fromDate: "2026-08-02", toDate: "2026-08-02" });
    expect(validateUsageDateRange(range)).toEqual(range);
  });

  it("近 7 天和近 30 天都包含今天", () => {
    const now = new Date(2026, 7, 2, 15, 30);

    expect(presetUsageDateRange("last7Days", now)).toEqual({
      fromDate: "2026-07-27",
      toDate: "2026-08-02"
    });
    expect(presetUsageDateRange("last30Days", now)).toEqual({
      fromDate: "2026-07-04",
      toDate: "2026-08-02"
    });
  });

  it("拒绝倒置的自定义日期", () => {
    expect(() =>
      validateUsageDateRange({ fromDate: "2026-08-03", toDate: "2026-08-02" })
    ).toThrow("结束日期不能早于开始日期");
  });

  it("计算午夜刷新延迟和不受当前时区影响的日期间距", () => {
    const now = new Date(2026, 7, 2, 23, 59, 59, 500);

    expect(millisecondsUntilNextLocalDate(now)).toBe(500);
    expect(usageDateOrdinal("2026-08-03") - usageDateOrdinal("2026-08-01")).toBe(2);
  });
});
