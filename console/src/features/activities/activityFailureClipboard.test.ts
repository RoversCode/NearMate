import { describe, expect, it } from "vitest";
import type { ActivityDetail } from "../../api/contracts/activities";
import { formatActivityFailureClipboard } from "./activityFailureClipboard";

const activity: ActivityDetail = {
  activity_id: "123e4567-e89b-42d3-a456-426614174000",
  kind: "task_result",
  type_label: "任务结果",
  status: "failed",
  goal_summary: "整理用户材料",
  failure_summary: "模型服务当前请求过多，暂时无法处理。",
  stage_code: "processing",
  stage_label: "分析与处理",
  started_at: "2026-07-22T10:00:00+08:00",
  updated_at: "2026-07-22T10:05:00+08:00",
  finished_at: "2026-07-22T10:05:00+08:00",
  can_cancel: false,
  goal: "整理用户材料并输出周报，包含 https://example.com/report 的数据",
  stage_plan: [
    { stage_code: "preparing", stage_label: "准备任务" },
    { stage_code: "loading_materials", stage_label: "读取材料" },
    { stage_code: "processing", stage_label: "分析与处理" },
    { stage_code: "generating_result", stage_label: "生成结果" },
    { stage_code: "delivering_result", stage_label: "交付结果" }
  ],
  stage_timeline: [],
  result_text: "",
  failure_reason: null,
  work_plan: null,
  turn_outputs: [],
  failure: {
    stage_code: "processing",
    stage_label: "分析与处理",
    code: "rate_limited",
    summary: "模型服务当前请求过多，暂时无法处理。",
    technical_detail: "error_type=RateLimitError; http_status=429; provider_request_id=req-1",
    suggested_action: "请稍等片刻后重新发起任务。"
  }
};

describe("formatActivityFailureClipboard", () => {
  it("复制文本固定包含任务身份、故障报告与时间", () => {
    const text = formatActivityFailureClipboard(activity);
    const lines = text.split("\n");

    expect(lines[0]).toBe(`任务编号：${activity.activity_id}`);
    expect(text).toContain("任务类型：任务结果");
    expect(text).toContain(`完整目标：${activity.goal}`);
    expect(text).toContain("当前状态：失败");
    expect(text).toContain("失败阶段：分析与处理（processing）");
    expect(text).toContain("故障代码：rate_limited");
    expect(text).toContain("原因摘要：模型服务当前请求过多，暂时无法处理。");
    expect(text).toContain(
      "安全技术详情：error_type=RateLimitError; http_status=429; provider_request_id=req-1"
    );
    expect(text).toContain("处理建议：请稍等片刻后重新发起任务。");
    expect(text).toContain("开始时间：");
    expect(text).toContain("失败时间：");
    expect(lines).toHaveLength(11);
  });

  it("复制文本不包含隐藏内部字段", () => {
    const text = formatActivityFailureClipboard(activity);

    expect(text).not.toContain("stage_plan");
    expect(text).not.toContain("stage_timeline");
    expect(text).not.toContain("reasoning");
    expect(text).not.toContain("result_text");
    expect(text).not.toContain("can_cancel");
  });

  it("Cognitive 自主失败复制具体失败原因", () => {
    const text = formatActivityFailureClipboard({
      ...activity,
      kind: "cognitive",
      type_label: "事实查询",
      failure_reason: "必要网页来源无法读取。",
      failure: {
        ...activity.failure!,
        code: "cognitive_task_failed",
        summary: "认知任务没有完成用户目标。",
        suggested_action: "请补充必要信息后重新发起任务。"
      }
    });

    expect(text).toContain("具体失败原因：必要网页来源无法读取。");
  });

  it("没有故障报告时仍提供任务身份与占位", () => {
    const text = formatActivityFailureClipboard({
      ...activity,
      status: "completed",
      failure_summary: null,
      result_text: "正文",
      failure: null
    });

    expect(text).toContain("故障代码：—");
    expect(text).toContain("原因摘要：—");
    expect(text).toContain(`任务编号：${activity.activity_id}`);
  });
});
