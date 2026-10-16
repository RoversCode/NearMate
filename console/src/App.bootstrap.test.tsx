import { StrictMode } from "react";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, it, expect, vi } from "vitest";
import { App } from "./App";

// 此套用例验证首次初始化；运行时阻断边界由 RuntimeInitializationGate 测试覆盖。
vi.mock("./features/runtime-initialization/RuntimeInitializationGate", () => ({
  RuntimeInitializationGate: ({ children }: { children: import("react").ReactNode }) => children
}));
import {
  createConsoleSettingsPayload,
  createBootstrapStatusPayload,
  createLlmConfigPayload,
  createConsoleFetchMock,
  installConsoleControlWebSocketMock,
  resetConsoleAppTestEnvironment
} from "./test/consoleAppTestHarness";
import { createCharacterCardPayload } from "./test/characterCardFixtures";
import { createTtsTaskFixture } from "./test/ttsSetupFixtures";

afterEach(resetConsoleAppTestEnvironment);

describe("console app bootstrap", () => {
  it("未初始化时优先渲染首次初始化页，而不是现有导航", async () => {
      const fetchMock = createConsoleFetchMock({ isInitialized: false });
      vi.stubGlobal("fetch", fetchMock);

      render(<App />);

      expect(await screen.findByRole("heading", { name: "NearMate Console" })).toBeTruthy();
      expect(await screen.findByRole("region", { name: "首次初始化" })).toBeTruthy();
      expect(await screen.findByRole("heading", { name: "第 2 步，共 5 步" })).toBeTruthy();
      expect(screen.queryByLabelText("和风天气 API Key")).toBeNull();
      expect(screen.queryByText("StreamVox")).toBeNull();
      expect(fetchMock).toHaveBeenCalledWith(expect.stringMatching(/\/api\/bootstrap\/status$/));
      expect(fetchMock).toHaveBeenCalledWith(expect.stringMatching(/\/api\/llm\/config$/));
      expect(fetchMock).toHaveBeenCalledWith(expect.stringMatching(/\/api\/llm\/supported-models$/));
      expect(fetchMock.mock.calls.some(([input]) => input.toString().endsWith("/api/settings"))).toBe(false);
    });

  it("LLM 已保存但状态回读失败时，在首次引导中展示提醒而不误报连接失败", async () => {
      const baseFetch = createConsoleFetchMock({ isInitialized: false });
      let bootstrapStatusReads = 0;
      const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = input.toString();
        if (url.endsWith("/api/bootstrap/status")) {
          bootstrapStatusReads += 1;
          if (bootstrapStatusReads >= 2) {
            return new Response(JSON.stringify({ detail: "状态服务暂时不可用" }), {
              status: 503,
              headers: { "Content-Type": "application/json" }
            });
          }
        }
        if (url.endsWith("/api/llm/config") && init?.method === "PUT") {
          return new Response(JSON.stringify(createLlmConfigPayload(true)), {
            headers: { "Content-Type": "application/json" }
          });
        }
        return baseFetch(input, init);
      });
      vi.stubGlobal("fetch", fetchMock);
      render(<App />);

      const workspaceInput = await screen.findByLabelText("Qwen 工作空间 ID（WorkspaceId）");
      fireEvent.change(workspaceInput, { target: { value: "workspace_next" } });
      fireEvent.change(screen.getByLabelText("Qwen API Key"), { target: { value: "qwen-key" } });
      fireEvent.click(screen.getByRole("button", { name: "验证两家连接并继续" }));

      expect(await screen.findByText(/已保存，但页面状态刷新失败：/)).toBeTruthy();
      expect(screen.queryByText(/连接验证未通过：/)).toBeNull();
    });

  it("验证 AI 配置后进入检查电脑步骤，全部就绪后保留欢迎页直到主动进入控制台", async () => {
      const sockets = installConsoleControlWebSocketMock();
      const uninitializedFetch = createConsoleFetchMock({ isInitialized: false });
      const initializedFetch = createConsoleFetchMock({ isInitialized: true });
      let llmReady = false;
      let initialized = false;
      const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = input.toString();
        if (url.endsWith("/api/bootstrap/status")) {
          return new Response(JSON.stringify({
            ...createBootstrapStatusPayload(initialized),
            llm: { is_ready: llmReady, missing: llmReady ? [] : ["qwen.api_key"], errors: [] },
            voice: { ...createBootstrapStatusPayload(false).voice, is_ready: initialized, phase: initialized ? "ready" : "unconfigured" }
          }));
        }
        if (url.endsWith("/api/llm/config")) {
          if (init?.method === "PUT") llmReady = true;
          const config = createLlmConfigPayload(llmReady);
          if (!llmReady) config.credentials.qwen.base_url = "https://workspace_initial.cn-beijing.maas.aliyuncs.com/compatible-mode/v1";
          return new Response(JSON.stringify(config));
        }
        return initialized ? initializedFetch(input, init) : uninitializedFetch(input, init);
      });
      vi.stubGlobal("fetch", fetchMock);
      render(<App />);

      const workspaceInput = await screen.findByLabelText("Qwen 工作空间 ID（WorkspaceId）");
      expect((workspaceInput as HTMLInputElement).value).toBe("workspace_initial");
      expect(screen.queryByLabelText("和风天气 API Key")).toBeNull();
      fireEvent.change(workspaceInput, { target: { value: " workspace_next " } });
      fireEvent.change(screen.getByLabelText("Qwen API Key"), { target: { value: " qwen-key " } });
      fireEvent.click(screen.getByRole("button", { name: "验证两家连接并继续" }));

      await screen.findByRole("heading", { name: "第 3 步，共 5 步" });
      expect(fetchMock).toHaveBeenCalledWith(expect.stringMatching(/\/api\/llm\/config$/), expect.objectContaining({
        method: "PUT",
        body: expect.stringContaining('"base_url":"workspace_next","api_key":"qwen-key"')
      }));
      expect(fetchMock.mock.calls.some(([input]) => input.toString().endsWith("/api/bootstrap/initialize"))).toBe(false);
      expect(screen.queryByText("StreamVox")).toBeNull();

      // 语音确认的权威事件到达后仍停留在交接页，避免最后一步突然切成设置面板。
      initialized = true;
      act(() => sockets[0].emitMessage({ type: "bootstrap.status_changed", payload: createBootstrapStatusPayload(true) }));
      expect(await screen.findByRole("heading", { name: "准备好了，开始认识你的伙伴吧" })).toBeTruthy();
      expect(screen.queryByText("StreamVox")).toBeNull();
      fireEvent.click(screen.getByRole("button", { name: "进入控制台" }));
      expect(await screen.findByText("StreamVox")).toBeTruthy();
      expect(screen.queryByRole("region", { name: "初始化完成" })).toBeNull();
    });

  it("AI 连接验证进行中会阻止重复提交，成功后进入下一步", async () => {
      let finishLlmSave!: (response: Response) => void;
      const llmSaveResponse = new Promise<Response>(resolve => { finishLlmSave = resolve; });
      const baseFetch = createConsoleFetchMock({ isInitialized: false });
      let llmReady = false;
      const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = input.toString();
        if (url.endsWith("/api/llm/config") && init?.method === "PUT") return llmSaveResponse;
        if (url.endsWith("/api/bootstrap/status")) return new Response(JSON.stringify({
          ...createBootstrapStatusPayload(false),
          llm: { is_ready: llmReady, missing: llmReady ? [] : ["qwen.api_key"], errors: [] },
          voice: { ...createBootstrapStatusPayload(false).voice, is_ready: false, phase: "unconfigured" }
        }));
        return baseFetch(input, init);
      });
      vi.stubGlobal("fetch", fetchMock);
      render(<App />);

      fireEvent.change(await screen.findByLabelText("Qwen 工作空间 ID（WorkspaceId）"), { target: { value: "workspace_next" } });
      fireEvent.change(screen.getByLabelText("Qwen API Key"), { target: { value: "qwen-key" } });
      const submitButton = screen.getByRole("button", { name: "验证两家连接并继续" }) as HTMLButtonElement;
      fireEvent.click(submitButton);
      fireEvent.click(submitButton);

      expect(submitButton.disabled).toBe(true);
      expect(fetchMock.mock.calls.filter(([input, init]) => input.toString().endsWith("/api/llm/config") && init?.method === "PUT")).toHaveLength(1);
      await act(async () => {
        llmReady = true;
        finishLlmSave(new Response(JSON.stringify(createLlmConfigPayload(true))));
        await llmSaveResponse;
      });
      expect(await screen.findByRole("heading", { name: "第 3 步，共 5 步" })).toBeTruthy();
      expect(fetchMock.mock.calls.some(([input]) => input.toString().endsWith("/api/bootstrap/initialize"))).toBe(false);
    });
  it("已经初始化的用户只有天气验证保存入口，无需再次提交初始化", async () => {
      const fetchMock = createConsoleFetchMock({ isInitialized: true });
      vi.stubGlobal("fetch", fetchMock);
      render(<App />);
      fireEvent.click(await screen.findByRole("tab", { name: "AI 与服务" }));
      expect(screen.queryByRole("region", { name: "首次初始化" })).toBeNull();
      expect(screen.queryByRole("button", { name: "保存服务配置" })).toBeNull();
      fireEvent.change(screen.getByLabelText("和风天气 API Host"), { target: { value: "example.qweatherapi.com" } });
      fireEvent.change(screen.getByLabelText("和风天气 API Key"), { target: { value: "weather-key" } });
      fireEvent.click(screen.getByRole("button", { name: "验证并保存" }));
      await waitFor(() => expect(fetchMock.mock.calls.some(([input, init]) => input.toString().includes("qweather") && init?.method === "POST")).toBe(true));
      expect(fetchMock.mock.calls.some(([input]) => input.toString().endsWith("/api/bootstrap/initialize"))).toBe(false);
    });

  it("严格模式重放 effect 后仍能完成首屏数据加载", async () => {
      const fetchMock = createConsoleFetchMock({ isInitialized: true });
      vi.stubGlobal("fetch", fetchMock);

      render(<StrictMode><App /></StrictMode>);

      expect(await screen.findByText("StreamVox")).toBeTruthy();
      expect(fetchMock).toHaveBeenCalledWith(expect.stringMatching(/\/api\/settings$/));
    });

  it("已进入控制台的用户准备新语音时保留模型管理，并继续接收设置更新", async () => {
      const sockets = installConsoleControlWebSocketMock();
      const baseFetch = createConsoleFetchMock({ isInitialized: true });
      vi.stubGlobal("fetch", baseFetch);
      window.history.replaceState(null, "", "/?section=streamvox");

      render(<App />);
      expect(await screen.findByText("音色管理")).toBeTruthy();
      await screen.findByLabelText("StreamVox 当前模型音色列表");
      const settingsCallCount = (): number => baseFetch.mock.calls.filter(
        ([input]) => input.toString().endsWith("/api/settings")
      ).length;

      const preparingStatus = {
        ...createBootstrapStatusPayload(true),
        is_initialized: false,
        voice: {
          ...createBootstrapStatusPayload(true).voice,
          is_ready: false,
          phase: "load"
        }
      };
      act(() => sockets[0].emitMessage({ type: "bootstrap.status_changed", payload: preparingStatus }));

      expect(screen.queryByRole("region", { name: "首次初始化" })).toBeNull();
      expect(screen.getByRole("heading", { name: "StreamVox" })).toBeTruthy();
      expect(screen.getByRole("status").textContent).toContain("语音正在准备");
      expect(screen.queryByLabelText("StreamVox 当前模型音色列表")).toBeNull();
      expect(settingsCallCount()).toBe(1);
      expect((screen.getByRole("button", { name: "任务" }) as HTMLButtonElement).disabled).toBe(true);
      expect((screen.getByRole("button", { name: "设置" }) as HTMLButtonElement).disabled).toBe(true);

      // 语音切换期间的权威设置仍要更新，否则管理表单会继续拿旧 revision 提交。
      act(() => sockets[0].emitMessage({
        type: "settings.changed",
        payload: { ...createConsoleSettingsPayload(), settings_revision: 4, user_self_intro: "语音准备期间的新资料" }
      }));
      act(() => sockets[0].emitMessage({ type: "bootstrap.status_changed", payload: createBootstrapStatusPayload(true) }));

      expect(await screen.findByLabelText("StreamVox 当前模型音色列表")).toBeTruthy();
      expect(screen.queryByRole("region", { name: "初始化完成" })).toBeNull();
      expect(settingsCallCount()).toBe(1);
      fireEvent.click(screen.getByRole("button", { name: "设置" }));
      expect(await screen.findByDisplayValue("语音准备期间的新资料")).toBeTruthy();
    });

  it("首次打开时只有语音未就绪，仍必须完成首次初始化", async () => {
      const baseFetch = createConsoleFetchMock({ isInitialized: true });
      const firstRunStatus = {
        ...createBootstrapStatusPayload(true),
        is_initialized: false,
        voice: { ...createBootstrapStatusPayload(true).voice, is_ready: false, phase: "load", has_confirmed_voice: false }
      };
      const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => (
        input.toString().endsWith("/api/bootstrap/status")
          ? Promise.resolve(new Response(JSON.stringify(firstRunStatus)))
          : baseFetch(input, init)
      ));
      vi.stubGlobal("fetch", fetchMock);
      window.history.replaceState(null, "", "/?section=streamvox");

      render(<App />);

      expect(await screen.findByRole("region", { name: "首次初始化" })).toBeTruthy();
      expect(screen.queryByRole("heading", { name: "StreamVox" })).toBeNull();
      expect(fetchMock.mock.calls.some(([input]) => input.toString().endsWith("/api/settings"))).toBe(false);
    });

  it("刷新时正在切换语音的既有用户仍回到 StreamVox 管理页", async () => {
      const baseFetch = createConsoleFetchMock({ isInitialized: true });
      const readyStatus = createBootstrapStatusPayload(true);
      const preparingStatus = {
        ...readyStatus,
        is_initialized: false,
        voice: {
          ...readyStatus.voice,
          is_ready: false,
          phase: "load",
          has_confirmed_voice: true
        }
      };
      const pendingTask = createTtsTaskFixture({
        task_id: "refresh-voice-task",
        model_id: "s2-pro-4b-gguf",
        phase: "load",
        can_cancel: true
      });
      let preparing = false;
      const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = input.toString();
        if (url.endsWith("/api/bootstrap/status")) {
          return new Response(JSON.stringify(preparing ? preparingStatus : readyStatus));
        }
        if (preparing && url.endsWith("/api/bootstrap/tts/status")) {
          return new Response(JSON.stringify(pendingTask));
        }
        return baseFetch(input, init);
      });
      vi.stubGlobal("fetch", fetchMock);
      const firstRender = render(<App />);
      expect(await screen.findByText("StreamVox")).toBeTruthy();

      firstRender.unmount();
      preparing = true;
      render(<App />);

      expect(await screen.findByRole("heading", { name: "StreamVox" })).toBeTruthy();
      expect(screen.queryByRole("region", { name: "首次初始化" })).toBeNull();
      expect(await screen.findByRole("region", { name: "语音任务" })).toBeTruthy();
      expect(screen.getByText("首次加载模型")).toBeTruthy();
    });

  it("Bootstrap 失效后淘汰旧快照，恢复时重新加载完整控制台数据", async () => {
      const sockets = installConsoleControlWebSocketMock();
      const initializedFetch = createConsoleFetchMock({ isInitialized: true });
      const uninitializedFetch = createConsoleFetchMock({ isInitialized: false });
      let isInitialized = true;
      const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => (
        isInitialized
          ? initializedFetch(input, init)
          : uninitializedFetch(input, init)
      ));
      vi.stubGlobal("fetch", fetchMock);

      render(<App />);

      expect(await screen.findByText("StreamVox")).toBeTruthy();
      expect(sockets).toHaveLength(1);
      const settingsCallCount = (): number => fetchMock.mock.calls.filter(
        ([input]) => input.toString().endsWith("/api/settings")
      ).length;
      expect(settingsCallCount()).toBe(1);

      isInitialized = false;
      act(() => {
        sockets[0].emitMessage({
          type: "bootstrap.status_changed",
          payload: createBootstrapStatusPayload(false)
        });
      });

      expect(await screen.findByRole("region", { name: "首次初始化" })).toBeTruthy();
      await waitFor(() => {
        expect(fetchMock.mock.calls.filter(
          ([input]) => input.toString().endsWith("/api/llm/config")
        ).length).toBeGreaterThanOrEqual(2);
      });
      expect(settingsCallCount()).toBe(1);

      isInitialized = true;
      act(() => {
        sockets[0].emitMessage({
          type: "bootstrap.status_changed",
          payload: createBootstrapStatusPayload(true)
        });
      });

      await waitFor(() => {
        expect(settingsCallCount()).toBe(2);
      });
      expect(await screen.findByRole("region", { name: "初始化完成" })).toBeTruthy();
      expect(screen.queryByText("StreamVox")).toBeNull();
      fireEvent.click(screen.getByRole("button", { name: "进入控制台" }));
      expect(await screen.findByText("StreamVox")).toBeTruthy();
    });

  it("Bootstrap 失效前的普通数据迟到时不能恢复旧控制台快照", async () => {
      const sockets = installConsoleControlWebSocketMock();
      const healthyFetch = createConsoleFetchMock({ isInitialized: true });
      let finishFirstSettingsRequest!: (response: Response) => void;
      const firstSettingsResponse = new Promise<Response>((resolve) => {
        finishFirstSettingsRequest = resolve;
      });
      let settingsRequestCount = 0;
      const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
        if (input.toString().endsWith("/api/settings") && init?.method !== "POST") {
          settingsRequestCount += 1;
          if (settingsRequestCount === 1) {
            return firstSettingsResponse;
          }
        }
        return healthyFetch(input, init);
      });
      vi.stubGlobal("fetch", fetchMock);

      render(<App />);

      await waitFor(() => {
        expect(settingsRequestCount).toBe(1);
        expect(sockets).toHaveLength(1);
      });
      act(() => {
        sockets[0].emitMessage({
          type: "bootstrap.status_changed",
          payload: createBootstrapStatusPayload(false)
        });
      });

      expect(await screen.findByRole("region", { name: "首次初始化" })).toBeTruthy();
      await act(async () => {
        finishFirstSettingsRequest(new Response(
          JSON.stringify(createConsoleSettingsPayload()),
          { headers: { "Content-Type": "application/json" } }
        ));
        await firstSettingsResponse;
      });

      expect(screen.getByRole("region", { name: "首次初始化" })).toBeTruthy();
      expect(screen.queryByText("StreamVox")).toBeNull();
      expect(settingsRequestCount).toBe(1);
    });

  it("Bootstrap 失效前的角色卡响应迟到时不能覆盖恢复后的新快照", async () => {
      const sockets = installConsoleControlWebSocketMock();
      const healthyFetch = createConsoleFetchMock({ isInitialized: true });
      let finishOldCardsRequest!: (response: Response) => void;
      let finishOldActiveCardRequest!: (response: Response) => void;
      const oldCardsResponse = new Promise<Response>((resolve) => {
        finishOldCardsRequest = resolve;
      });
      const oldActiveCardResponse = new Promise<Response>((resolve) => {
        finishOldActiveCardRequest = resolve;
      });
      let cardsRequestCount = 0;
      let activeCardRequestCount = 0;
      const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
        const url = input.toString();
        if (url.endsWith("/api/character-cards")) {
          cardsRequestCount += 1;
          if (cardsRequestCount === 1) return oldCardsResponse;
        }
        if (url.endsWith("/api/character-cards/active")) {
          activeCardRequestCount += 1;
          if (activeCardRequestCount === 1) return oldActiveCardResponse;
        }
        return healthyFetch(input, init);
      });
      vi.stubGlobal("fetch", fetchMock);

      render(<App />);

      fireEvent.click(await screen.findByText("角色卡"));
      await waitFor(() => {
        expect(cardsRequestCount).toBe(1);
        expect(activeCardRequestCount).toBe(1);
      });
      act(() => {
        sockets[0].emitMessage({
          type: "bootstrap.status_changed",
          payload: createBootstrapStatusPayload(false)
        });
      });
      expect(await screen.findByRole("region", { name: "首次初始化" })).toBeTruthy();

      act(() => {
        sockets[0].emitMessage({
          type: "bootstrap.status_changed",
          payload: createBootstrapStatusPayload(true)
        });
      });
      expect(await screen.findByRole("region", { name: "初始化完成" })).toBeTruthy();
      fireEvent.click(screen.getByRole("button", { name: "进入控制台" }));
      expect(await screen.findByRole("button", { name: /火花/ })).toBeTruthy();
      expect(cardsRequestCount).toBe(2);
      expect(activeCardRequestCount).toBe(2);

      const staleCard = createCharacterCardPayload();
      if (staleCard.card === null) {
        throw new Error("角色卡测试夹具不能为空");
      }
      staleCard.card.meta.name = "迟到旧角色";
      await act(async () => {
        finishOldCardsRequest(new Response(JSON.stringify({
          cards: [staleCard.card],
          active_card_id: staleCard.active_card_id,
          shared_world_canon: staleCard.shared_world_canon
        }), { headers: { "Content-Type": "application/json" } }));
        finishOldActiveCardRequest(new Response(
          JSON.stringify(staleCard),
          { headers: { "Content-Type": "application/json" } }
        ));
        await Promise.all([oldCardsResponse, oldActiveCardResponse]);
      });

      expect(screen.queryByText("迟到旧角色")).toBeNull();
      expect(screen.getByRole("button", { name: /火花/ })).toBeTruthy();
    });

  it("首屏接口失败后可点击重试重新加载", async () => {
      let shouldFailBootstrapStatus = true;
      const healthyFetchMock = createConsoleFetchMock({ isInitialized: true });
      const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        // 只让第一道门禁接口失败，其余接口保持正常，聚焦验证失败横幅的重试入口。
        if (shouldFailBootstrapStatus && input.toString().endsWith("/api/bootstrap/status")) {
          return new Response(null, { status: 500 });
        }
        return healthyFetchMock(input, init);
      });
      vi.stubGlobal("fetch", fetchMock);

      render(<App />);

      const retryButton = await screen.findByRole("button", { name: "重试" });
      expect(screen.getByText(/加载失败：/)).toBeTruthy();

      shouldFailBootstrapStatus = false;
      fireEvent.click(retryButton);

      expect(await screen.findByText("StreamVox")).toBeTruthy();
    });

  it("已初始化后可以在设置页验证并保存和风天气密钥", async () => {
      const fetchMock = createConsoleFetchMock({ isInitialized: true });
      vi.stubGlobal("fetch", fetchMock);

      render(<App />);
      fireEvent.click(await screen.findByRole("tab", { name: "AI 与服务" }));

      fireEvent.change(await screen.findByLabelText("和风天气 API Host"), {
        target: {
          value: " abc1234xyz.def.qweatherapi.com "
        }
      });
      fireEvent.change(screen.getByLabelText("和风天气 API Key"), {
        target: {
          value: " qweather-key "
        }
      });
      fireEvent.click(screen.getByRole("button", { name: "验证并保存" }));

      await waitFor(() => {
        expect(fetchMock).toHaveBeenCalledWith(
          expect.stringMatching(/\/api\/service-keys\/qweather$/),
          expect.objectContaining({
            method: "POST",
            body: JSON.stringify({
              qweather_api_key: "qweather-key",
              qweather_api_host: "abc1234xyz.def.qweatherapi.com"
            })
          })
        );
      });
    });
});
