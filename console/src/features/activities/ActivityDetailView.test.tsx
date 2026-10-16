// @vitest-environment jsdom

import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { ActivityDetail } from "../../api/contracts/activities";
import { ActivityDetailView } from "./ActivityDetailView";

function makeActivity(overrides: Partial<ActivityDetail>): ActivityDetail {
  return {
    activity_id: "123e4567-e89b-42d3-a456-426614174000",
    kind: "task_result",
    type_label: "任务结果",
    status: "completed",
    goal_summary: "总结报告",
    failure_summary: null,
    stage_code: "delivering_result",
    stage_label: "交付结果",
    started_at: "2026-07-22T10:00:00+08:00",
    updated_at: "2026-07-22T10:00:01+08:00",
    finished_at: "2026-07-22T10:00:01+08:00",
    can_cancel: false,
    goal: "总结报告",
    stage_plan: [
      { stage_code: "preparing", stage_label: "准备任务" },
      { stage_code: "loading_materials", stage_label: "读取材料" },
      { stage_code: "processing", stage_label: "分析与处理" },
      { stage_code: "generating_result", stage_label: "生成结果" },
      { stage_code: "delivering_result", stage_label: "交付结果" }
    ],
    stage_timeline: [
      {
        stage_code: "preparing",
        stage_label: "准备任务",
        started_at: "2026-07-22T10:00:00+08:00",
        finished_at: "2026-07-22T10:00:01+08:00"
      }
    ],
    result_text: "结果正文",
    failure: null,
    failure_reason: null,
    work_plan: null,
    turn_outputs: [],
    ...overrides
  };
}

const NOW = new Date("2026-07-22T10:00:02+08:00");

