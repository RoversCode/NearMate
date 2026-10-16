// @vitest-environment jsdom

import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { App } from "./App";

// 此套用例覆盖普通控制台的领域行为，运行时边界由 RuntimeInitializationGate 测试单独验证。
vi.mock("./features/runtime-initialization/RuntimeInitializationGate", () => ({
  RuntimeInitializationGate: ({ children }: { children: import("react").ReactNode }) => children
}));
import type { TimedReminderSnapshot } from "./api/contracts/timedReminders";
import {
  createConsoleFetchMock,
  installConsoleControlWebSocketMock,
  resetConsoleAppTestEnvironment
} from "./test/consoleAppTestHarness";

const RUNTIME_ID = "00000000-0000-4000-8000-0000000000aa";
const RESTARTED_RUNTIME_ID = "00000000-0000-4000-8000-0000000000bb";
const REMINDER_ID = "123e4567-e89b-42d3-a456-426614174010";

afterEach(resetConsoleAppTestEnvironment);

describe("console timed reminders", () => {
  it("通过任务页签进入提醒视图，并完成行内修改与取消", async () => {
    window.history.replaceState(null, "", "/?section=tasks&taskView=reminders");
    installConsoleControlWebSocketMock();
    const context = installTimedReminderFetch();

    render(<App />);

    expect(await screen.findByRole("heading", { name: "任务中心" })).toBeTruthy();
    expect(screen.getByRole("tab", { name: "定时提醒" }).getAttribute("aria-selected"))
      .toBe("true");
    const reminderRow = (await screen.findByText("准备面试")).closest("tr") as HTMLTableRowElement;
    fireEvent.click(within(reminderRow).getByRole("button", { name: "修改" }));
    fireEvent.change(screen.getByLabelText("提醒内容"), {
      target: { value: "检查摄像头" }
    });
    fireEvent.change(screen.getByLabelText("提醒时间"), {
      target: { value: "2026-09-04T15:30" }
    });
    fireEvent.click(screen.getByRole("button", { name: "保存" }));

    expect(await screen.findByText("检查摄像头")).toBeTruthy();
    expect(context.patchCalls).toHaveLength(1);
    expect(JSON.parse(String(context.patchCalls[0]?.body))).toMatchObject({
      content: "检查摄像头"
    });

    const updatedRow = screen.getByText("检查摄像头").closest("tr") as HTMLTableRowElement;
    fireEvent.click(within(updatedRow).getByRole("button", { name: "取消" }));
    expect(await screen.findByText("已取消")).toBeTruthy();
    expect(context.deleteCalls).toBe(1);
    expect(window.location.search).toBe("?section=tasks&taskView=reminders");
  });

  it("新 Backend 运行期用 HTTP 快照接管并提示旧提醒已清空", async () => {
    window.history.replaceState(null, "", "/?section=tasks&taskView=reminders");
    const sockets = installConsoleControlWebSocketMock();
    const context = installTimedReminderFetch();

    render(<App />);
    expect(await screen.findByText("准备面试")).toBeTruthy();
    await waitFor(() => expect(sockets).toHaveLength(2));

    context.snapshot = emptySnapshot(0, RESTARTED_RUNTIME_ID);
    act(() => sockets[1].emitMessage({
      type: "reminders.snapshot_changed",
      payload: {
        runtime_id: RESTARTED_RUNTIME_ID,
        revision: 0,
        active: [],
        recent: []
      }
    }));

    expect(await screen.findByText("Backend 已重启，上一运行期的提醒已清空。")).toBeTruthy();
    expect(await screen.findByText("本次运行还没有定时提醒。")).toBeTruthy();
    expect(screen.queryByText("准备面试")).toBeNull();
  });

  it("提醒视图不解释 Activity 深链接，切回执行任务时清理查询参数", async () => {
    window.history.replaceState(
      null,
      "",
      `/?section=tasks&taskView=reminders&activity=${REMINDER_ID}`
    );
    installConsoleControlWebSocketMock();
    installTimedReminderFetch();

    render(<App />);
    expect(await screen.findByText("准备面试")).toBeTruthy();
    expect(window.location.search).toBe("?section=tasks&taskView=reminders");
    fireEvent.click(screen.getByRole("tab", { name: "执行任务" }));
    expect(window.location.search).toBe("?section=tasks&taskView=activities");
  });

  it("同一运行期不高于当前版本的通知不会重复读取 HTTP 快照", async () => {
    window.history.replaceState(null, "", "/?section=tasks&taskView=reminders");
    const sockets = installConsoleControlWebSocketMock();
    const context = installTimedReminderFetch();

    render(<App />);
    expect(await screen.findByText("准备面试")).toBeTruthy();
    await waitFor(() => expect(sockets).toHaveLength(2));
    const readsBefore = context.getCalls;

    act(() => sockets[1].emitMessage({
      type: "reminders.snapshot_changed",
      payload: {
        runtime_id: RUNTIME_ID,
        revision: 1,
        active: [],
        recent: []
      }
    }));
    await act(async () => Promise.resolve());

    expect(context.getCalls).toBe(readsBefore);
  });

  it("旧运行期取消请求的迟到失败不能污染新运行期", async () => {
    window.history.replaceState(null, "", "/?section=tasks&taskView=reminders");
    const sockets = installConsoleControlWebSocketMock();
    const context = installTimedReminderFetch();
    let resolveDelete: ((response: Response) => void) | null = null;
    context.deleteResponse = new Promise(resolve => {
      resolveDelete = resolve;
    });

    render(<App />);
    const reminderRow = (await screen.findByText("准备面试")).closest("tr") as HTMLTableRowElement;
    await waitFor(() => expect(sockets).toHaveLength(2));
    fireEvent.click(within(reminderRow).getByRole("button", { name: "取消" }));

    context.snapshot = emptySnapshot(0, RESTARTED_RUNTIME_ID);
    act(() => sockets[1].emitMessage({
      type: "reminders.snapshot_changed",
      payload: context.snapshot
    }));
    expect(await screen.findByText("本次运行还没有定时提醒。")).toBeTruthy();

    await act(async () => {
      resolveDelete?.(jsonResponse({ detail: "旧运行期冲突" }, 409));
      await Promise.resolve();
    });

    expect(screen.queryByText("提醒状态已经变化，当前不能取消。")).toBeNull();
  });

  it.each([1, 3])("第 %i 次播放期间可关闭，并由 HTTP 权威快照收起正在提醒区", async ringCount => {
    window.history.replaceState(null, "", "/?section=tasks&taskView=reminders");
    installConsoleControlWebSocketMock();
    const context = installTimedReminderFetch();
    context.snapshot = ringingSnapshot();
    context.snapshot.active[0].ring_count = ringCount;
    context.snapshot.active[0].next_ring_at = null;

    render(<App />);
    expect(await screen.findByText("准备面试")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "全部知道了" }));

    expect(await screen.findByText("已知道")).toBeTruthy();
    expect(context.dismissCalls).toHaveLength(1);
    expect(JSON.parse(String(context.dismissCalls[0]?.body))).toEqual({
      runtime_id: RUNTIME_ID,
      reminder_ids: [REMINDER_ID]
    });
    expect(screen.getByText("当前没有正在提醒的事项。")).toBeTruthy();
  });
});

