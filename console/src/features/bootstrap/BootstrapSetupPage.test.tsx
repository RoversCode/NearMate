import { useState } from "react";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { BootstrapStatus } from "../../api/contracts/bootstrap";
import type { LlmConfig, SupportedLlmModelsResponse } from "../../api/contracts/llm";
import { createBootstrapStatusPayload, createLlmConfigPayload, createSupportedLlmModelsPayload } from "../../test/consoleAppTestHarness";
import { createTtsConfigurationFixture, createTtsTaskFixture, ttsCapabilitiesFixture, ttsModelsFixture } from "../../test/ttsSetupFixtures";
import { BootstrapSetupPage } from "./BootstrapSetupPage";

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

/**
 * 用真实控制器和响应解析覆盖向导，只替换网络与音频对象边界。
 * Args:
 *   confirmed: 是否已有保存的设备配置。
 *   initialTask: 重开页面时服务端持有的任务。
 * Returns:
 *   网络调用记录，以及可注入一次性失败的测试状态。
 */
function installService(confirmed = false, initialTask = createTtsTaskFixture()) {
  let configuration = createTtsConfigurationFixture(confirmed);
  let task = initialTask;
  const failures = { read: false, save: false, preview: false };
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    let payload: unknown;
    if (url.endsWith("/models")) payload = { provider: "local_streamvox", models: ttsModelsFixture, configured_model: task.model_id };
    else if (url.endsWith("/configuration")) {
      if (init?.method === "PUT") {
        if (failures.save) return new Response(JSON.stringify({ detail: "该目录不可写" }), { status: 409, headers: { "Content-Type": "application/json" } });
        configuration = { ...JSON.parse(String(init.body)), capabilities: ttsCapabilitiesFixture, confirmed: true };
        task = createTtsTaskFixture();
      }
      payload = configuration;
    } else if (url.endsWith("/probe")) payload = ttsCapabilitiesFixture;
    else if (url.endsWith("/status")) {
      if (failures.read) throw new Error("本地服务暂时断开");
      payload = task;
    } else if (url.endsWith("/tasks")) {
      task = createTtsTaskFixture({ task_id: "voice-1", model_id: JSON.parse(String(init?.body)).model_id, phase: "awaiting_confirmation", can_confirm: true, can_cancel: true });
      payload = task;
    } else if (url.endsWith("/preview")) {
      if (failures.preview) throw new Error("试听暂时不可读");
      return new Response(new Blob(["wave"], { type: "audio/wav" }));
    } else if (url.endsWith("/confirm")) {
      task = { ...task, phase: "ready", can_confirm: false, can_cancel: false };
      payload = createBootstrapStatusPayload(true);
    } else if (url.endsWith("/retry")) {
      task = { ...task, phase: "download", error: null, can_retry: false, can_cancel: true };
      payload = task;
    } else throw new Error(`未预期请求：${url}`);
    return new Response(JSON.stringify(payload), { headers: { "Content-Type": "application/json" } });
  });
  vi.stubGlobal("fetch", fetchMock);
  const OriginalUrl = URL;
  let objectSequence = 0;
  vi.stubGlobal("URL", class extends OriginalUrl {
    static createObjectURL = vi.fn(() => `blob:voice-${++objectSequence}`);
    static revokeObjectURL = vi.fn();
  });
  return { fetchMock, failures };
}

function Journey({ initialStatus, warningMessage, onComplete = vi.fn() }: { initialStatus?: BootstrapStatus; warningMessage?: string; onComplete?: () => void }) {
  const [status, setStatus] = useState(initialStatus ?? { ...createBootstrapStatusPayload(false), llm: { is_ready: true, missing: [], errors: [] } });
  return <BootstrapSetupPage baseUrl="http://localhost" status={status} llmConfig={createLlmConfigPayload(true) as LlmConfig}
    supportedLlmModels={createSupportedLlmModelsPayload() as SupportedLlmModelsResponse} isSaving={false} errorMessage={null}
    warningMessage={warningMessage}
    onLlmConfigSubmit={async () => true} onStatusChange={setStatus} onComplete={onComplete} />;
}