describe("ActivityDetailView", () => {
  it("目标和结果把真实 URL 显示为截短文字并保留完整点击目标", () => {
    const goalUrl = "https://example.com/reports/2026/very-long-report-path?source=console&view=goal";
    const resultUrl = "https://example.com/results/2026/another-long-result-path?source=console&view=result";
    const activity = makeActivity({
      goal: `总结 ${goalUrl}`,
      result_text: `结果见 ${resultUrl}`
    });

    render(
      <ActivityDetailView
        activity={activity}
        cancelling={false}
        now={NOW}
        onBack={vi.fn()}
        onCancel={vi.fn()}
      />
    );

    const links = screen.getAllByRole("link") as HTMLAnchorElement[];
    expect(links.map(link => link.href)).toEqual([goalUrl, resultUrl]);
    expect(links.every(link => link.textContent?.includes("…"))).toBe(true);
    expect(links.every(link => link.target === "_blank")).toBe(true);
  });

  it("链接边界保留 URL 自身括号并排除外部反引号", () => {
    const pairedUrl = "https://en.wikipedia.org/wiki/Foo_(bar)";
    const activity = makeActivity({
      goal: `查看 ${pairedUrl}`,
      result_text: "再看 https://example.com/report`后续"
    });

    render(
      <ActivityDetailView
        activity={activity}
        cancelling={false}
        now={NOW}
        onBack={vi.fn()}
        onCancel={vi.fn()}
      />
    );

    const links = screen.getAllByRole("link") as HTMLAnchorElement[];
    expect(links.map(link => link.href)).toEqual([
      pairedUrl,
      "https://example.com/report"
    ]);
  });

  it("失败终态展示结构化故障报告与复制按钮", () => {
    const activity = makeActivity({
      status: "failed",
      result_text: "",
      stage_code: "processing",
      stage_label: "分析与处理",
      failure_summary: "模型服务当前请求过多，暂时无法处理。",
      failure: {
        stage_code: "processing",
        stage_label: "分析与处理",
        code: "rate_limited",
        summary: "模型服务当前请求过多，暂时无法处理。",
        technical_detail: "error_type=RateLimitError; http_status=429",
        suggested_action: "请稍等片刻后重新发起任务。"
      }
    });

    const { container } = render(
      <ActivityDetailView
        activity={activity}
        cancelling={false}
        now={NOW}
        onBack={vi.fn()}
        onCancel={vi.fn()}
      />
    );

    expect(screen.getByText("故障报告")).toBeTruthy();
    expect(screen.getByText("rate_limited")).toBeTruthy();
    expect(screen.getByText("模型服务当前请求过多，暂时无法处理。")).toBeTruthy();
    expect(screen.getByText("请稍等片刻后重新发起任务。")).toBeTruthy();
    expect(screen.getByText("复制故障详情")).toBeTruthy();
    expect(
      [...container.querySelectorAll("dt, dd")].every(item => item.closest("dl") !== null)
    ).toBe(true);
  });

  it("Cognitive 自主失败展示实际阶段与具体失败原因", () => {
    const activity = makeActivity({
      kind: "cognitive",
      type_label: "事实查询",
      status: "failed",
      result_text: "",
      stage_code: "generating_result",
      stage_label: "生成结果",
      failure_summary: "认知任务没有完成用户目标。",
      stage_timeline: [
        {
          stage_code: "preparing",
          stage_label: "准备任务",
          started_at: "2026-07-22T10:00:00+08:00",
          finished_at: "2026-07-22T10:00:01+08:00"
        },
        {
          stage_code: "generating_result",
          stage_label: "生成结果",
          started_at: "2026-07-22T10:00:01+08:00",
          finished_at: "2026-07-22T10:00:02+08:00"
        }
      ],
      failure_reason: "必要网页来源无法读取。",
      failure: {
        stage_code: "generating_result",
        stage_label: "生成结果",
        code: "cognitive_task_failed",
        summary: "认知任务没有完成用户目标。",
        technical_detail: "",
        suggested_action: "请补充必要信息后重新发起任务。"
      }
    });

    render(
      <ActivityDetailView
        activity={activity}
        cancelling={false}
        now={NOW}
        onBack={vi.fn()}
        onCancel={vi.fn()}
      />
    );

    expect(screen.getByText("cognitive_task_failed")).toBeTruthy();
    expect(screen.getByText("具体失败原因")).toBeTruthy();
    expect(screen.getByText("必要网页来源无法读取。")).toBeTruthy();
    expect(screen.getAllByText("生成结果").length).toBeGreaterThan(0);
  });

  it("运行中的真实执行计划默认展开并展示当前动作", () => {
    const activity = makeActivity({
      status: "running",
      finished_at: null,
      can_cancel: true,
      result_text: "",
      work_plan: {
        revision: 3,
        items: [
          {
            task_id: "1",
            subject: "理解参考项目",
            active_form: "正在理解参考项目",
            status: "completed",
            order: 1
          },
          {
            task_id: "2",
            subject: "核对完成链路",
            active_form: "正在核对完成链路",
            status: "in_progress",
            order: 2
          },
          {
            task_id: "3",
            subject: "整理建议",
            active_form: "正在整理建议",
            status: "pending",
            order: 3
          }
        ]
      }
    });

    const { container } = render(
      <ActivityDetailView
        activity={activity}
        cancelling={false}
        now={NOW}
        onBack={vi.fn()}
        onCancel={vi.fn()}
      />
    );

    expect(container.querySelector("details")?.open).toBe(true);
    expect(screen.getByText("1/3")).toBeTruthy();
    expect(screen.getByText("正在核对完成链路")).toBeTruthy();
    expect(screen.queryByText("核对完成链路")).toBeNull();
  });

  it("终态计划默认折叠但保留完成摘要", () => {
    const activity = makeActivity({
      work_plan: {
        revision: 4,
        items: [
          {
            task_id: "1",
            subject: "整理结果",
            active_form: "正在整理结果",
            status: "completed",
            order: 1
          }
        ]
      }
    });

    const { container } = render(
      <ActivityDetailView
        activity={activity}
        cancelling={false}
        now={NOW}
        onBack={vi.fn()}
        onCancel={vi.fn()}
      />
    );

    expect(container.querySelector("details")?.open).toBe(false);
    expect(screen.getByText("1/1")).toBeTruthy();
  });

  it("按 Turn 顺序显示纯文本输出并保留换行", () => {
    const activity = makeActivity({
      turn_outputs: [
        {
          turn_index: 1,
          content_text: "第一轮\n继续",
          created_at: "2026-07-22T10:00:00+08:00"
        },
        {
          turn_index: 2,
          content_text: "第二轮",
          created_at: "2026-07-22T10:00:01+08:00"
        }
      ]
    });

    render(
      <ActivityDetailView
        activity={activity}
        cancelling={false}
        now={NOW}
        onBack={vi.fn()}
        onCancel={vi.fn()}
      />
    );

    expect(screen.getByText("智能体运行详情")).toBeTruthy();
    const turnSection = screen.getByText("智能体运行详情").closest("section");
    const scrollContainer = turnSection?.querySelector("[aria-label='智能体运行详情列表']");
    expect(scrollContainer?.className).toContain("overflow-y-auto");
    expect(scrollContainer?.className).toContain("max-h-[min(32rem,60vh)]");
    expect(scrollContainer?.querySelector("div.divide-y")?.className).toBe("divide-y");
    expect([...turnSection?.querySelectorAll("p") ?? []].map(node => node.textContent)).toEqual([
      "第一轮\n继续",
      "第二轮"
    ]);
  });
});