function installTimedReminderFetch() {
  const baseFetch = createConsoleFetchMock();
  const context = {
    snapshot: scheduledSnapshot(),
    getCalls: 0,
    patchCalls: [] as RequestInit[],
    dismissCalls: [] as RequestInit[],
    deleteCalls: 0,
    deleteResponse: null as Promise<Response> | null
  };
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url.endsWith("/api/timed-reminders") && (!init?.method || init.method === "GET")) {
      context.getCalls += 1;
      return jsonResponse(context.snapshot);
    }
    if (url.endsWith(`/api/timed-reminders/${REMINDER_ID}`) && init?.method === "PATCH") {
      context.patchCalls.push(init);
      const body = JSON.parse(String(init.body)) as { content: string; trigger_at: string };
      context.snapshot = {
        ...context.snapshot,
        revision: context.snapshot.revision + 1,
        active: [{
          ...context.snapshot.active[0],
          content: body.content,
          trigger_at: body.trigger_at,
          updated_at: "2026-09-04T10:01:00+08:00"
        }]
      };
      return jsonResponse({ reminder: context.snapshot.active[0] });
    }
    if (url.endsWith(`/api/timed-reminders/${REMINDER_ID}`) && init?.method === "DELETE") {
      context.deleteCalls += 1;
      if (context.deleteResponse !== null) {
        return context.deleteResponse;
      }
      const cancelled = {
        ...context.snapshot.active[0],
        state: "cancelled" as const,
        updated_at: "2026-09-04T10:02:00+08:00",
        finished_at: "2026-09-04T10:02:00+08:00",
        finish_reason: "user_cancelled" as const,
        can_modify: false,
        can_cancel: false,
        can_dismiss: false
      };
      context.snapshot = {
        ...context.snapshot,
        revision: context.snapshot.revision + 1,
        active: [],
        recent: [cancelled]
      };
      return new Response(null, { status: 204 });
    }
    if (url.endsWith("/api/timed-reminders/dismiss") && init?.method === "POST") {
      context.dismissCalls.push(init);
      const dismissed = {
        ...context.snapshot.active[0],
        state: "dismissed" as const,
        updated_at: "2026-09-04T15:00:05+08:00",
        finished_at: "2026-09-04T15:00:05+08:00",
        next_ring_at: null,
        finish_reason: "user_dismissed" as const,
        can_dismiss: false
      };
      context.snapshot = {
        ...context.snapshot,
        revision: context.snapshot.revision + 1,
        active: [],
        recent: [dismissed]
      };
      return jsonResponse({ runtime_id: RUNTIME_ID, dismissed_ids: [REMINDER_ID] });
    }
    return baseFetch(input, init);
  }));
  return context;
}

function scheduledSnapshot(): TimedReminderSnapshot {
  return {
    runtime_id: RUNTIME_ID,
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

function emptySnapshot(revision: number, runtimeId: string): TimedReminderSnapshot {
  return { runtime_id: runtimeId, revision, active: [], recent: [] };
}

function ringingSnapshot(): TimedReminderSnapshot {
  return {
    ...scheduledSnapshot(),
    revision: 2,
    active: [{
      ...scheduledSnapshot().active[0],
      state: "ringing",
      updated_at: "2026-09-04T15:00:00+08:00",
      ring_count: 1,
      next_ring_at: "2026-09-04T15:01:00+08:00",
      can_modify: false,
      can_cancel: false,
      can_dismiss: true
    }]
  };
}

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json" }
  });
}
