import { afterEach, describe, expect, it, vi } from "vitest";
import {
  cancelTimedReminder,
  dismissTimedReminders,
  updateTimedReminder
} from "./timedReminderClient";

describe("timedReminderClient", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("只提交实际变化的提醒字段", async () => {
    const fetchMock = vi.fn(async () => new Response("{}", { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);

    await updateTimedReminder("http://127.0.0.1:8765/", "reminder/a", {
      content: "准备面试"
    });

    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:8765/api/timed-reminders/reminder%2Fa",
      {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          content: "准备面试"
        })
      }
    );
  });

  it("使用冻结运行期和编号原子关闭响铃批次", async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({
      runtime_id: "runtime-a",
      dismissed_ids: ["reminder-a"]
    }), { status: 200, headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fetchMock);

    await expect(dismissTimedReminders(
      "http://127.0.0.1:8765/",
      "runtime-a",
      ["reminder-a"]
    )).resolves.toEqual({ runtimeId: "runtime-a", dismissedIds: ["reminder-a"] });
    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:8765/api/timed-reminders/dismiss",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          runtime_id: "runtime-a",
          reminder_ids: ["reminder-a"]
        })
      })
    );
  });

  it("使用 DELETE 取消提醒", async () => {
    const fetchMock = vi.fn(async () => new Response(null, { status: 204 }));
    vi.stubGlobal("fetch", fetchMock);

    await cancelTimedReminder("http://127.0.0.1:8765", "reminder-a");

    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:8765/api/timed-reminders/reminder-a",
      { method: "DELETE" }
    );
  });

  it.each([
    [404, "提醒已不在本次运行中。"],
    [409, "提醒状态已经变化，当前不能修改。"],
    [422, "提醒内容或时间无效，请检查后重试。"],
    [503, "提醒运行时暂不可用，请稍后重试。"]
  ])("将修改 HTTP %i 映射为明确错误", async (status, message) => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(null, { status })));

    await expect(updateTimedReminder("http://localhost", "id", {
      content: "事项",
      triggerAt: "2026-09-04T07:00:00.000Z"
    })).rejects.toThrow(message);
  });
});
