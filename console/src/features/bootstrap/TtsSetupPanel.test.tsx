import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { TtsSetupPanel } from "./TtsSetupPanel";
import { TtsTaskProgress } from "./TtsTaskProgress";
import { createBootstrapStatusPayload } from "../../test/consoleAppTestHarness";
import { createTtsConfigurationFixture, createTtsTaskFixture, ttsCapabilitiesFixture, ttsModelsFixture } from "../../test/ttsSetupFixtures";
import type { TtsSetupTask } from "../../api/contracts/bootstrap";
import { confirmTtsSetup } from "../../api/services/bootstrap";

afterEach(() => vi.unstubAllGlobals());

/**
 * 用真实 HTTP 服务函数驱动表单，只替换网络边界。
 * Args:
 *   configured: 是否已有明确执行配置。
 *   initialTask: 页面加载时已有的受管任务。
 *   statusSequence: 依次返回的任务状态，用于模拟真实阶段交接。
 * Returns:
 *   可核对请求体的 fetch 替身。
 */
function mockService(configured = false, initialTask = createTtsTaskFixture(), statusSequence: TtsSetupTask[] = []) {
  let config = createTtsConfigurationFixture(configured);
  let task = initialTask;
  let downloadTask = createTtsTaskFixture({ phase: "idle" });
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    let result: unknown;
    if (url.endsWith("/models")) result = { provider: "local_streamvox", models: ttsModelsFixture.map((model, index) => ({ ...model, local_state: index === 0 ? "downloaded" : "not_downloaded" })), configured_model: configured ? ttsModelsFixture[0].model_id : null, download_task: downloadTask };
    else if (url.endsWith("/configuration")) {
      if (init?.method === "PUT") config = { ...JSON.parse(String(init.body)), confirmed: true, capabilities: ttsCapabilitiesFixture };
      result = config;
    } else if (url.endsWith("/probe")) result = ttsCapabilitiesFixture;
    else if (url.endsWith("/status")) result = statusSequence.shift() ?? task;
    else if (url.endsWith("/downloads")) {
      if (init?.method === "POST") downloadTask = createTtsTaskFixture({ task_id: "download-new", model_id: JSON.parse(String(init.body)).model_id, phase: "download", can_cancel: true, progress: { phase: "download", completed_bytes: 128, total_bytes: 1024 } });
      result = downloadTask;
    }
    else if (url.endsWith("/tasks")) { task = createTtsTaskFixture({ task_id: "task-new", model_id: ttsModelsFixture[0].model_id, phase: "download", can_cancel: true, progress: { phase: "download", completed_bytes: 128, total_bytes: 1024 } }); result = task; }
    else if (url.endsWith("/cancel")) { task = { ...task, phase: "cancelled", can_cancel: false, can_retry: true }; result = task; }
    else if (url.endsWith("/retry")) { task = { ...task, phase: "load", can_retry: false, can_cancel: true }; result = task; }
    else if (url.endsWith("/preview")) return new Response(new Blob(["wave"], { type: "audio/wav" }));
    else if (url.endsWith("/confirm")) { task = { ...task, phase: "ready", can_confirm: false, can_cancel: false }; result = createBootstrapStatusPayload(true); }
    else throw new Error(`未预期的语音请求：${url}`);
    return new Response(JSON.stringify(result), { headers: { "Content-Type": "application/json" } });
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

describe("统一语音准备面板", () => {
  it("不自动准备首模型，检测后也必须明确确认执行方式和绝对目录", async () => {
    const fetchMock = mockService();
    render(<TtsSetupPanel baseUrl="http://localhost" status={createBootstrapStatusPayload(false)} onStatusChange={vi.fn()} />);
    await waitFor(() => expect((screen.getByRole("button", { name: "检测本机执行方式" }) as HTMLButtonElement).disabled).toBe(false));
    expect((screen.getByRole("button", { name: "下载 MOSS TTS Nano" }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "检测本机执行方式" }));
    await screen.findByRole("button", { name: "采用检测建议" });
    expect((screen.getByLabelText("ONNX 执行方式") as HTMLSelectElement).value).toBe("");
    expect(screen.getByText(/缺少 CUDA Runtime/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "采用检测建议" }));
    fireEvent.change(screen.getByLabelText("模型保存目录"), { target: { value: "relative/models" } });
    expect((screen.getByRole("button", { name: "确认执行方式与目录" }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.change(screen.getByLabelText("模型保存目录"), { target: { value: "E:\\NearMate Models" } });
    fireEvent.click(screen.getByRole("button", { name: "确认执行方式与目录" }));
    await screen.findByRole("button", { name: "执行方式与目录已确认" });
    expect(fetchMock).toHaveBeenCalledWith("http://localhost/api/bootstrap/tts/configuration", expect.objectContaining({ method: "PUT", body: JSON.stringify({ schema_version: 1, selection: { onnx_provider: "directml", llama_backend: "vulkan" }, model_directory: "E:\\NearMate Models" }) }));
    expect(fetchMock.mock.calls.some(([url]) => String(url).endsWith("/tasks"))).toBe(false);
    expect(screen.getByText(/2.00 GiB/)).toBeTruthy();
    expect(screen.getByText(/音频解码固定使用 CPU/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "下载 MOSS TTS Nano" }));
    await screen.findByText("下载语音模型");
  });

  it("下载数字不泄漏到校验、加载、音色和试听阶段", () => {
    const task = createTtsTaskFixture({ task_id: "task", phase: "verify", progress: { phase: "download", completed_bytes: 128, total_bytes: 256, fraction: 0.5 } });
    const view = render(<TtsTaskProgress task={task} />);
    for (const phase of ["verify", "publish", "load", "prepare_voice", "preview", "release", "awaiting_confirmation"]) {
      view.rerender(<TtsTaskProgress task={{ ...task, phase }} />);
      expect(screen.queryByText(/^已下载 /)).toBeNull();
      expect(screen.queryByText(/50%/)).toBeNull();
    }
    view.rerender(<TtsTaskProgress task={{ ...task, phase: "download" }} />);
    expect(screen.getByText(/已下载 128 字节/)).toBeTruthy();
  });

  it.each([
    ["publish", "保存已验证的模型"],
    ["release", "正在结束当前任务"],
    ["awaiting_confirmation", "语音已准备好，可以试听了"],
  ])("%s 阶段持续轮询直到临时资源释放后允许试听", async (phase, label) => {
    const initial = createTtsTaskFixture({ task_id: "transition-task", phase, can_cancel: true });
    const completed = { ...initial, phase: "awaiting_confirmation", can_confirm: true };
    const fetchMock = mockService(true, initial, [initial, completed]);
    render(<TtsSetupPanel baseUrl="http://localhost" status={createBootstrapStatusPayload(false)} onStatusChange={vi.fn()} />);
    await screen.findByText(label);
    expect((screen.getByRole("button", { name: "下载 MOSS TTS Nano" }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.queryByRole("button", { name: "加载并恢复试听" })).toBeNull();
    await screen.findByRole("button", { name: "加载并恢复试听" }, { timeout: 2500 });
    expect(fetchMock.mock.calls.filter(([url]) => String(url).endsWith("/status"))).toHaveLength(2);
  });

  it("重新打开页面恢复现有试听，播放后才允许提交确认", async () => {
    const fetchMock = mockService(true, createTtsTaskFixture({ task_id: "saved-task", model_id: ttsModelsFixture[0].model_id, phase: "awaiting_confirmation", can_confirm: true, can_cancel: true }));
    const onStatusChange = vi.fn();
    const revokeObjectUrl = vi.fn();
    const NativeUrl = URL;
    vi.stubGlobal("URL", class extends NativeUrl {
      static createObjectURL = vi.fn(() => "blob:preview");
      static revokeObjectURL = revokeObjectUrl;
    });
    const view = render(<TtsSetupPanel baseUrl="http://localhost" status={createBootstrapStatusPayload(false)} onStatusChange={onStatusChange} />);
    fireEvent.click(await screen.findByRole("button", { name: "加载并恢复试听" }));
    const audio = await screen.findByLabelText("角色语音试听");
    expect((screen.getByRole("button", { name: "确认并使用这套音色" }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.ended(audio);
    fireEvent.click(screen.getByRole("button", { name: "确认并使用这套音色" }));
    await waitFor(() => expect(onStatusChange).toHaveBeenCalledWith(createBootstrapStatusPayload(true)));
    expect(fetchMock.mock.calls.some(([url]) => String(url).endsWith("/tasks"))).toBe(false);
    view.unmount(); expect(revokeObjectUrl).toHaveBeenCalledWith("blob:preview");
  });

  it("失败任务能沿已有任务编号重试，不隐式创建首模型任务", async () => {
    const fetchMock = mockService(true, createTtsTaskFixture({ task_id: "failed-task", model_id: ttsModelsFixture[0].model_id, phase: "interrupted", can_retry: true, error: "上次准备被中断" }));
    render(<TtsSetupPanel baseUrl="http://localhost" status={createBootstrapStatusPayload(false)} onStatusChange={vi.fn()} />);
    fireEvent.click(await screen.findByRole("button", { name: "重新尝试" }));
    await screen.findByText("首次加载模型");
    expect(fetchMock).toHaveBeenCalledWith("http://localhost/api/bootstrap/tts/tasks/failed-task/retry", expect.objectContaining({ method: "POST" }));
  });

  it("确认接口缺少完整 Bootstrap 状态时拒绝成功", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ phase: "ready", is_ready: true } satisfies Partial<TtsSetupTask>))));
    await expect(confirmTtsSetup("http://localhost", "task")).rejects.toThrow("完整初始化状态");
  });
});
