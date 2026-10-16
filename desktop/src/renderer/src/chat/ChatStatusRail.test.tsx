// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ActivitySnapshot, TimedReminderSnapshot } from "../types/backendEvents";
import { ChatStatusRail } from "./ChatStatusRail";
import { ChatVoiceOrb } from "./ChatVoiceOrb";
import type { ChatRuntime } from "./useChatRuntime";
import type { LlmAccountStatusSnapshot } from "./llmAccountStatus";

type Workspace = ChatRuntime["workspace"];

afterEach(() => {
  cleanup();
});

const runtimeId = "runtime-1";
const activityId = "activity-1";
const scheduledReminderId = "reminder-scheduled";
const ringingReminderId = "reminder-ringing";

const accountStatus: LlmAccountStatusSnapshot = {
  providers: [{
    provider_id: "deepseek",
    display_name: "DeepSeek",
    active: true,
    status: "available",
    balance: "12.50",
    currency: "CNY",
    fetched_at: "2026-09-25T05:00:00Z",
    message: null
  }]
};

function activitySnapshot(): ActivitySnapshot {
  return {
    revision: 1,
    runtime_id: runtimeId,
    active: [{
      activity_id: activityId,
      kind: "background_agent",
      type_label: "后台任务",
      status: "running",
      goal_summary: "整理本周会议纪要",
      failure_summary: null,
      stage_code: "processing",
      stage_label: "处理中",
      started_at: "2026-09-24T10:00:00Z",
      updated_at: "2026-09-24T10:01:00Z",
      finished_at: null,
      can_cancel: true
    }],
    recent: []
  };
}

function reminderSnapshot(): TimedReminderSnapshot {
  return {
    revision: 2,
    runtime_id: runtimeId,
    active: [
      {
        reminder_id: scheduledReminderId,
        content: "下午三点开会",
        state: "scheduled",
        trigger_at: "2026-09-24T15:00:00Z",
        created_at: "2026-09-24T09:00:00Z",
        updated_at: "2026-09-24T09:00:00Z",
        finished_at: null,
        ring_count: 0,
        next_ring_at: "2026-09-24T15:00:00Z",
        finish_reason: null,
        can_modify: true,
        can_cancel: true,
        can_dismiss: false
      },
      {
        reminder_id: ringingReminderId,
        content: "喝水",
        state: "ringing",
        trigger_at: "2026-09-24T09:30:00Z",
        created_at: "2026-09-24T09:00:00Z",
        updated_at: "2026-09-24T09:30:00Z",
        finished_at: null,
        ring_count: 1,
        next_ring_at: null,
        finish_reason: null,
        can_modify: false,
        can_cancel: false,
        can_dismiss: true
      }
    ],
    recent: []
  };
}

function workspace(overrides: Partial<Workspace> = {}): Workspace {
  return {
    activitySnapshot: activitySnapshot(),
    reminderSnapshot: reminderSnapshot(),
    requestingActivityIds: new Set(),
    requestingReminderIds: new Set(),
    errors: { activity: null, reminder: null },
    runtimeRestartMessage: null,
    enabledSkills: [{ id: "web.search", name: "网页搜索", ready: true, usable: true }],
    requestCancelActivity: vi.fn(async () => undefined),
    requestUpdateReminder: vi.fn(async () => undefined),
    requestCancelReminder: vi.fn(async () => undefined),
    requestDismissReminders: vi.fn(async () => undefined),
    openActivityDetail: vi.fn(),
    ...overrides
  };
}

describe("ChatVoiceOrb", () => {
  it.each([
    ["ready", "状态同步中"],
    ["listening", "正在听"],
    ["thinking", "思考中"],
    ["responding", "正在回复"],
    ["error", "暂时无法回复"]
  ] as const)("呈现 %s 语音状态且不承担点击控制", (state, label) => {
    const { container } = render(<ChatVoiceOrb state={state} companionName="火花" />);
    expect(screen.getByText("火花")).toBeTruthy();
    expect(screen.getByText(label)).toBeTruthy();
    expect(container.querySelector(`.chat-voice-orb--${state === "ready" ? "syncing" : state}`)).toBeTruthy();
    expect(container.querySelector("button")).toBeNull();
  });
});

