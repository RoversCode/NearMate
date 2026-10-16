// @vitest-environment jsdom

import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { App } from "./App";

// 此套用例覆盖普通控制台的领域行为，运行时边界由 RuntimeInitializationGate 测试单独验证。
vi.mock("./features/runtime-initialization/RuntimeInitializationGate", () => ({
  RuntimeInitializationGate: ({ children }: { children: import("react").ReactNode }) => children
}));
import type { ActivitySnapshot } from "./api/contracts/activities";
import {
  createConsoleFetchMock,
  installConsoleControlWebSocketMock,
  resetConsoleAppTestEnvironment
} from "./test/consoleAppTestHarness";

const activeId = "123e4567-e89b-42d3-a456-426614174000";
const staleId = "123e4567-e89b-42d3-a456-426614174099";
const RUNTIME_ID = "00000000-0000-4000-8000-0000000000aa";
const RESTARTED_RUNTIME_ID = "00000000-0000-4000-8000-0000000000bb";

const stagePlan = [
  { stage_code: "preparing", stage_label: "准备任务" },
  { stage_code: "loading_materials", stage_label: "读取材料" },
  { stage_code: "processing", stage_label: "分析与处理" },
  { stage_code: "generating_result", stage_label: "生成结果" },
  { stage_code: "delivering_result", stage_label: "交付结果" }
] as const;

afterEach(resetConsoleAppTestEnvironment);

