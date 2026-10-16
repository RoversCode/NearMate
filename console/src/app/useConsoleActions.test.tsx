import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useState } from "react";
import { createConsoleSettingsPayload } from "../test/consoleAppTestHarness";
import { initialConsoleData, type ConsoleData } from "./consoleTypes";
import { useConsoleActions } from "./useConsoleActions";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

type Deferred<T> = {
  promise: Promise<T>;
  resolve: (value: T) => void;
  reject: (reason?: unknown) => void;
};

function createDeferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((promiseResolve, promiseReject) => {
    resolve = promiseResolve;
    reject = promiseReject;
  });
  return { promise, resolve, reject };
}

function createSettingsResponse(settingsRevision: number): Response {
  return new Response(
    JSON.stringify({
      ...createConsoleSettingsPayload(),
      settings_revision: settingsRevision
    }),
    { headers: { "Content-Type": "application/json" } }
  );
}

function useConsoleActionsTestHarness(baseUrl: string) {
  const [consoleData, setConsoleData] = useState<ConsoleData>(() => ({
    ...initialConsoleData,
    settings: createConsoleSettingsPayload()
  }));

  const actions = useConsoleActions({
    baseUrl,
    consoleData,
    setConsoleData,
    setBootstrapStatus: vi.fn(),
    loadConsoleData: vi.fn(async () => undefined)
  });

  return { actions, settings: consoleData.settings };
}

describe("useConsoleActions 设置快照回读", () => {
  it("有专用 mutation 提交点快照时直接同步，不再发起二次 GET", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const view = renderHook(() => useConsoleActionsTestHarness("http://localhost"));
    const candidateSettings = {
      ...createConsoleSettingsPayload(),
      settings_revision: 9,
      user_self_intro: "来自专用配置响应"
    };

    await act(async () => {
      await view.result.current.actions.refreshSettingsSnapshot(candidateSettings);
    });

    expect(view.result.current.settings).toMatchObject(candidateSettings);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("较晚回读失败时仍应用较早请求拿到的权威快照", async () => {
    const firstRead = createDeferred<Response>();
    const secondRead = createDeferred<Response>();
    const fetchMock = vi.fn()
      .mockReturnValueOnce(firstRead.promise)
      .mockReturnValueOnce(secondRead.promise);
    vi.stubGlobal("fetch", fetchMock);

    const view = renderHook(() => useConsoleActionsTestHarness("http://localhost"));
    let firstRefresh!: Promise<unknown>;
    let secondRefresh!: Promise<unknown>;
    await act(async () => {
      firstRefresh = view.result.current.actions.refreshSettingsSnapshot();
      secondRefresh = view.result.current.actions.refreshSettingsSnapshot();
    });

    await act(async () => {
      secondRead.reject(new Error("后发回读暂时失败"));
      await secondRefresh;
    });
    await act(async () => {
      firstRead.resolve(createSettingsResponse(7));
      await firstRefresh;
    });

    expect(view.result.current.settings?.settings_revision).toBe(7);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
});
