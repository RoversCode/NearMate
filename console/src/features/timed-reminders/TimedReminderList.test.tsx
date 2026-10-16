// @vitest-environment jsdom

import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { TimedReminderSnapshot } from "../../api/contracts/timedReminders";
import { TimedReminderList } from "./TimedReminderList";

const REMINDER_ID = "123e4567-e89b-42d3-a456-426614174010";

describe("TimedReminderList", () => {
  it("显示共享时钟倒计时，并把行内修改和取消路由到提醒编号", async () => {
    const onUpdate = vi.fn(async () => true);
    const onCancel = vi.fn();
    const onDismiss = vi.fn();
    render(
      <TimedReminderList
        now={new Date("2026-09-04T14:30:00+08:00")}
        onCancel={onCancel}
        onDismiss={onDismiss}
        onUpdate={onUpdate}
        requestingIds={new Set()}
        snapshot={scheduledSnapshot()}
      />
    );

    expect(screen.getByText("00:30:00")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "修改" }));
    const contentInput = screen.getByLabelText("提醒内容");
    expect(contentInput.hasAttribute("maxlength")).toBe(false);
    fireEvent.change(contentInput, {
      target: { value: "😀".repeat(501) }
    });
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    expect(screen.getByText("提醒内容不能超过五百个字符。")).toBeTruthy();
    expect(onUpdate).not.toHaveBeenCalled();

    fireEvent.change(screen.getByLabelText("提醒内容"), {
      target: { value: "检查摄像头" }
    });
    fireEvent.change(screen.getByLabelText("提醒时间"), {
      target: { value: "2026-09-04T15:30:45" }
    });
    fireEvent.click(screen.getByRole("button", { name: "保存" }));

    await waitFor(() => expect(onUpdate).toHaveBeenCalledWith(REMINDER_ID, {
      content: "检查摄像头",
      triggerAt: expect.stringMatching(/^2026-09-04T/)
    }));
    const row = (await screen.findByText("准备面试")).closest("tr") as HTMLTableRowElement;
    fireEvent.click(within(row).getByRole("button", { name: "取消" }));
    expect(onCancel).toHaveBeenCalledWith(REMINDER_ID);
  });

  it("把当前运行期全部响铃项作为一个批次关闭，并在请求中禁用重复提交", () => {
    const onDismiss = vi.fn();
    const snapshot = ringingSnapshot();
    const { rerender } = render(
      <TimedReminderList
        now={new Date("2026-09-04T15:00:05+08:00")}
        onCancel={vi.fn()}
        onDismiss={onDismiss}
        onUpdate={vi.fn(async () => true)}
        requestingIds={new Set([snapshot.active[0].reminder_id])}
        snapshot={snapshot}
      />
    );

    const ringingHeader = screen.getByRole("heading", { name: "正在提醒" }).parentElement;
    expect(ringingHeader).not.toBeNull();
    const dismissButton = within(ringingHeader as HTMLElement).getByRole(
      "button",
      { name: "处理中" }
    );
    expect(dismissButton.hasAttribute("disabled")).toBe(true);
    expect(screen.getAllByText("正在提醒")).toHaveLength(3);
    expect(screen.queryByRole("button", { name: "取消" })).toBeNull();

    fireEvent.click(dismissButton);
    expect(onDismiss).not.toHaveBeenCalled();

    rerender(
      <TimedReminderList
        now={new Date("2026-09-04T15:00:05+08:00")}
        onCancel={vi.fn()}
        onDismiss={onDismiss}
        onUpdate={vi.fn(async () => true)}
        requestingIds={new Set()}
        snapshot={snapshot}
      />
    );
    fireEvent.click(within(ringingHeader as HTMLElement).getByRole(
      "button",
      { name: "全部知道了" }
    ));
    expect(onDismiss).toHaveBeenCalledWith(
      snapshot.runtime_id,
      snapshot.active.map(reminder => reminder.reminder_id)
    );
  });
});

function scheduledSnapshot(): TimedReminderSnapshot {
  return {
    runtime_id: "00000000-0000-4000-8000-0000000000aa",
    revision: 1,
    active: [{
      reminder_id: REMINDER_ID,
      content: "准备面试",
      state: "scheduled",
      trigger_at: "2026-09-04T15:00:00+08:00",
      created_at: "2026-09-04T10:00:00+08:00",
      updated_at: "2026-09-04T10:00:00+08:00",
      finished_at: null,
      ring_count: 0,
      next_ring_at: null,
      finish_reason: null,
      can_modify: true,
      can_cancel: true,
      can_dismiss: false
    }],
    recent: []
  };
}

function ringingSnapshot(): TimedReminderSnapshot {
  const first = {
    ...scheduledSnapshot().active[0],
    state: "ringing" as const,
    updated_at: "2026-09-04T15:00:00+08:00",
    ring_count: 1,
    next_ring_at: "2026-09-04T15:01:00+08:00",
    can_modify: false,
    can_cancel: false,
    can_dismiss: true
  };
  return {
    runtime_id: scheduledSnapshot().runtime_id,
    revision: 2,
    active: [
      first,
      {
        ...first,
        reminder_id: "123e4567-e89b-42d3-a456-426614174011",
        content: "结束专注计时"
      }
    ],
    recent: []
  };
}
