import { afterEach, describe, expect, it, vi } from "vitest";
import {
  ActivityApiError,
  cancelActivity,
  fetchActivities,
  fetchActivityDetail
} from "./activities";

const RUNTIME_ID = "00000000-0000-4000-8000-0000000000aa";

const compact = {
  activity_id: "01890f3e-7cc2-7d00-8abc-1234567890ab",
  kind: "future_agent",
  type_label: "未来任务",
  status: "running",
  goal_summary: "整理材料",
  failure_summary: null,
  stage_code: "processing",
  stage_label: "分析与处理",
  started_at: "2026-07-18T10:00:00+08:00",
  updated_at: "2026-07-18T10:01:00+08:00",
  finished_at: null,
  can_cancel: true
} as const;

const stagePlan = [
  { stage_code: "preparing", stage_label: "准备任务" },
  { stage_code: "loading_materials", stage_label: "读取材料" },
  { stage_code: "processing", stage_label: "分析与处理" },
  { stage_code: "generating_result", stage_label: "生成结果" },
  { stage_code: "delivering_result", stage_label: "交付结果" }
] as const;

afterEach(() => vi.unstubAllGlobals());

describe("activities service", () => {
  it("校验列表和详情的公共契约", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse({
        revision: 1,
        runtime_id: RUNTIME_ID,
        active: [compact],
        recent: []
      }))
      .mockResolvedValueOnce(jsonResponse({
        ...compact,
        goal: "整理材料",
        stage_plan: stagePlan,
        stage_timeline: [],
        result_text: "",
        failure: null,
        failure_reason: null,
        work_plan: {
          revision: 2,
          items: [{
            task_id: "1",
            subject: "整理材料",
            active_form: "正在整理材料",
            status: "in_progress",
            order: 1
          }]
        }
      }));
    vi.stubGlobal("fetch", fetchMock);

    await expect(fetchActivities("http://localhost/")).resolves.toMatchObject({
      revision: 1,
      runtime_id: RUNTIME_ID
    });
    await expect(fetchActivityDetail("http://localhost", compact.activity_id)).resolves.toMatchObject({
      goal: "整理材料",
      failure: null,
      work_plan: { revision: 2, items: [{ status: "in_progress" }] }
    });
    expect(fetchMock.mock.calls[0][0]).toBe("http://localhost/api/console/activities");
  });

  it("拒绝缺少 runtime_id 的快照响应", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({
      revision: 1,
      active: [compact],
      recent: []
    })));

    await expect(fetchActivities("http://localhost")).rejects.toThrow("活动列表响应格式无效");
  });

  it("拒绝内部状态替代公共 Activity 字段的畸形响应", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({
      revision: 1,
      runtime_id: RUNTIME_ID,
      active: [{ task_id: "internal", phase: "reasoning", reasoning: "secret" }],
      recent: []
    })));

    await expect(fetchActivities("http://localhost")).rejects.toThrow("活动列表响应格式无效");
  });

  it("严格校验失败摘要与终态语义", async () => {
    const failed = {
      ...compact,
      status: "failed",
      failure_summary: "模型服务暂时不可用。",
      started_at: null,
      finished_at: "2026-07-18T10:02:00+08:00",
      can_cancel: false
    } as const;
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({
      revision: 2,
      runtime_id: RUNTIME_ID,
      active: [],
      recent: [failed]
    })));

    await expect(fetchActivities("http://localhost")).resolves.toMatchObject({
      recent: [{ failure_summary: "模型服务暂时不可用。", started_at: null }]
    });

    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({
      revision: 3,
      runtime_id: RUNTIME_ID,
      active: [{ ...compact, started_at: null }],
      recent: []
    })));
    await expect(fetchActivities("http://localhost")).rejects.toThrow(
      "活动列表响应格式无效"
    );

    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({
      revision: 4,
      runtime_id: RUNTIME_ID,
      active: [],
      recent: [{ ...failed, failure_summary: null }]
    })));
    await expect(fetchActivities("http://localhost")).rejects.toThrow(
      "活动列表响应格式无效"
    );
  });

  it("拒绝非法阶段代码与未知错误码的详情响应", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({
      ...compact,
      stage_code: "reasoning",
      goal: "整理材料",
      stage_plan: stagePlan,
      stage_timeline: [],
      result_text: "",
      failure: null,
      failure_reason: null,
      work_plan: null
    })));

    await expect(
      fetchActivityDetail("http://localhost", compact.activity_id)
    ).rejects.toThrow("活动详情响应格式无效");

    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({
      ...compact,
      goal: "整理材料",
      stage_plan: stagePlan,
      stage_timeline: [],
      result_text: "",
      failure: {
        stage_code: "processing",
        stage_label: "分析与处理",
        code: "sdk_internal_error",
        summary: "x",
        technical_detail: "",
        suggested_action: "y"
      },
      failure_reason: null,
      work_plan: null
    })));

    await expect(
      fetchActivityDetail("http://localhost", compact.activity_id)
    ).rejects.toThrow("活动详情响应格式无效");
  });

  it("严格校验详情故障报告与失败状态及摘要一致", async () => {
    const failure = {
      stage_code: "processing",
      stage_label: "分析与处理",
      code: "provider_unavailable",
      summary: "模型服务暂时不可用。",
      technical_detail: "error_type=APIConnectionError",
      suggested_action: "请稍后再试。"
    } as const;
    const failedDetail = {
      ...compact,
      status: "failed",
      failure_summary: failure.summary,
      finished_at: "2026-07-18T10:02:00+08:00",
      can_cancel: false,
      goal: "整理材料",
      stage_plan: stagePlan,
      stage_timeline: [],
      result_text: "",
      failure,
      failure_reason: null,
      work_plan: null
    } as const;

    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({
      ...failedDetail,
      failure: null
    })));
    await expect(
      fetchActivityDetail("http://localhost", compact.activity_id)
    ).rejects.toThrow("活动详情响应格式无效");

    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({
      ...failedDetail,
      failure: { ...failure, summary: "另一条摘要" }
    })));
    await expect(
      fetchActivityDetail("http://localhost", compact.activity_id)
    ).rejects.toThrow("活动详情响应格式无效");

    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({
      ...compact,
      goal: "整理材料",
      stage_plan: stagePlan,
      stage_timeline: [],
      result_text: "",
      failure,
      failure_reason: null,
      work_plan: null
    })));
    await expect(
      fetchActivityDetail("http://localhost", compact.activity_id)
    ).rejects.toThrow("活动详情响应格式无效");
  });

  it("接受后台智能体公开的稳定故障码", async () => {
    const failure = {
      stage_code: "processing",
      stage_label: "分析与处理",
      code: "agent_cli_crash",
      summary: "Claude Code 运行时意外中断，后台任务未能完成。",
      technical_detail: "",
      suggested_action: "请重新发起任务。"
    } as const;
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({
      ...compact,
      status: "failed",
      failure_summary: failure.summary,
      finished_at: "2026-07-18T10:02:00+08:00",
      can_cancel: false,
      goal: "运行后台任务",
      stage_plan: stagePlan,
      stage_timeline: [],
      result_text: "",
      failure,
      failure_reason: null,
      work_plan: null
    })));

    await expect(
      fetchActivityDetail("http://localhost", compact.activity_id)
    ).resolves.toMatchObject({ failure: { code: "agent_cli_crash" } });
  });

  it("接受 Cognitive 自主失败详情并保留具体失败原因", async () => {
    const failure = {
      stage_code: "generating_result",
      stage_label: "生成结果",
      code: "cognitive_task_failed",
      summary: "认知任务没有完成用户目标。",
      technical_detail: "",
      suggested_action: "请补充必要信息后重新发起任务。"
    } as const;
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({
      ...compact,
      kind: "cognitive",
      type_label: "事实查询",
      status: "failed",
      failure_summary: failure.summary,
      stage_code: failure.stage_code,
      stage_label: failure.stage_label,
      finished_at: "2026-07-18T10:02:00+08:00",
      can_cancel: false,
      goal: "核对最新事实",
      stage_plan: stagePlan,
      stage_timeline: [{
        stage_code: "generating_result",
        stage_label: "生成结果",
        started_at: "2026-07-18T10:01:00+08:00",
        finished_at: "2026-07-18T10:02:00+08:00"
      }],
      result_text: "",
      failure_reason: "必要网页来源无法读取。",
      failure,
      work_plan: null
    })));

    await expect(
      fetchActivityDetail("http://localhost", compact.activity_id)
    ).resolves.toMatchObject({
      status: "failed",
      failure_reason: "必要网页来源无法读取。",
      failure: { code: "cognitive_task_failed" }
    });
  });

  it("接受 Cognitive 结果协议失败且不伪造具体原因", async () => {
    const failure = {
      stage_code: "generating_result",
      stage_label: "生成结果",
      code: "cognitive_result_protocol_failed",
      summary: "认知模型连续返回了无法可靠解析的结果。",
      technical_detail: "",
      suggested_action: "请重新发起任务。"
    } as const;
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({
      ...compact,
      kind: "cognitive",
      type_label: "事实查询",
      status: "failed",
      failure_summary: failure.summary,
      stage_code: failure.stage_code,
      stage_label: failure.stage_label,
      finished_at: "2026-07-18T10:02:00+08:00",
      can_cancel: false,
      goal: "核对最新事实",
      stage_plan: stagePlan,
      stage_timeline: [],
      result_text: "",
      failure_reason: null,
      failure,
      work_plan: null
    })));

    await expect(
      fetchActivityDetail("http://localhost", compact.activity_id)
    ).resolves.toMatchObject({
      failure_reason: null,
      failure: { code: "cognitive_result_protocol_failed" }
    });
  });

  it("拒绝乱序、重复编号或未知状态的执行计划", async () => {
    const detail = {
      ...compact,
      goal: "整理材料",
      stage_plan: stagePlan,
      stage_timeline: [],
      result_text: "",
      failure: null,
      failure_reason: null
    };
    const item = {
      task_id: "1",
      subject: "理解材料",
      active_form: "正在理解材料",
      status: "pending",
      order: 1
    };

    for (const workPlan of [
      { revision: 0, items: [item] },
      { revision: 1, items: [item, { ...item, order: 2 }] },
      { revision: 1, items: [{ ...item, order: 2 }] },
      { revision: 1, items: [{ ...item, status: "thinking" }] }
    ]) {
      vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({
        ...detail,
        work_plan: workPlan
      })));
      await expect(
        fetchActivityDetail("http://localhost", compact.activity_id)
      ).rejects.toThrow("活动详情响应格式无效");
    }
  });

  it("取消使用 POST 且保留 404/409/503 状态供 UI 映射", async () => {
    const fetchMock = vi.fn(async () => jsonResponse({ detail: "activity state conflict" }, 409));
    vi.stubGlobal("fetch", fetchMock);

    await expect(cancelActivity("http://localhost", compact.activity_id)).rejects.toMatchObject({
      name: "ActivityApiError",
      status: 409,
      message: "任务状态已经变化，当前不能取消。"
    } satisfies Partial<ActivityApiError>);
    expect(fetchMock).toHaveBeenCalledWith(
      `http://localhost/api/activities/${compact.activity_id}/cancel`,
      { method: "POST" }
    );
  });
});

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json" }
  });
}