describe("ChatStatusRail", () => {
  it("常驻角色状态并展示活动任务、提醒和已启用 Skill", () => {
    render(<ChatStatusRail companionName="火花" voiceState="ready" workspace={workspace()} llmAccountStatus={accountStatus} />);

    expect(screen.getByRole("complementary", { name: "当前状态" })).toBeTruthy();
    expect(screen.getByText("火花")).toBeTruthy();
    expect(screen.getByText("整理本周会议纪要")).toBeTruthy();
    expect(screen.getByText("下午三点开会")).toBeTruthy();
    expect(screen.getByText("喝水")).toBeTruthy();
    expect(screen.getByText("网页搜索")).toBeTruthy();
    expect(screen.getByText("知道了")).toBeTruthy();
  });

  it("显示当前对话厂商和账户余额", () => {
    render(<ChatStatusRail companionName="火花" voiceState="ready" workspace={workspace()} llmAccountStatus={accountStatus} />);

    expect(screen.getByText("模型账户余额")).toBeTruthy();
    expect(screen.getByText("DeepSeek")).toBeTruthy();
    expect(screen.getByText("当前对话")).toBeTruthy();
    expect(screen.getByText("余额 12.50 CNY")).toBeTruthy();
  });

  it("尚未收到余额时显示加载态，不把未知账户数显示成零", () => {
    render(<ChatStatusRail companionName="火花" voiceState="ready" workspace={workspace()} />);

    const group = screen.getByText("模型账户余额").closest("section")!;
    expect(within(group).getByText("正在读取余额…")).toBeTruthy();
    expect(within(group).queryByText("0")).toBeNull();
    expect(within(group).queryByText("暂无已配置的模型账户")).toBeNull();
  });

  it("余额读取失败时显示错误并允许重试", () => {
    const refresh = vi.fn(async () => undefined);
    render(<ChatStatusRail
      companionName="火花"
      voiceState="ready"
      workspace={workspace({
        llmAccountStatusError: "余额读取失败，请重试",
        refreshLlmAccountStatus: refresh
      })}
    />);

    expect(screen.getByText("余额读取失败，请重试")).toBeTruthy();
    expect(screen.queryByText("暂无已配置的模型账户")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "重新读取余额" }));
    expect(refresh).toHaveBeenCalledOnce();
  });

  it("厂商余额查询失败时保留账户状态，并提供一次整组重试", () => {
    const refresh = vi.fn(async () => undefined);
    const failedProvider = {
      ...accountStatus.providers[0],
      status: "error" as const,
      balance: null,
      currency: null,
      message: "余额服务暂时不可用"
    };
    render(<ChatStatusRail
      companionName="火花"
      voiceState="ready"
      workspace={workspace({ refreshLlmAccountStatus: refresh })}
      llmAccountStatus={{ providers: [
        failedProvider,
        { ...failedProvider, provider_id: "qwen", display_name: "Qwen", active: false }
      ] }}
    />);

    expect(screen.getByText("DeepSeek")).toBeTruthy();
    expect(screen.getByText("Qwen")).toBeTruthy();
    expect(screen.getAllByText("余额服务暂时不可用")).toHaveLength(2);
    const retryButtons = screen.getAllByRole("button", { name: "重新读取余额" });
    expect(retryButtons).toHaveLength(1);
    fireEvent.click(retryButtons[0]);
    expect(refresh).toHaveBeenCalledOnce();
  });

  it.each(["available", "unsupported", "not_configured"] as const)(
    "厂商状态为 %s 时不显示错误重试入口",
    status => {
      render(<ChatStatusRail
        companionName="火花"
        voiceState="ready"
        workspace={workspace({ refreshLlmAccountStatus: vi.fn(async () => undefined) })}
        llmAccountStatus={{ providers: [{ ...accountStatus.providers[0], status }] }}
      />);

      expect(screen.queryByRole("button", { name: "重新读取余额" })).toBeNull();
    }
  );

  it("成功读到空账户列表后才展示未配置提示", () => {
    render(<ChatStatusRail
      companionName="火花"
      voiceState="ready"
      workspace={workspace()}
      llmAccountStatus={{ providers: [] }}
    />);

    expect(screen.getByText("暂无已配置的模型账户")).toBeTruthy();
    expect(screen.queryByText("正在读取余额…")).toBeNull();
  });

  it("活动任务取消和详情按钮沿用现有操作回调", () => {
    const cancel = vi.fn(async () => undefined);
    const detail = vi.fn();
    render(<ChatStatusRail
      companionName="火花"
      voiceState="thinking"
      workspace={workspace({ requestCancelActivity: cancel, openActivityDetail: detail })}
    />);

    const activityCard = screen.getByText("整理本周会议纪要").closest("article")!;
    fireEvent.click(within(activityCard).getByRole("button", { name: "取消" }));
    fireEvent.click(within(activityCard).getByRole("button", { name: "详情" }));
    expect(cancel).toHaveBeenCalledWith(activityId);
    expect(detail).toHaveBeenCalledWith(activityId);
  });

  it("提醒知道了会把当前响铃批次作为整体提交", () => {
    const dismiss = vi.fn(async () => undefined);
    render(<ChatStatusRail
      companionName="火花"
      voiceState="listening"
      workspace={workspace({ requestDismissReminders: dismiss })}
    />);

    fireEvent.click(screen.getByRole("button", { name: "知道了" }));
    expect(dismiss).toHaveBeenCalledWith(runtimeId, [ringingReminderId]);
  });

  it("提醒修改只提交真正变更的字段，取消仍走原有回调", async () => {
    const update = vi.fn(async () => undefined);
    const cancel = vi.fn(async () => undefined);
    render(<ChatStatusRail
      companionName="火花"
      voiceState="ready"
      workspace={workspace({ requestUpdateReminder: update, requestCancelReminder: cancel })}
    />);

    fireEvent.click(screen.getByRole("button", { name: "修改" }));
    const contentInput = screen.getByLabelText("内容");
    fireEvent.change(contentInput, { target: { value: "下午四点开会" } });
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    await vi.waitFor(() => expect(update).toHaveBeenCalledWith(
      scheduledReminderId,
      { content: "下午四点开会" }
    ));

    // 操作回调是异步的，测试先结束编辑态再验证取消按钮仍在同一张卡片上。
    fireEvent.click(screen.getByRole("button", { name: "放弃" }));
    const reminderCard = screen.getByText("下午三点开会").closest("article")!;
    fireEvent.click(within(reminderCard).getByRole("button", { name: "取消" }));
    expect(cancel).toHaveBeenCalledWith(scheduledReminderId);
  });

  it("没有动态数据时保留空态和角色状态区", () => {
    render(<ChatStatusRail
      companionName="火花"
      voiceState="error"
      workspace={workspace({
        activitySnapshot: { revision: 0, runtime_id: runtimeId, active: [], recent: [] },
        reminderSnapshot: { revision: 0, runtime_id: runtimeId, active: [], recent: [] },
        enabledSkills: []
      })}
    />);

    expect(screen.getByText("暂时无法回复")).toBeTruthy();
    expect(screen.getByText("当前没有活动任务")).toBeTruthy();
    expect(screen.getByText("当前没有待处理提醒")).toBeTruthy();
    expect(screen.getByText("暂无已启用 Skill")).toBeTruthy();
  });
});