describe("首次初始化分步引导", () => {
  it("五步进度共享名称，每次凭据屏只显示一家且不提前保存", async () => {
    const { fetchMock } = installService();
    const onSubmit = vi.fn().mockResolvedValue(false);
    const config = createLlmConfigPayload(true) as LlmConfig;
    config.credentials.deepseek.api_key_configured = false;
    render(<BootstrapSetupPage baseUrl="http://localhost" status={createBootstrapStatusPayload(false)}
      llmConfig={config} supportedLlmModels={createSupportedLlmModelsPayload() as SupportedLlmModelsResponse}
      isSaving={false} errorMessage={null} onLlmConfigSubmit={onSubmit} onStatusChange={vi.fn()} onComplete={vi.fn()} />);
    await screen.findByRole("heading", { name: "第 1 步，共 5 步" });
    expect(screen.getByRole("list", { name: "初始化步骤" }).children).toHaveLength(5);
    expect(screen.queryByLabelText("Qwen API Key")).toBeNull();
    fireEvent.change(screen.getByLabelText("DeepSeek API Key"), { target: { value: "deepseek-key" } });
    fireEvent.click(screen.getByRole("button", { name: "下一步：连接 Qwen" }));
    expect(screen.getByRole("heading", { name: "第 2 步，共 5 步" })).toBeTruthy();
    expect(screen.queryByLabelText("DeepSeek API Key")).toBeNull();
    expect(onSubmit).not.toHaveBeenCalled();
    expect(fetchMock.mock.calls.some(([, init]) => init?.method === "PUT")).toBe(false);
  });

  it("写入成功但状态回读失败时展示状态提醒而不是初始化失败", async () => {
    installService(true);
    render(<Journey warningMessage="已保存，但页面状态刷新失败：初始化状态暂时不可用" />);

    expect((await screen.findByRole("status")).textContent).toContain("已保存，但页面状态刷新失败");
    expect(screen.queryByText("初始化失败：已保存，但页面状态刷新失败：初始化状态暂时不可用")).toBeNull();
  });

  it("重开未完成的 AI 配置时直接补 Qwen，第一家已保存的 Key 无需重新填写", async () => {
    installService();
    const config = createLlmConfigPayload(true) as LlmConfig;
    const props = { baseUrl: "http://localhost", status: createBootstrapStatusPayload(false), llmConfig: config,
      supportedLlmModels: createSupportedLlmModelsPayload() as SupportedLlmModelsResponse,
      isSaving: false, errorMessage: null, onLlmConfigSubmit: vi.fn().mockResolvedValue(false),
      onStatusChange: vi.fn(), onComplete: vi.fn() };
    const firstPage = render(<BootstrapSetupPage {...props} />);
    await screen.findByRole("heading", { name: "第 2 步，共 5 步" });
    fireEvent.change(screen.getByLabelText("Qwen API Key"), { target: { value: "unsaved-key" } });
    firstPage.unmount();
    render(<BootstrapSetupPage {...props} />);
    await screen.findByRole("heading", { name: "第 2 步，共 5 步" });
    expect((screen.getByLabelText("Qwen API Key") as HTMLInputElement).value).toBe("");
    fireEvent.click(screen.getByRole("button", { name: "上一步：连接 DeepSeek" }));
    expect((screen.getByLabelText("DeepSeek API Key") as HTMLInputElement).value).toBe("");
    expect((screen.getByRole("button", { name: "下一步：连接 Qwen" }) as HTMLButtonElement).disabled).toBe(false);
  });

  it("设备检测仅填草稿，用户确认后才保存；返回查看保留未提交的 AI 输入", async () => {
    const { fetchMock } = installService();
    render(<Journey />);
    await screen.findByRole("heading", { name: "让语音适合这台电脑" });
    expect(screen.getByRole("link", { name: "查看本步完整教程" }).getAttribute("href")).toContain("#check-computer");
    expect(screen.queryByLabelText("和风天气 API Key")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "上一步：连接 Qwen" }));
    const keyInput = screen.getByLabelText("Qwen API Key") as HTMLInputElement;
    fireEvent.change(keyInput, { target: { value: "unsaved-draft" } });
    fireEvent.click(screen.getByRole("button", { name: "使用已保存的 AI 配置继续" }));
    fireEvent.click(screen.getByRole("button", { name: "检测我的电脑" }));
    await waitFor(() => expect((screen.getByRole("button", { name: "使用此设置并继续" }) as HTMLButtonElement).disabled).toBe(false));
    expect(fetchMock.mock.calls.some(([, init]) => init?.method === "PUT")).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: "上一步：连接 Qwen" }));
    expect((screen.getByLabelText("Qwen API Key") as HTMLInputElement).value).toBe("unsaved-draft");
    fireEvent.click(screen.getByRole("button", { name: "使用已保存的 AI 配置继续" }));
    fireEvent.click(screen.getByRole("button", { name: "使用此设置并继续" }));
    await screen.findByRole("heading", { name: "为角色准备本地语音" });
    expect(screen.getByRole("link", { name: "查看本步完整教程" }).getAttribute("href")).toContain("#voice-model");
    expect(fetchMock.mock.calls.filter(([, init]) => init?.method === "PUT")).toHaveLength(1);
    expect(screen.getAllByRole("radio").every(input => !(input as HTMLInputElement).checked)).toBe(true);
    expect((screen.getByRole("button", { name: "下载并准备语音" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("设备配置保存失败停留原步，保留路径并允许修正", async () => {
    const { failures } = installService(); failures.save = true;
    render(<Journey />);
    fireEvent.click(await screen.findByRole("button", { name: "检测我的电脑" }));
    await waitFor(() => expect((screen.getByRole("button", { name: "使用此设置并继续" }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.change(screen.getByLabelText("模型保存目录"), { target: { value: "E:\\My Voice" } });
    fireEvent.click(screen.getByRole("button", { name: "使用此设置并继续" }));
    await screen.findByText(/该目录不可写/);
    expect(screen.getByRole("heading", { name: "让语音适合这台电脑" })).toBeTruthy();
    expect((screen.getByLabelText("模型保存目录") as HTMLInputElement).value).toBe("E:\\My Voice");
    failures.save = false;
    fireEvent.click(screen.getByRole("button", { name: "使用此设置并继续" }));
    await screen.findByRole("heading", { name: "为角色准备本地语音" });
  });

  it("恢复正在运行的任务，阻止返回修改和同时启动新任务", async () => {
    installService(true, createTtsTaskFixture({ task_id: "existing", phase: "download", model_id: ttsModelsFixture[0].model_id, can_cancel: true }));
    render(<Journey />);
    await screen.findByText("下载语音模型");
    expect((screen.getByRole("button", { name: "上一步：检查电脑" }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.queryByRole("button", { name: "下载并准备语音" })).toBeNull();
    expect(screen.getByRole("button", { name: "取消准备" })).toBeTruthy();
  });

  it("缺失安装组件给出更新建议，重试沿用原任务", async () => {
    const { fetchMock } = installService(true, createTtsTaskFixture({ task_id: "failed-task", phase: "failed", model_id: ttsModelsFixture[0].model_id, can_retry: true, error: "No module named 'numpy.testing'" }));
    render(<Journey />);
    await screen.findByText("安装组件缺失或无法加载");
    expect(screen.getByText(/重复下载语音模型不能解决组件缺失/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "已更新安装包，重新尝试" }));
    await screen.findByText("下载语音模型");
    expect(fetchMock).toHaveBeenCalledWith("http://localhost/api/bootstrap/tts/tasks/failed-task/retry", expect.objectContaining({ method: "POST" }));
  });

  it("初始读取失败可恢复，不把暂时不可达当成需要重新设置", async () => {
    const { failures } = installService(true); failures.read = true;
    render(<Journey />);
    await screen.findByText("暂时无法读取语音准备进度，已保存的配置不会被清空。");
    failures.read = false;
    fireEvent.click(screen.getByRole("button", { name: "重新读取进度" }));
    await screen.findByRole("heading", { name: "为角色准备本地语音" });
  });

  it("恢复试听只取一次音频，听完才可确认，播放失败不会放行；完成后主动进入控制台", async () => {
    const { fetchMock } = installService(true, createTtsTaskFixture({ task_id: "preview-task", phase: "awaiting_confirmation", model_id: ttsModelsFixture[0].model_id, can_confirm: true }));
    const onComplete = vi.fn();
    render(<Journey onComplete={onComplete} />);
    const audio = await screen.findByLabelText("角色语音试听", { selector: "audio" });
    expect(screen.getByRole("link", { name: "查看本步完整教程" }).getAttribute("href")).toContain("#preview");
    await act(async () => {});
    expect(fetchMock.mock.calls.filter(([url]) => String(url).endsWith("/preview"))).toHaveLength(1);
    const confirm = screen.getByRole("button", { name: "声音没问题，完成设置" }) as HTMLButtonElement;
    fireEvent.play(audio); expect(confirm.disabled).toBe(true);
    fireEvent.ended(audio); expect(confirm.disabled).toBe(false);
    fireEvent.error(audio); expect(confirm.disabled).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "重新加载试听" }));
    await waitFor(() => expect(fetchMock.mock.calls.filter(([url]) => String(url).endsWith("/preview"))).toHaveLength(2));
    await act(async () => {});
    expect(confirm.disabled).toBe(true);
    fireEvent.ended(screen.getByLabelText("角色语音试听", { selector: "audio" }));
    fireEvent.click(confirm);
    await screen.findByRole("heading", { name: "准备好了，开始认识你的伙伴吧" });
    expect(screen.getByRole("link", { name: "发送第一条消息" }).getAttribute("href")).toContain("chat-and-voice.html#first-message");
    expect(screen.getByRole("link", { name: "语音使用入门" }).getAttribute("href")).toContain("chat-and-voice.html#voice");
    expect(onComplete).not.toHaveBeenCalled();
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "进入控制台" })));
    expect(onComplete).toHaveBeenCalledOnce();
  });

  it("修改设备配置会淘汰旧试听，必须重新准备模型", async () => {
    const { fetchMock } = installService(true, createTtsTaskFixture({ task_id: "old-preview", phase: "awaiting_confirmation", model_id: ttsModelsFixture[0].model_id, can_confirm: true }));
    render(<Journey />);
    await screen.findByLabelText("角色语音试听", { selector: "audio" });
    await waitFor(() => expect((screen.getByRole("button", { name: "返回查看语音准备" }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByRole("button", { name: "返回查看语音准备" }));
    fireEvent.click(screen.getByRole("button", { name: "上一步：检查电脑" }));
    fireEvent.click(screen.getByRole("button", { name: "重新检测电脑" }));
    await waitFor(() => expect((screen.getByRole("button", { name: "下一步：选择语音模型" }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.change(screen.getByLabelText("模型保存目录"), { target: { value: "E:\\New Models" } });
    fireEvent.click(screen.getByRole("button", { name: "使用此设置并继续" }));
    await screen.findByRole("heading", { name: "为角色准备本地语音" });
    expect(screen.queryByRole("button", { name: "下一步：试听声音" })).toBeNull();
    expect(screen.queryByLabelText("角色语音试听", { selector: "audio" })).toBeNull();
    expect(fetchMock.mock.calls.some(([url]) => String(url).endsWith("/confirm"))).toBe(false);
  });
});
