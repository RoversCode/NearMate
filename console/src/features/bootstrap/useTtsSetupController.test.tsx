import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { TtsSetupTask } from "../../api/contracts/bootstrap";
import { createTtsConfigurationFixture, createTtsTaskFixture, ttsModelsFixture } from "../../test/ttsSetupFixtures";
import { createBootstrapStatusPayload } from "../../test/consoleAppTestHarness";
import { useTtsSetupController } from "./useTtsSetupController";

afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals(); });

/**
 * 保留真实服务解析，只替换 HTTP 边界并按顺序交付任务快照。
 * Args:
 *   statuses: 首次加载和每次轮询的任务响应，可以包含延迟请求与网络失败。
 * Returns:
 *   用于核对请求次数的网络替身。
 */
function mockRequests(statuses: Array<TtsSetupTask | Error | Promise<TtsSetupTask>>) {
  let last = createTtsTaskFixture();
  let configuration = createTtsConfigurationFixture(true);
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    let payload: unknown;
    if (url.endsWith("/models")) payload = { provider: "local_streamvox", models: ttsModelsFixture, configured_model: ttsModelsFixture[0].model_id };
    else if (url.endsWith("/configuration")) {
      if (init?.method === "PUT") configuration = { ...configuration, ...JSON.parse(String(init.body)) };
      payload = configuration;
    }
    else if (url.endsWith("/status")) {
      const next = statuses.shift() ?? last;
      if (next instanceof Error) throw next;
      last = await next; payload = last;
    } else if (url.endsWith("/cancel") && init?.method === "POST") payload = { ...last, phase: "cancelled", can_cancel: false, can_retry: true };
    else throw new Error(`未预期的请求：${url}`);
    return new Response(JSON.stringify(payload));
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

const activeTask = () => createTtsTaskFixture({ task_id: "task-1", model_id: ttsModelsFixture[0].model_id, phase: "download", can_cancel: true, progress: { phase: "download", completed_bytes: 1, total_bytes: 2 } });

/** 等待一轮轮询及其 React 状态提交，不依赖真实一秒计时。 */
async function pollOnce() {
  await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
}

describe("语音任务状态恢复", () => {
  it("另一窗口提交新下载时采用权威任务，不把旧任务永久锁在下载中", async () => {
    vi.useFakeTimers();
    let catalogReads = 0;
    const oldTask = { ...activeTask(), task_id: "download-old" };
    const newTask = { ...oldTask, task_id: "download-new", phase: "completed", can_cancel: false };
    const initialFetch = mockRequests([createTtsTaskFixture({ phase: "ready" })]);
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/models")) {
        catalogReads += 1;
        return new Response(JSON.stringify({ provider: "local_streamvox", models: ttsModelsFixture.map(model => ({ ...model, local_state: "downloaded" })), configured_model: null, download_task: catalogReads === 1 ? oldTask : newTask }));
      }
      if (url.endsWith("/downloads")) return new Response(JSON.stringify(newTask));
      return initialFetch(input, init);
    }));
    const view = renderHook(() => useTtsSetupController("http://localhost", vi.fn()));
    await act(async () => {});
    expect(view.result.current.downloadRunning).toBe(true);
    await pollOnce();
    expect(view.result.current.downloadTask?.task_id).toBe("download-new");
    expect(view.result.current.anyTaskRunning).toBe(false);
    expect(view.result.current.models[0].local_state).toBe("downloaded");
    expect(catalogReads).toBe(2);
  });

  it.each(["failed", "cancelled"])("%s 仍在释放进程时继续轮询，直到重试资格开放", async phase => {
    vi.useFakeTimers();
    const pending = { ...activeTask(), phase };
    const fetchMock = mockRequests([pending, { ...pending, can_cancel: false, can_retry: true }]);
    const view = renderHook(() => useTtsSetupController("http://localhost", vi.fn()));
    await act(async () => {});
    expect(view.result.current.running).toBe(true);
    await pollOnce();
    expect(view.result.current.running).toBe(false);
    expect(view.result.current.task?.can_retry).toBe(true);
    expect(fetchMock.mock.calls.filter(([url]) => String(url).endsWith("/status"))).toHaveLength(2);
  });

  it("瞬时读取错误在下一次有效任务快照到达后消失，真实失败原因仍保留", async () => {
    vi.useFakeTimers();
    mockRequests([activeTask(), new Error("暂时无法读取进度"), { ...activeTask(), phase: "failed", can_cancel: false, can_retry: true, error: "缺少安装组件" }]);
    const view = renderHook(() => useTtsSetupController("http://localhost", vi.fn()));
    await act(async () => {});
    await pollOnce();
    expect(view.result.current.error).toContain("暂时无法读取进度");
    await pollOnce();
    expect(view.result.current.error).toBeNull();
    expect(view.result.current.task?.error).toBe("缺少安装组件");
  });

  it("取消提交前开始的迟到读取失败不能覆盖已经成功的取消", async () => {
    vi.useFakeTimers();
    let rejectOldRead!: (reason: Error) => void;
    const oldRead = new Promise<TtsSetupTask>((_, reject) => { rejectOldRead = reject; });
    mockRequests([activeTask(), oldRead]);
    const view = renderHook(() => useTtsSetupController("http://localhost", vi.fn()));
    await act(async () => {});
    await pollOnce();
    await act(async () => { await view.result.current.cancel(); });
    await act(async () => { rejectOldRead(new Error("旧请求已经断开")); });
    expect(view.result.current.task?.phase).toBe("cancelled");
    expect(view.result.current.error).toBeNull();
  });

  it("成功轮询不清除仍需用户处理的操作错误", async () => {
    vi.useFakeTimers();
    const initialFetch = mockRequests([activeTask(), activeTask()]);
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL, init?: RequestInit) => String(input).endsWith("/cancel")
      ? Promise.resolve(new Response(JSON.stringify({ detail: "暂时无法取消" }), { status: 409, headers: { "Content-Type": "application/json" } })) : initialFetch(input, init)));
    const view = renderHook(() => useTtsSetupController("http://localhost", vi.fn()));
    await act(async () => {});
    await act(async () => { await view.result.current.cancel(); });
    await pollOnce();
    expect(view.result.current.actionError).toContain("暂时无法取消");
    expect(view.result.current.pollError).toBeNull();
    expect(view.result.current.error).toContain("暂时无法取消");
  });

  it("初次读取失败可以显式重新加载并恢复已有任务", async () => {
    mockRequests([new Error("服务暂时不可达"), { ...activeTask(), phase: "failed", can_cancel: false, can_retry: true }]);
    const view = renderHook(() => useTtsSetupController("http://localhost", vi.fn()));
    await act(async () => {});
    expect(view.result.current.initialError).toBe("服务暂时不可达");
    act(() => { view.result.current.reload(); });
    await act(async () => {});
    expect(view.result.current.initialError).toBeNull();
    expect(view.result.current.task?.can_retry).toBe(true);
  });

  it("配置保存成功返回 true，后续读取失败不会诱导用户重复提交", async () => {
    mockRequests([createTtsTaskFixture(), new Error("刷新任务暂时失败")]);
    const view = renderHook(() => useTtsSetupController("http://localhost", vi.fn()));
    await act(async () => {});
    let saved: boolean | undefined;
    await act(async () => { saved = await view.result.current.saveConfiguration(); });
    expect(saved).toBe(true);
    expect(view.result.current.configuration?.confirmed).toBe(true);
    expect(view.result.current.actionError).toBeNull();
    expect(view.result.current.pollError).toBe("刷新任务暂时失败");
  });

  it("换配置已提交但读取失败时移除旧试听资格，重新读取才能恢复任务", async () => {
    const previous = { ...activeTask(), phase: "awaiting_confirmation", can_confirm: true };
    const fetchMock = mockRequests([previous, new Error("刷新任务暂时失败"), createTtsTaskFixture()]);
    const view = renderHook(() => useTtsSetupController("http://localhost", vi.fn()));
    await act(async () => {});
    act(() => { view.result.current.markPreviewPlayed(); view.result.current.setModelDirectory("E:\\New NearMate Models"); });
    await act(async () => { expect(await view.result.current.saveConfiguration()).toBe(true); });
    expect(view.result.current.configurationMatches).toBe(true);
    expect(view.result.current.task).toBeNull();
    expect(view.result.current.hasPlayedPreview).toBe(false);
    await act(async () => {
      expect(await view.result.current.preview()).toBe(false);
      expect(await view.result.current.confirm()).toBe(false);
    });
    expect(view.result.current.pollError).toBe("刷新任务暂时失败");
    expect(fetchMock.mock.calls.some(([url]) => /\/(?:preview|confirm)$/.test(String(url)))).toBe(false);
    act(() => { view.result.current.reload(); });
    await act(async () => {});
    expect(view.result.current.task?.phase).toBe("unconfigured");
    expect(view.result.current.pollError).toBeNull();
  });

  it("执行配置草稿与保存值不一致时，控制器本身也拒绝旧试听和确认", async () => {
    const fetchMock = mockRequests([{ ...activeTask(), phase: "awaiting_confirmation", can_confirm: true }]);
    const view = renderHook(() => useTtsSetupController("http://localhost", vi.fn()));
    await act(async () => {});
    act(() => { view.result.current.markPreviewPlayed(); view.result.current.setModelDirectory("E:\\Other Models"); });
    const requestCount = fetchMock.mock.calls.length;
    await act(async () => {
      expect(await view.result.current.preview()).toBe(false);
      expect(await view.result.current.confirm()).toBe(false);
    });
    expect(fetchMock.mock.calls).toHaveLength(requestCount);
  });

  it("配置保存失败、重复点击和非法目录都返回 false，向导不能误前进", async () => {
    let resolveSave!: (value: Response) => void;
    const initialFetch = mockRequests([createTtsTaskFixture()]);
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => init?.method === "PUT"
      ? new Promise<Response>(resolve => { resolveSave = resolve; }) : initialFetch(input, init));
    vi.stubGlobal("fetch", fetchMock);
    const view = renderHook(() => useTtsSetupController("http://localhost", vi.fn()));
    await act(async () => {});
    let saved!: Promise<boolean>;
    let duplicate!: Promise<boolean>;
    act(() => { saved = view.result.current.saveConfiguration(); duplicate = view.result.current.saveConfiguration(); });
    expect(await duplicate).toBe(false);
    await act(async () => {
      resolveSave(new Response(JSON.stringify({ detail: "目录不可写" }), { status: 400, headers: { "Content-Type": "application/json" } }));
      expect(await saved).toBe(false);
    });
    expect(view.result.current.actionError).toContain("目录不可写");
    act(() => { view.result.current.setModelDirectory("relative/path"); });
    await act(async () => { expect(await view.result.current.saveConfiguration()).toBe(false); });
    expect(fetchMock.mock.calls.filter(([, init]) => init?.method === "PUT")).toHaveLength(1);
  });

  it("已提交的试听确认即使后续进度读取失败也会发布完整初始化状态", async () => {
    const initialFetch = mockRequests([{ ...activeTask(), phase: "awaiting_confirmation", can_confirm: true }, new Error("读取任务暂时失败")]);
    const status = createBootstrapStatusPayload(true);
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL, init?: RequestInit) => String(input).endsWith("/confirm")
      ? Promise.resolve(new Response(JSON.stringify(status))) : initialFetch(input, init)));
    const onStatusChange = vi.fn();
    const view = renderHook(() => useTtsSetupController("http://localhost", onStatusChange));
    await act(async () => {});
    act(() => { view.result.current.markPreviewPlayed(); });
    await act(async () => { expect(await view.result.current.confirm()).toBe(true); });
    expect(onStatusChange).toHaveBeenCalledWith(status);
    expect(view.result.current.actionError).toBeNull();
    expect(view.result.current.pollError).toBe("读取任务暂时失败");
    expect(view.result.current.task).toBeNull();
    expect(view.result.current.hasPlayedPreview).toBe(false);
    await act(async () => { expect(await view.result.current.confirm()).toBe(false); });
    expect(onStatusChange).toHaveBeenCalledTimes(1);
  });

  it("确认后正式运行时启动失败时读取修复状态，不再保留旧确认资格", async () => {
    const previewTask = { ...activeTask(), phase: "awaiting_confirmation", can_confirm: true };
    const repairTask = { ...previewTask, phase: "repair_required", can_confirm: false, can_cancel: false, can_retry: true, error: "请准备并确认当前角色语音" };
    const initialFetch = mockRequests([previewTask, repairTask]);
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => String(input).endsWith("/confirm")
      ? Promise.resolve(new Response(JSON.stringify({ detail: "正式语音运行时启动失败" }), { status: 503, headers: { "Content-Type": "application/json" } })) : initialFetch(input, init));
    vi.stubGlobal("fetch", fetchMock);
    const onStatusChange = vi.fn();
    const view = renderHook(() => useTtsSetupController("http://localhost", onStatusChange));
    await act(async () => {});
    act(() => { view.result.current.markPreviewPlayed(); });
    await act(async () => { expect(await view.result.current.confirm()).toBe(false); });
    expect(view.result.current.task?.phase).toBe("repair_required");
    expect(view.result.current.task?.can_retry).toBe(true);
    expect(view.result.current.task?.can_confirm).toBe(false);
    expect(view.result.current.hasPlayedPreview).toBe(false);
    expect(view.result.current.actionError).toContain("正式语音运行时启动失败");
    expect(view.result.current.pollError).toBeNull();
    expect(onStatusChange).not.toHaveBeenCalled();
    expect(fetchMock.mock.calls.filter(([url]) => String(url).endsWith("/confirm"))).toHaveLength(1);
    expect(fetchMock.mock.calls.filter(([url]) => String(url).endsWith("/status"))).toHaveLength(2);
  });

  it("启动响应丢失时只读取已接纳的任务并恢复轮询，不再次发送启动请求", async () => {
    vi.useFakeTimers();
    const accepted = { ...activeTask(), task_id: "accepted-task", phase: "queued" };
    const initialFetch = mockRequests([createTtsTaskFixture(), accepted, { ...accepted, phase: "download" }]);
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => String(input).endsWith("/tasks")
      ? Promise.reject(new TypeError("启动响应连接中断")) : initialFetch(input, init));
    vi.stubGlobal("fetch", fetchMock);
    const view = renderHook(() => useTtsSetupController("http://localhost", vi.fn()));
    await act(async () => {});
    await act(async () => { expect(await view.result.current.start()).toBe(false); });
    expect(view.result.current.task?.task_id).toBe("accepted-task");
    expect(view.result.current.running).toBe(true);
    expect(view.result.current.actionError).toBe("启动响应连接中断");
    await pollOnce();
    expect(view.result.current.task?.phase).toBe("download");
    expect(view.result.current.actionError).toBe("启动响应连接中断");
    expect(fetchMock.mock.calls.filter(([url]) => String(url).endsWith("/tasks"))).toHaveLength(1);
  });

  it("操作失败后的状态对账也失败时保留两类错误，旧试听只能重新读取", async () => {
    const initialFetch = mockRequests([{ ...activeTask(), phase: "awaiting_confirmation", can_confirm: true }, new Error("读取任务连接中断")]);
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL, init?: RequestInit) => String(input).endsWith("/confirm")
      ? Promise.reject(new TypeError("确认响应连接中断")) : initialFetch(input, init)));
    const view = renderHook(() => useTtsSetupController("http://localhost", vi.fn()));
    await act(async () => {});
    act(() => { view.result.current.markPreviewPlayed(); });
    await act(async () => { expect(await view.result.current.confirm()).toBe(false); });
    expect(view.result.current.task).toBeNull();
    expect(view.result.current.hasPlayedPreview).toBe(false);
    expect(view.result.current.actionError).toBe("确认响应连接中断");
    expect(view.result.current.pollError).toBe("读取任务连接中断");
    expect(view.result.current.running).toBe(false);
  });

  it("切换控制面后忽略旧操作成功返回的任务", async () => {
    let resolveCancel!: (value: Response) => void;
    const initialFetch = mockRequests([activeTask(), createTtsTaskFixture()]);
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL, init?: RequestInit) => String(input).endsWith("/cancel")
      ? new Promise<Response>(resolve => { resolveCancel = resolve; }) : initialFetch(input, init)));
    const view = renderHook(({ baseUrl }) => useTtsSetupController(baseUrl, vi.fn()), { initialProps: { baseUrl: "http://first" } });
    await act(async () => {});
    let cancellation!: Promise<boolean>;
    act(() => { cancellation = view.result.current.cancel(); });
    view.rerender({ baseUrl: "http://second" });
    await act(async () => {});
    await act(async () => {
      resolveCancel(new Response(JSON.stringify({ ...activeTask(), phase: "cancelled", can_cancel: false, can_retry: true })));
      await cancellation;
    });
    expect(view.result.current.task?.phase).toBe("unconfigured");
  });
});
