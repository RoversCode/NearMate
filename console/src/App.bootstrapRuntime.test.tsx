import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { App } from "./App";
import type { RuntimeInitializationStatus } from "./features/runtime-initialization/runtime-status";
import {
  createBootstrapStatusPayload,
  createConsoleFetchMock,
  createLlmConfigPayload,
  installConsoleControlWebSocketMock,
  resetConsoleAppTestEnvironment
} from "./test/consoleAppTestHarness";
import { createTtsConfigurationFixture, createTtsTaskFixture, ttsModelsFixture } from "./test/ttsSetupFixtures";

afterEach(resetConsoleAppTestEnvironment);

/**
 * 保留真实运行时门禁，只替换它依赖的 HTTP 与 WebSocket。
 *
 * Args:
 *   initialized: 本次打开之前是否已经完成首次配置。
 * Returns:
 *   可以推进运行时阶段与完成试听确认的测试服务。
 */
function installBootstrapRuntimeService(initialized = false) {
  const sockets = installConsoleControlWebSocketMock();
  const configuredFetch = createConsoleFetchMock({ isInitialized: true });
  let isInitialized = initialized;
  let runtime: RuntimeInitializationStatus = {
    runtime_id: "d01f2fa6-8e4d-430f-8b72-4bc3d02051ec",
    operation_id: null,
    revision: 0,
    state: "idle",
    phase: "idle",
    message: "可以继续首次配置",
    retryable: false
  };
  let finishConfirmation: ((response: Response) => void) | null = null;
  const reply = (value: unknown) => new Response(JSON.stringify(value), {
    headers: { "Content-Type": "application/json" }
  });
  const bootstrapStatus = () => ({
    ...createBootstrapStatusPayload(isInitialized),
    llm: { is_ready: true, missing: [], errors: [] },
    voice: {
      ...createBootstrapStatusPayload(isInitialized).voice,
      is_ready: isInitialized,
      phase: isInitialized ? "ready" : "awaiting_confirmation",
      has_confirmed_voice: isInitialized
    }
  });
  const publishRuntime = () => {
    for (const socket of sockets) {
      if (socket.close.mock.calls.length === 0) {
        socket.emitMessage({ type: "runtime.initialization_changed", payload: runtime });
      }
    }
  };
  const startRuntime = () => {
    runtime = {
      ...runtime,
      operation_id: "88c655af-c333-4e30-b424-a1fb67794847",
      revision: runtime.revision + 1,
      state: "initializing",
      phase: "assistant",
      message: "正在启动助手与语音引擎",
      retryable: false
    };
    publishRuntime();
  };

  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const url = input.toString();
    if (url.endsWith("/api/runtime/initialization")) return reply(runtime);
    if (url.endsWith("/api/runtime/initialization/retry")) {
      startRuntime();
      return reply(runtime);
    }
    if (url.endsWith("/api/bootstrap/status")) return reply(bootstrapStatus());
    if (url.endsWith("/api/llm/config")) return reply(createLlmConfigPayload(true));
    if (url.endsWith("/api/bootstrap/tts/configuration")) return reply(createTtsConfigurationFixture(true));
    if (url.endsWith("/api/bootstrap/tts/status")) return reply(createTtsTaskFixture({
      task_id: "first-voice", model_id: ttsModelsFixture[0].model_id,
      phase: isInitialized ? "ready" : "awaiting_confirmation",
      can_confirm: !isInitialized
    }));
    if (url.endsWith("/first-voice/preview")) return new Response(new Blob(["试听测试音频"]));
    if (url.endsWith("/first-voice/confirm")) {
      // 正式确认会先重建运行时，HTTP 回执可以在向导卸载之后才返回。
      const response = new Promise<Response>(resolve => { finishConfirmation = resolve; });
      startRuntime();
      return response;
    }
    return configuredFetch(input, init);
  });
  vi.stubGlobal("fetch", fetchMock);
  const OriginalUrl = URL;
  vi.stubGlobal("URL", class extends OriginalUrl {
    static createObjectURL = vi.fn(() => "blob:first-voice");
    static revokeObjectURL = vi.fn();
  });

  return {
    fetchMock,
    failRuntime() {
      runtime = { ...runtime, revision: runtime.revision + 1, state: "failed", phase: "failed",
        message: "语音引擎初始化失败，请重试。", retryable: true };
      publishRuntime();
    },
    finishRuntime() {
      isInitialized = true;
      runtime = { ...runtime, revision: runtime.revision + 1, state: "ready", phase: "ready",
        message: "初始化完成", retryable: false };
      finishConfirmation?.(reply(bootstrapStatus()));
      publishRuntime();
    }
  };
}

describe("首次向导与真实运行时门禁的交接", () => {
  it.each([false, true])("试听确认后经过运行时重建仍保留完成页（需要重试：%s）", async needsRetry => {
    const service = installBootstrapRuntimeService();
    render(<App />);
    const audio = await screen.findByLabelText("角色语音试听");
    await waitFor(() => expect(audio.getAttribute("src")).toBe("blob:first-voice"));
    fireEvent.ended(audio);
    fireEvent.click(screen.getByRole("button", { name: "声音没问题，完成设置" }));
    await screen.findByRole("dialog", { name: "正在初始化 NearMate" });
    expect(screen.queryByRole("region", { name: "首次初始化" })).toBeNull();

    if (needsRetry) {
      await act(async () => service.failRuntime());
      fireEvent.click(await screen.findByRole("button", { name: "重新初始化" }));
      await waitFor(() => expect(service.fetchMock.mock.calls.filter(([input]) =>
        input.toString().endsWith("/api/runtime/initialization/retry"))).toHaveLength(1));
    }

    await act(async () => service.finishRuntime());
    await screen.findByRole("region", { name: "初始化完成" });
    expect(screen.queryByRole("button", { name: "设置" })).toBeNull();
    expect(service.fetchMock.mock.calls.filter(([input]) => input.toString().endsWith("/first-voice/confirm"))).toHaveLength(1);
    fireEvent.click(screen.getByRole("button", { name: "进入控制台" }));
    await screen.findByRole("button", { name: "设置" });
    expect(screen.queryByRole("region", { name: "初始化完成" })).toBeNull();
  });

  it("已经完成初始化的用户直接进入控制台", async () => {
    installBootstrapRuntimeService(true);
    render(<App />);
    await screen.findByRole("button", { name: "设置" });
    expect(screen.queryByRole("region", { name: "首次初始化" })).toBeNull();
    expect(screen.queryByRole("region", { name: "初始化完成" })).toBeNull();
  });
});