describe("console activity center", () => {
  it("从任务列表进入深链接详情，只展示公开字段并支持返回", async () => {
    window.history.replaceState(null, "", "/?section=tasks");
    installConsoleControlWebSocketMock();
    const { fetchMock } = installActivityFetch();

    render(<App />);

    expect(await screen.findByRole("heading", { name: "任务中心" })).toBeTruthy();
    const goalCell = await screen.findByText("整理用户材料");
    const activeRow = goalCell.closest("tr");
    expect(activeRow).not.toBeNull();
    expect(within(activeRow as HTMLTableRowElement).getByText("分析与处理")).toBeTruthy();
    fireEvent.click(within(activeRow as HTMLTableRowElement).getByRole("button", { name: "详情" }));

    expect(await screen.findByText("执行阶段")).toBeTruthy();
    expect(screen.getByText("公开结果正文")).toBeTruthy();
    expect(screen.queryByText("内部推理内容")).toBeNull();
    expect(screen.queryByText("reasoning")).toBeNull();
    expect(screen.queryByText("internal-token-secret")).toBeNull();
    expect(window.location.search).toBe(
      `?section=tasks&taskView=activities&activity=${activeId}`
    );
    expect(
      fetchMock.mock.calls.some(([input]) => String(input).endsWith(`/api/console/activities/${activeId}`))
    ).toBe(true);

    fireEvent.click(screen.getByRole("button", { name: "返回任务列表" }));
    expect(window.location.search).toBe("?section=tasks&taskView=activities");
    expect(await screen.findByText("最近任务")).toBeTruthy();
  });

  it("合法但已不存在的深链接展示过期空态", async () => {
    window.history.replaceState(null, "", `/?section=tasks&activity=${staleId}`);
    installConsoleControlWebSocketMock();
    installActivityFetch({ staleActivityId: staleId });

    render(<App />);

    expect(await screen.findByText("这项任务已不在本次运行记录中。")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "返回任务列表" }));
    expect(window.location.search).toBe("?section=tasks&taskView=activities");
  });

  it("取消请求中禁用重复操作，不乐观显示已取消", async () => {
    window.history.replaceState(null, "", "/?section=tasks");
    installConsoleControlWebSocketMock();
    const cancelResponse = createDeferred<Response>();
    const context = installActivityFetch({ cancelResponse: cancelResponse.promise });

    render(<App />);

    const goalCell = await screen.findByText("整理用户材料");
    const activeRow = goalCell.closest("tr") as HTMLTableRowElement;
    fireEvent.click(within(activeRow).getByRole("button", { name: "取消" }));

    const cancellingButton = within(activeRow).getByRole("button", { name: "取消中" });
    expect(cancellingButton.hasAttribute("disabled")).toBe(true);
    expect(screen.queryByText("已取消")).toBeNull();
    expect(context.cancelCalls).toBe(1);

    context.snapshot = {
      revision: 2,
      runtime_id: RUNTIME_ID,
      active: [{
        ...context.snapshot.active[0],
        status: "cancelling",
        can_cancel: false,
        updated_at: "2026-07-18T10:02:00+08:00"
      }],
      recent: []
    };
    await act(async () => {
      cancelResponse.resolve(new Response(null, { status: 202 }));
      await Promise.resolve();
    });

    expect(await screen.findByText("取消中", { selector: "span" })).toBeTruthy();
    expect(screen.queryByText("已取消")).toBeNull();
  });

  it("WebSocket 更高 revision 触发 HTTP 刷新，旧列表响应不会覆盖新状态", async () => {
    window.history.replaceState(null, "", "/?section=tasks");
    const sockets = installConsoleControlWebSocketMock();
    const firstList = createDeferred<Response>();
    const secondList = createDeferred<Response>();
    const baseFetch = createConsoleFetchMock();
    let activityListCalls = 0;
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/api/console/activities")) {
        activityListCalls += 1;
        return activityListCalls === 1 ? firstList.promise : secondList.promise;
      }
      return baseFetch(input);
    }));

    render(<App />);
    await waitFor(() => expect(sockets).toHaveLength(2));
    act(() => sockets[1].emitMessage({
      type: "activities.snapshot_changed",
      payload: { revision: 2, runtime_id: RUNTIME_ID, active: [], recent: [] }
    }));
    await waitFor(() => expect(activityListCalls).toBe(2));

    secondList.resolve(jsonResponse(completedSnapshot(2)));
    expect(await screen.findByText("已完成")).toBeTruthy();
    firstList.resolve(jsonResponse(runningSnapshot(1)));
    await act(async () => Promise.resolve());

    expect(screen.getByText("已完成")).toBeTruthy();
    expect(screen.queryByText("运行中")).toBeNull();
  });

  it("同一 runtime 的旧 revision 通知被忽略", async () => {
    window.history.replaceState(null, "", "/?section=tasks");
    const sockets = installConsoleControlWebSocketMock();
    const baseFetch = createConsoleFetchMock();
    let activityListCalls = 0;
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/api/console/activities")) {
        activityListCalls += 1;
        return jsonResponse(runningSnapshot(1));
      }
      return baseFetch(input);
    }));

    render(<App />);
    await screen.findByText("整理用户材料");
    await waitFor(() => expect(sockets).toHaveLength(2));
    const callsBefore = activityListCalls;

    act(() => sockets[1].emitMessage({
      type: "activities.snapshot_changed",
      payload: { revision: 1, runtime_id: RUNTIME_ID, active: [], recent: [] }
    }));
    await act(async () => Promise.resolve());

    expect(activityListCalls).toBe(callsBefore);
  });

  it("HTTP 快照提交后同一 revision 紧随到达时不重复刷新", async () => {
    window.history.replaceState(null, "", "/?section=tasks");
    const sockets = installConsoleControlWebSocketMock();
    const refreshedList = createDeferred<Response>();
    const baseFetch = createConsoleFetchMock();
    let activityListCalls = 0;
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/api/console/activities")) {
        activityListCalls += 1;
        return activityListCalls === 1
          ? jsonResponse(runningSnapshot(1))
          : refreshedList.promise;
      }
      return baseFetch(input);
    }));

    render(<App />);
    await screen.findByText("整理用户材料");
    await waitFor(() => expect(sockets).toHaveLength(2));

    act(() => sockets[1].emitMessage({
      type: "activities.snapshot_changed",
      payload: { revision: 2, runtime_id: RUNTIME_ID, active: [], recent: [] }
    }));
    await waitFor(() => expect(activityListCalls).toBe(2));

    // React 提交 effect 前又收到同一版本时，也必须使用刚完成的 HTTP 版本判断。
    await act(async () => {
      refreshedList.resolve(jsonResponse(completedSnapshot(2)));
      await flushHttpMicrotasks();
      sockets[1].emitMessage({
        type: "activities.snapshot_changed",
        payload: { revision: 2, runtime_id: RUNTIME_ID, active: [], recent: [] }
      });
      await Promise.resolve();
    });

    expect(activityListCalls).toBe(2);
    expect(screen.getByText("已完成")).toBeTruthy();
  });

  it("后台刷新期间保留当前详情不闪烁", async () => {
    window.history.replaceState(null, "", `/?section=tasks&activity=${activeId}`);
    const sockets = installConsoleControlWebSocketMock();
    const pendingDetail = createDeferred<Response>();
    const context = installActivityFetch({ nextDetailResponse: pendingDetail.promise });

    render(<App />);
    expect(await screen.findByText("公开结果正文")).toBeTruthy();
    await waitFor(() => expect(sockets).toHaveLength(2));

    context.detailResponse = pendingDetail.promise;
    act(() => sockets[1].emitMessage({
      type: "activities.snapshot_changed",
      payload: { revision: 2, runtime_id: RUNTIME_ID, active: [], recent: [] }
    }));
    await act(async () => Promise.resolve());

    // 详情请求尚未返回时，旧详情必须原地保留，不出现加载占位。
    expect(screen.getByText("公开结果正文")).toBeTruthy();
    expect(screen.queryByText("正在加载任务详情。")).toBeNull();

    await act(async () => {
      pendingDetail.resolve(jsonResponse(detailPayload()));
      await Promise.resolve();
    });
    expect(screen.getByText("公开结果正文")).toBeTruthy();
  });

  it("runtime 改变后清空旧任务并显示一次重启提示", async () => {
    window.history.replaceState(null, "", "/?section=tasks");
    const sockets = installConsoleControlWebSocketMock();
    const context = installActivityFetch();

    render(<App />);
    expect(await screen.findByText("运行中")).toBeTruthy();
    await waitFor(() => expect(sockets).toHaveLength(2));

    context.snapshot = completedSnapshot(0, RESTARTED_RUNTIME_ID);
    act(() => sockets[1].emitMessage({
      type: "activities.snapshot_changed",
      payload: { revision: 0, runtime_id: RESTARTED_RUNTIME_ID, active: [], recent: [] }
    }));

    expect(await screen.findByText("应用服务已重新启动，未完成的任务已结束。")).toBeTruthy();
    expect(await screen.findByText("已完成")).toBeTruthy();
    expect(screen.queryByText("运行中")).toBeNull();
  });

  it("HTTP 首先发现 runtime 改变时也清理旧运行期状态", async () => {
    window.history.replaceState(null, "", "/?section=tasks");
    const sockets = installConsoleControlWebSocketMock();
    const refreshedList = createDeferred<Response>();
    const baseFetch = createConsoleFetchMock();
    let activityListCalls = 0;
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/api/console/activities")) {
        activityListCalls += 1;
        return activityListCalls === 1
          ? jsonResponse(runningSnapshot(1))
          : refreshedList.promise;
      }
      return baseFetch(input);
    }));

    render(<App />);
    expect(await screen.findByText("运行中")).toBeTruthy();
    await waitFor(() => expect(sockets).toHaveLength(2));

    // 通知仍属于旧 runtime，但随后的 HTTP 权威快照已经来自新 Backend 进程。
    act(() => sockets[1].emitMessage({
      type: "activities.snapshot_changed",
      payload: { revision: 2, runtime_id: RUNTIME_ID, active: [], recent: [] }
    }));
    await waitFor(() => expect(activityListCalls).toBe(2));
    refreshedList.resolve(jsonResponse(completedSnapshot(0, RESTARTED_RUNTIME_ID)));

    expect(await screen.findByText("应用服务已重新启动，未完成的任务已结束。")).toBeTruthy();
    expect(await screen.findByText("已完成")).toBeTruthy();
    expect(screen.queryByText("运行中")).toBeNull();
  });

  it("断线保留旧数据并显示连接提示，恢复后静默校准", async () => {
    window.history.replaceState(null, "", "/?section=tasks");
    const sockets = installConsoleControlWebSocketMock();
    installActivityFetch();

    render(<App />);
    expect(await screen.findByText("整理用户材料")).toBeTruthy();
    await waitFor(() => expect(sockets).toHaveLength(2));

    vi.useFakeTimers();
    try {
      act(() => sockets[1].emitClose());

      // 普通断线不改变任务状态：旧列表保留，只显示轻量连接提示。
      expect(screen.getByText("任务状态连接已断开，正在重连。")).toBeTruthy();
      expect(screen.getByText("整理用户材料")).toBeTruthy();

      act(() => {
        vi.advanceTimersByTime(1000);
      });
      expect(sockets).toHaveLength(3);

      // 同 runtime 的初始快照负责重新校准；有效快照到达后连接提示清除。
      act(() => sockets[2].emitMessage({
        type: "activities.snapshot_changed",
        payload: { revision: 1, runtime_id: RUNTIME_ID, active: [], recent: [] }
      }));
      expect(screen.queryByText("任务状态连接已断开，正在重连。")).toBeNull();
      expect(screen.getByText("整理用户材料")).toBeTruthy();
    } finally {
      vi.useRealTimers();
    }
  });

  it("当前详情从运行中原地更新为完成", async () => {
    window.history.replaceState(null, "", `/?section=tasks&activity=${activeId}`);
    const sockets = installConsoleControlWebSocketMock();
    const context = installActivityFetch({ detailStatus: "running" });

    render(<App />);
    expect(await screen.findByText("运行中")).toBeTruthy();
    await waitFor(() => expect(sockets).toHaveLength(2));

    context.detailStatus = "completed";
    act(() => sockets[1].emitMessage({
      type: "activities.snapshot_changed",
      payload: { revision: 2, runtime_id: RUNTIME_ID, active: [], recent: [] }
    }));

    expect(await screen.findByText("已完成")).toBeTruthy();
    expect(screen.queryByText("正在加载任务详情。")).toBeNull();
  });

  it("详情刚变为过期时忽略同一提交窗口内的后续失效信号", async () => {
    window.history.replaceState(null, "", `/?section=tasks&activity=${activeId}`);
    const sockets = installConsoleControlWebSocketMock();
    const staleDetail = createDeferred<Response>();
    const context = installActivityFetch();

    render(<App />);
    expect(await screen.findByText("公开结果正文")).toBeTruthy();
    await waitFor(() => expect(sockets).toHaveLength(2));

    context.detailResponse = staleDetail.promise;
    act(() => sockets[1].emitMessage({
      type: "activities.snapshot_changed",
      payload: { revision: 2, runtime_id: RUNTIME_ID, active: [], recent: [] }
    }));
    await waitFor(() => expect(context.detailCalls).toBe(2));

    // 404 与后续通知落在同一次 React 提交中时，stale 状态仍须立即生效。
    await act(async () => {
      staleDetail.resolve(jsonResponse({ detail: "activity not found" }, 404));
      await flushHttpMicrotasks();
      sockets[1].emitMessage({
        type: "activities.snapshot_changed",
        payload: { revision: 3, runtime_id: RUNTIME_ID, active: [], recent: [] }
      });
      await Promise.resolve();
    });

    expect(context.detailCalls).toBe(2);
    expect(screen.getByText("这项任务已不在本次运行记录中。")).toBeTruthy();
  });

  it("runtime 改变后清空旧取消占位", async () => {
    window.history.replaceState(null, "", "/?section=tasks");
    const sockets = installConsoleControlWebSocketMock();
    const cancelResponse = createDeferred<Response>();
    const context = installActivityFetch({ cancelResponse: cancelResponse.promise });

    render(<App />);
    const goalCell = await screen.findByText("整理用户材料");
    const activeRow = goalCell.closest("tr") as HTMLTableRowElement;
    fireEvent.click(within(activeRow).getByRole("button", { name: "取消" }));
    expect(
      within(activeRow).getByRole("button", { name: "取消中" }).hasAttribute("disabled")
    ).toBe(true);
    await waitFor(() => expect(sockets).toHaveLength(2));

    // runtime 改变时取消中的本地占位必须随旧任务一起清空。
    context.snapshot = runningSnapshot(0, RESTARTED_RUNTIME_ID);
    act(() => sockets[1].emitMessage({
      type: "activities.snapshot_changed",
      payload: { revision: 0, runtime_id: RESTARTED_RUNTIME_ID, active: [], recent: [] }
    }));

    const nextGoalCell = await screen.findByText("整理用户材料");
    const nextRow = nextGoalCell.closest("tr") as HTMLTableRowElement;
    await waitFor(() => {
      expect(within(nextRow).getByRole("button", { name: "取消" })).toBeTruthy();
    });

    await act(async () => {
      cancelResponse.resolve(new Response(null, { status: 202 }));
      await Promise.resolve();
    });
  });
});

