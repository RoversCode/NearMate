// @vitest-environment jsdom

import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { BackgroundAgentUsageByDay } from "@/api/contracts/backgroundAgentUsage";
import { UsageDailyTrend } from "./UsageDailyTrend";


describe("usage daily trend", () => {
  it("按真实日期定位并且不跨缺失日期连线", () => {
    const view = render(
      <UsageDailyTrend
        fromDate="2026-08-01"
        rows={[
          usageRow("2026-08-01", 100),
          usageRow("2026-08-02", 80),
          usageRow("2026-08-04", 60)
        ]}
        toDate="2026-08-04"
      />
    );

    const polylines = view.container.querySelectorAll("polyline");
    const circles = view.container.querySelectorAll("circle.fill-primary");

    expect(polylines).toHaveLength(2);
    expect(polylines[0].getAttribute("points")?.split(" ")).toHaveLength(2);
    expect(Number(circles[2].getAttribute("cx"))).toBeGreaterThan(
      Number(circles[1].getAttribute("cx"))
    );
  });

  it("单日统计继续显示输入和输出数据点", () => {
    const view = render(
      <UsageDailyTrend
        fromDate="2026-08-02"
        rows={[usageRow("2026-08-02", 100)]}
        toDate="2026-08-02"
      />
    );

    expect(view.container.querySelectorAll("polyline")).toHaveLength(0);
    expect(view.container.querySelectorAll("circle")).toHaveLength(2);
  });
});


function usageRow(date: string, totalInputTokens: number): BackgroundAgentUsageByDay {
  // 固定其它统计字段，让测试只关注日期间距和连线行为。

  return {
    date,
    task_count: 1,
    complete_task_count: 1,
    incomplete_task_count: 0,
    non_cached_input_tokens: totalInputTokens,
    cache_creation_input_tokens: 0,
    cache_read_input_tokens: 0,
    total_input_tokens: totalInputTokens,
    output_tokens: 10,
    cache_hit_tokens: 0,
    cache_hit_rate: 0,
    reference_cost_usd: null,
    reference_cost_task_count: 0
  };
}
