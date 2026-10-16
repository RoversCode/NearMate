import { afterEach, describe, expect, it, vi } from "vitest";
import {
  cancelTimedReminder,
  dismissTimedReminders,
  fetchTimedReminders,
  TimedReminderApiError,
  updateTimedReminder
} from "./timedReminders";

const RUNTIME_ID = "00000000-0000-4000-8000-0000000000aa";
const REMINDER_ID = "123e4567-e89b-42d3-a456-426614174010";
const scheduledReminder = {
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
} as const;

afterEach(() => vi.unstubAllGlobals());

describe("timed reminders service", () => {
  it("读取并严格解析完整提醒快照", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({
      runtime_id: RUNTIME_ID,
      revision: 1,
      active: [scheduledReminder],
      recent: []
    })));

    await expect(fetchTimedReminders("http://localhost/")).resolves.toEqual({
      runtime_id: RUNTIME_ID,
      revision: 1,
      active: [scheduledReminder],
      recent: []
    });
  });

  it.each([
    [{ ...scheduledReminder, state: "dismissed" }],
    [{ ...scheduledReminder, can_modify: false }],
    [{ ...scheduledReminder, internal_attempts: 2 }]
  ])("拒绝非法生命周期或混入内部字段的活跃提醒", async invalidReminder => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({
      runtime_id: RUNTIME_ID,
      revision: 1,
      active: [invalidReminder],
      recent: []
    })));

    await expect(fetchTimedReminders("http://localhost")).rejects.toThrow(
      "定时提醒列表响应格式无效"
    );
  });

  it("拒绝没有明确时区的绝对时间", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({
      runtime_id: RUNTIME_ID,
      revision: 1,
      active: [{ ...scheduledReminder, trigger_at: "2026-09-04T15:00:00" }],
      recent: []
    })));

    await expect(fetchTimedReminders("http://localhost")).rejects.toThrow(
      "定时提醒列表响应格式无效"
    );
  });

  it("按 Unicode 字符而不是 UTF-16 单元校验提醒内容长度", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({
      runtime_id: RUNTIME_ID,
      revision: 1,
      active: [{ ...scheduledReminder, content: "😀".repeat(500) }],
      recent: []
    })));
    await expect(fetchTimedReminders("http://localhost")).resolves.toMatchObject({
      active: [{ content: "😀".repeat(500) }]
    });

    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({
      runtime_id: RUNTIME_ID,
      revision: 1,
      active: [{ ...scheduledReminder, content: "😀".repeat(501) }],
      recent: []
    })));
    await expect(fetchTimedReminders("http://localhost")).rejects.toThrow(
      "定时提醒列表响应格式无效"
    );
  });

  it("PATCH 只发送变化字段，DELETE 取消同一条提醒", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(jsonResponse({ reminder: scheduledReminder }))
      .mockResolvedValueOnce(new Response(null, { status: 204 }));
    vi.stubGlobal("fetch", fetchMock);

    await updateTimedReminder("http://localhost/", REMINDER_ID, {
      content: "检查摄像头"
    });
    await cancelTimedReminder("http://localhost", REMINDER_ID);

    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      `http://localhost/api/timed-reminders/${REMINDER_ID}`,
      {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          content: "检查摄像头"
        })
      }
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      `http://localhost/api/timed-reminders/${REMINDER_ID}`,
      { method: "DELETE" }
    );
  });

  it("按冻结运行期与编号原子关闭响铃批次", async () => {
    const fetchMock = vi.fn(async () => jsonResponse({
      runtime_id: RUNTIME_ID,
      dismissed_ids: [REMINDER_ID]
    }));
    vi.stubGlobal("fetch", fetchMock);

    await expect(dismissTimedReminders(
      "http://localhost/",
      RUNTIME_ID,
      [REMINDER_ID]
    )).resolves.toEqual({
      runtimeId: RUNTIME_ID,
      dismissedIds: [REMINDER_ID]
    });
    expect(fetchMock).toHaveBeenCalledWith(
      "http://localhost/api/timed-reminders/dismiss",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ runtime_id: RUNTIME_ID, reminder_ids: [REMINDER_ID] })
      }
    );
  });

  it("保留写接口状态码并映射稳定提示", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({ detail: "conflict" }, 409)));

    await expect(cancelTimedReminder("http://localhost", REMINDER_ID)).rejects.toMatchObject({
      name: "TimedReminderApiError",
      status: 409,
      message: "提醒状态已经变化，当前不能取消。"
    } satisfies Partial<TimedReminderApiError>);
  });
});

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json" }
  });
}