function installActivityFetch(options: {
  staleActivityId?: string;
  cancelResponse?: Promise<Response>;
  nextDetailResponse?: Promise<Response>;
  detailStatus?: "running" | "completed";
} = {}) {
  const baseFetch = createConsoleFetchMock();
  const context = {
    snapshot: runningSnapshot(1),
    cancelCalls: 0,
    detailCalls: 0,
    detailResponse: null as Promise<Response> | null,
    detailStatus: options.detailStatus ?? "completed"
  };
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url.endsWith("/api/console/activities")) {
      return jsonResponse(context.snapshot);
    }
    if (options.staleActivityId && url.endsWith(`/api/console/activities/${options.staleActivityId}`)) {
      return jsonResponse({ detail: "activity not found" }, 404);
    }
    if (url.endsWith(`/api/console/activities/${activeId}`)) {
      context.detailCalls += 1;
      return context.detailResponse ?? jsonResponse(detailPayload(context.detailStatus));
    }
    if (url.endsWith(`/api/activities/${activeId}/cancel`) && init?.method === "POST") {
      context.cancelCalls += 1;
      return options.cancelResponse ?? new Response(null, { status: 202 });
    }
    return baseFetch(input);
  });
  vi.stubGlobal("fetch", fetchMock);
  return Object.assign(context, { fetchMock });
}

