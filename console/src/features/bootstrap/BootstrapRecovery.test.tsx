import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { BootstrapStatus, TtsSetupTask } from "../../api/contracts/bootstrap";
import type { LlmConfig, SupportedLlmModelsResponse } from "../../api/contracts/llm";
import type { TtsCapabilities } from "../../api/contracts/ttsSetup";
import { createBootstrapStatusPayload, createLlmConfigPayload, createSupportedLlmModelsPayload } from "../../test/consoleAppTestHarness";
import { createTtsConfigurationFixture, createTtsTaskFixture, ttsCapabilitiesFixture, ttsModelsFixture } from "../../test/ttsSetupFixtures";
import { BootstrapSetupPage } from "./BootstrapSetupPage";
import { TtsExecutionPanel } from "./TtsExecutionPanel";
import { useTtsSetupController } from "./useTtsSetupController";

afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });

describe("首次引导的资格失效恢复", () => {
  it("重新检测判定已保存的运行方式不可用时，先采用可用建议并保存，再进入下一步", async () => {
    const onContinue = vi.fn();
    let config = createTtsConfigurationFixture(true);
    const newCapabilities: TtsCapabilities = {
      backends: ttsCapabilitiesFixture.backends.map(item => ({ ...item, available: item.backend === "cpu" })),
      recommendation: { onnx_provider: "cpu", llama_backend: "cpu" }
    };
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      let result: unknown;
      if (path.endsWith("/models")) result = { models: ttsModelsFixture, configured_model: null, provider: "local_streamvox" };
      else if (path.endsWith("/configuration")) {
        if (init?.method === "PUT") config = { ...config, selection: newCapabilities.recommendation, capabilities: newCapabilities };
        result = config;
      } else if (path.endsWith("/probe")) result = newCapabilities;
      else if (path.endsWith("/status")) result = createTtsTaskFixture();
      else throw new Error(`未预期的请求：${path}`);
      return new Response(JSON.stringify(result));
    });
    vi.stubGlobal("fetch", fetchMock);

    /** 让检测面板使用生产控制器，覆盖保存资格与页面继续动作的接线。 */
    function ExecutionStep(): JSX.Element {
      const controller = useTtsSetupController("http://localhost", vi.fn());
      return <TtsExecutionPanel controller={controller} guided onContinue={onContinue} />;
    }
    render(<ExecutionStep />);
    const probe = await screen.findByRole("button", { name: "重新检测电脑" });
    await waitFor(() => expect((probe as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(probe);
    await screen.findByText("上次选择的运行方式在本次检测中不可用，请使用检测建议或在高级设置中重新选择。");
    const next = screen.getByRole("button", { name: "下一步：选择语音模型" });
    expect((next as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(next);
    expect(onContinue).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "使用检测建议" }));
    const save = screen.getByRole("button", { name: "使用此设置并继续" });
    expect((save as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(save);
    await waitFor(() => expect(onContinue).toHaveBeenCalledOnce());
    const savedRequest = fetchMock.mock.calls.find(([, init]) => init?.method === "PUT");
    expect(JSON.parse(String(savedRequest?.[1]?.body)).selection).toEqual(newCapabilities.recommendation);
  });

  it.each(["preview", "confirm"] as const)("%s 拒绝旧试听后刷新真实状态，返回模型步骤并允许重试", async operation => {
    const failure = "角色素材或执行方式已变化，请重新准备语音";
    let task: TtsSetupTask = createTtsTaskFixture({
      task_id: "task-a", model_id: ttsModelsFixture[0].model_id,
      phase: "awaiting_confirmation", can_confirm: true, can_cancel: true
    });
    vi.stubGlobal("URL", class extends URL {
      static createObjectURL = vi.fn(() => "blob:test");
      static revokeObjectURL = vi.fn();
    });
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path.endsWith(`/${operation}`)) {
        // GET status 会公开后端已失效的资格，旧的 can_confirm 不能继续用于导航。
        task = { ...task, phase: "repair_required", can_confirm: false, can_cancel: false, can_retry: true, error: failure };
        return new Response(JSON.stringify({ detail: failure }), { status: 409, headers: { "Content-Type": "application/json" } });
      }
      if (path.endsWith("/preview")) return new Response(new Blob(["wave"], { type: "audio/wav" }));
      let result: unknown;
      if (path.endsWith("/models")) result = { models: ttsModelsFixture, configured_model: task.model_id, provider: "local_streamvox" };
      else if (path.endsWith("/configuration")) result = createTtsConfigurationFixture(true);
      else if (path.endsWith("/status")) result = task;
      else if (path.endsWith("/retry") && init?.method === "POST") {
        task = { ...task, task_id: "task-b", phase: "load", can_retry: false, can_cancel: true, error: null };
        result = task;
      } else throw new Error(`未预期的请求：${path}`);
      return new Response(JSON.stringify(result));
    });
    vi.stubGlobal("fetch", fetchMock);
    const status: BootstrapStatus = {
      ...createBootstrapStatusPayload(false),
      llm: { is_ready: true, missing: [], errors: [] },
      voice: { provider: "local_streamvox", is_ready: false, phase: "awaiting_confirmation", model_id: task.model_id, role_name: "huohua", has_confirmed_voice: false }
    };
    render(<BootstrapSetupPage baseUrl="http://localhost" status={status}
      llmConfig={createLlmConfigPayload(true) as LlmConfig}
      supportedLlmModels={createSupportedLlmModelsPayload() as SupportedLlmModelsResponse}
      isSaving={false} errorMessage={null} onLlmConfigSubmit={vi.fn()} onStatusChange={vi.fn()} onComplete={vi.fn()} />);
    if (operation === "confirm") {
      const audio = await screen.findByLabelText("角色语音试听");
      fireEvent.ended(audio);
      const confirm = screen.getByRole("button", { name: "声音没问题，完成设置" });
      await waitFor(() => expect((confirm as HTMLButtonElement).disabled).toBe(false));
      fireEvent.click(confirm);
    }
    await screen.findByText("语音配置需要重新确认");
    expect(screen.queryByRole("button", { name: "声音没问题，完成设置" })).toBeNull();
    expect(screen.queryByRole("button", { name: "下一步：试听声音" })).toBeNull();
    expect(screen.getByRole("button", { name: "重新选择模型" })).toBeTruthy();
    const retry = screen.getByRole("button", { name: "重新尝试" });
    await waitFor(() => expect((retry as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(retry);
    await screen.findByText("首次加载模型");
    expect(fetchMock.mock.calls.filter(([path]) => String(path).endsWith("/task-a/retry"))).toHaveLength(1);
  });
});
