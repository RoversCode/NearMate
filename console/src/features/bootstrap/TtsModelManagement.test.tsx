import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { TtsModelDescriptor } from "../../api/contracts/bootstrap";
import { createBootstrapStatusPayload } from "../../test/consoleAppTestHarness";
import { createTtsConfigurationFixture, createTtsTaskFixture, ttsModelsFixture } from "../../test/ttsSetupFixtures";
import { TtsSetupPanel } from "./TtsSetupPanel";

afterEach(() => vi.unstubAllGlobals());

/**
 * 在真实 HTTP 边界模拟已完成初始化的模型库，不运行实际下载。
 * Args:
 *   secondModelState: 第二个模型的本地文件状态。
 *   completesImmediately: 启动响应是否已经完成，用于覆盖离线复用已有文件。
 * Returns:
 *   记录请求的网络替身和当前已就绪的 Bootstrap 状态。
 */
function mockModelLibrary(secondModelState: TtsModelDescriptor["local_state"] = "not_downloaded", completesImmediately = false) {
  const models: TtsModelDescriptor[] = ttsModelsFixture.map((model, index) => ({
    ...model,
    local_state: index === 0 ? "downloaded" : secondModelState,
    is_current: index === 0,
    total_size_bytes: model.estimated_size_bytes,
    installed_size_bytes: index === 0 ? model.estimated_size_bytes : 0,
    integrity_status: "not_verified"
  }));
  const status = createBootstrapStatusPayload(true);
  status.voice = { ...status.voice, is_ready: true, model_id: models[0].model_id, role_name: "huohua", phase: "ready" };
  let downloadTask = createTtsTaskFixture({ phase: "idle" });
  let voiceTask = createTtsTaskFixture({ phase: "ready", model_id: models[0].model_id, role_name: "huohua", is_ready: true });
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    let payload: unknown;
    if (url.endsWith("/models")) payload = { provider: "local_streamvox", models, configured_model: models[0].model_id, download_task: downloadTask };
    else if (url.endsWith("/configuration")) payload = createTtsConfigurationFixture();
    else if (url.endsWith("/status")) payload = voiceTask;
    else if (url.endsWith("/downloads") && init?.method === "POST") {
      downloadTask = createTtsTaskFixture({ task_id: "download-1", model_id: JSON.parse(String(init.body)).model_id, phase: completesImmediately ? "completed" : "download", can_cancel: !completesImmediately });
      if (completesImmediately) models[1] = { ...models[1], local_state: "downloaded", installed_size_bytes: models[1].total_size_bytes };
      payload = downloadTask;
    } else if (url.endsWith("/downloads")) {
      models[1] = { ...models[1], local_state: "downloaded", installed_size_bytes: models[1].total_size_bytes };
      downloadTask = { ...downloadTask, phase: "completed", can_cancel: false };
      payload = downloadTask;
    } else if (url.endsWith("/tasks")) {
      voiceTask = createTtsTaskFixture({ task_id: "voice-2", model_id: JSON.parse(String(init?.body)).model_id, phase: "load", can_cancel: true });
      payload = voiceTask;
    } else throw new Error(`未预期的模型管理请求：${url}`);
    return new Response(JSON.stringify(payload), { headers: { "Content-Type": "application/json" } });
  });
  vi.stubGlobal("fetch", fetchMock);
  return { fetchMock, status };
}

describe("已初始化后的语音模型管理", () => {
  it("逐个显示已下载、未下载和当前使用，不再次要求下载当前模型", async () => {
    const { status } = mockModelLibrary();
    render(<TtsSetupPanel baseUrl="http://localhost" status={status} onStatusChange={vi.fn()} />);
    const currentModel = await screen.findByRole("article", { name: "Qwen3 TTS 0.6B" });
    expect(within(currentModel).getByText("已下载")).toBeTruthy();
    expect(within(currentModel).getByRole("button", { name: "当前使用" }).hasAttribute("disabled")).toBe(true);
    expect(within(currentModel).queryByRole("button", { name: /下载/ })).toBeNull();
    expect(within(screen.getByRole("article", { name: "MOSS TTS Nano" })).getByText("未下载")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "下载并准备语音" })).toBeNull();
  });

  it("下载其他模型只请求文件下载，不启动语音切换或提交初始化状态", async () => {
    const { fetchMock, status } = mockModelLibrary();
    const onStatusChange = vi.fn();
    render(<TtsSetupPanel baseUrl="http://localhost" status={status} onStatusChange={onStatusChange} />);
    fireEvent.click(await screen.findByRole("button", { name: "下载 MOSS TTS Nano" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("http://localhost/api/bootstrap/tts/downloads", expect.objectContaining({ method: "POST", body: JSON.stringify({ model_id: "moss-tts-nano-onnx" }) })));
    expect(fetchMock.mock.calls.some(([url, init]) => String(url).endsWith("/tasks") && init?.method === "POST")).toBe(false);
    expect(onStatusChange).not.toHaveBeenCalled();
  });

  it("下载完成后刷新模型库，并开放准备试听入口", async () => {
    const { status } = mockModelLibrary();
    render(<TtsSetupPanel baseUrl="http://localhost" status={status} onStatusChange={vi.fn()} />);
    fireEvent.click(await screen.findByRole("button", { name: "下载 MOSS TTS Nano" }));
    const prepare = await screen.findByRole("button", { name: "准备并试听 MOSS TTS Nano" }, { timeout: 3000 });
    expect(prepare.hasAttribute("disabled")).toBe(false);
    expect(within(screen.getByRole("article", { name: "MOSS TTS Nano" })).getByText("已下载")).toBeTruthy();
  });

  it("只有明确准备另一个已下载模型时才启动语音配置任务", async () => {
    const { fetchMock, status } = mockModelLibrary("downloaded");
    render(<TtsSetupPanel baseUrl="http://localhost" status={status} onStatusChange={vi.fn()} />);
    fireEvent.click(await screen.findByRole("button", { name: "准备并试听 MOSS TTS Nano" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("http://localhost/api/bootstrap/tts/tasks", expect.objectContaining({ method: "POST", body: JSON.stringify({ model_id: "moss-tts-nano-onnx" }) })));
    expect(fetchMock.mock.calls.some(([url, init]) => String(url).endsWith("/downloads") && init?.method === "POST")).toBe(false);
  });

  it("启动响应已完成时也重读磁盘清单，不依赖后续轮询", async () => {
    const { status, fetchMock } = mockModelLibrary("not_downloaded", true);
    render(<TtsSetupPanel baseUrl="http://localhost" status={status} onStatusChange={vi.fn()} />);
    fireEvent.click(await screen.findByRole("button", { name: "下载 MOSS TTS Nano" }));
    expect((await screen.findByRole("button", { name: "准备并试听 MOSS TTS Nano" })).hasAttribute("disabled")).toBe(false);
    expect(fetchMock.mock.calls.filter(([url]) => String(url).endsWith("/models"))).toHaveLength(2);
    expect(fetchMock.mock.calls.some(([url, init]) => String(url).endsWith("/downloads") && init?.method !== "POST")).toBe(false);
  });

  it("缺少库存字段时显示状态未知，不把未知当成未下载", async () => {
    const { status } = mockModelLibrary("unknown");
    render(<TtsSetupPanel baseUrl="http://localhost" status={status} onStatusChange={vi.fn()} />);
    const model = await screen.findByRole("article", { name: "MOSS TTS Nano" });
    expect(within(model).getByText("下载状态待确认")).toBeTruthy();
    expect(within(model).queryByText("未下载")).toBeNull();
  });
});