function runningSnapshot(revision: number, runtimeId: string = RUNTIME_ID): ActivitySnapshot {
  return {
    revision,
    runtime_id: runtimeId,
    active: [{
      activity_id: activeId,
      kind: "future_agent",
      type_label: "未来任务",
      status: "running",
      goal_summary: "整理用户材料",
      failure_summary: null,
      stage_code: "processing",
      stage_label: "分析与处理",
      started_at: "2026-07-18T10:00:00+08:00",
      updated_at: "2026-07-18T10:01:00+08:00",
      finished_at: null,
      can_cancel: true
    }],
    recent: []
  };
}

function completedSnapshot(revision: number, runtimeId: string = RUNTIME_ID): ActivitySnapshot {
  return {
    revision,
    runtime_id: runtimeId,
    active: [],
    recent: [{
      ...runningSnapshot(revision, runtimeId).active[0],
      status: "completed",
      updated_at: "2026-07-18T10:03:00+08:00",
      finished_at: "2026-07-18T10:03:00+08:00",
      can_cancel: false
    }]
  };
}

function detailPayload(status: "running" | "completed" = "completed") {
  const base = status === "running"
    ? runningSnapshot(1).active[0]
    : completedSnapshot(1).recent[0];
  return {
    ...base,
    goal: "整理用户材料",
    stage_plan: stagePlan,
    stage_timeline: [],
    result_text: status === "running" ? "" : "公开结果正文",
    failure: null,
    failure_reason: null,
    work_plan: null,
    turn_outputs: [],
    reasoning_text: "内部推理内容",
    internal_phase: "reasoning",
    token_usage: "internal-token-secret"
  };
}

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json" }
  });
}

function createDeferred<T>() {
  let resolve: (value: T) => void = () => undefined;
  const promise = new Promise<T>(nextResolve => {
    resolve = nextResolve;
  });
  return { promise, resolve };
}

/** 让嵌套 HTTP 解析完成，同时保持当前 React 提交尚未结束。 */
async function flushHttpMicrotasks(): Promise<void> {
  for (let index = 0; index < 8; index += 1) {
    await Promise.resolve();
  }
}
