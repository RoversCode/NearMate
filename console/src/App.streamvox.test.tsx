import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, it, expect, vi } from "vitest";
import { App } from "./App";

// 此套用例覆盖普通控制台的领域行为，运行时边界由 RuntimeInitializationGate 测试单独验证。
vi.mock("./features/runtime-initialization/RuntimeInitializationGate", () => ({
  RuntimeInitializationGate: ({ children }: { children: import("react").ReactNode }) => children
}));
import {
  createConsoleSettingsPayload,
  createShortcutStatusResponse,
  createWakeResponseConfigPayload,
  createBootstrapStatusPayload,
  createSupportedLlmModelsPayload,
  createLlmConfigPayload,
  createStreamVoxRuntimePayload,
  createConsoleFetchMock,
  installConsoleControlWebSocketMock,
  resetConsoleAppTestEnvironment
} from "./test/consoleAppTestHarness";
import { createTtsTaskFixture, ttsModelsFixture } from "./test/ttsSetupFixtures";

afterEach(resetConsoleAppTestEnvironment);

describe("console app StreamVox", () => {
  it("创建已有音色 HTTP 失败保留名称、文本和文件，修改名称即可重试", async () => {
    const originalCreateObjectUrl = URL.createObjectURL;
    const originalRevokeObjectUrl = URL.revokeObjectURL;
    URL.createObjectURL = vi.fn(() => "blob:reference-audio");
    URL.revokeObjectURL = vi.fn();
    const defaultFetch = createConsoleFetchMock();
    const createRequests: FormData[] = [];
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      if (input.toString().endsWith("/api/streamvox/roles") && init?.method === "POST") {
        createRequests.push(init.body as FormData);
        if (createRequests.length === 1) {
          return Promise.resolve(new Response(JSON.stringify({ detail: "StreamVox role already exists: demo_role" }), {
            status: 400, headers: { "Content-Type": "application/json" }
          }));
        }
      }
      return defaultFetch(input, init);
    }));

    try {
      render(<App />);
      fireEvent.click(await screen.findByText("StreamVox"));
      fireEvent.click(await screen.findByText("新建音色"));
      const nameInput = screen.getByLabelText("新音色名称") as HTMLInputElement;
      const textInput = screen.getByLabelText("参考音频文本") as HTMLTextAreaElement;
      const fileInput = screen.getByLabelText("参考音频文件") as HTMLInputElement;
      const audioFile = new File(["reference audio"], "retained-reference.wav", { type: "audio/wav" });
      fireEvent.change(nameInput, { target: { value: "demo_role" } });
      fireEvent.change(textInput, { target: { value: "失败后应保留的参考音频原文。" } });
      fireEvent.change(fileInput, { target: { files: [audioFile] } });
      fireEvent.click(screen.getByRole("button", { name: "创建音色" }));

      await screen.findByText(/创建 StreamVox 音色失败：StreamVox role already exists: demo_role/);
      expect(nameInput.value).toBe("demo_role");
      expect(textInput.value).toBe("失败后应保留的参考音频原文。");
      expect(fileInput.files?.[0]).toBe(audioFile);
      expect((screen.getByLabelText("上传原音频试听") as HTMLAudioElement).src).toBe("blob:reference-audio");
      expect((screen.getByRole("button", { name: "创建音色" }) as HTMLButtonElement).disabled).toBe(false);

      // 仅修改重名字段；第二次上传仍须使用同一份参考文本和文件。
      fireEvent.change(nameInput, { target: { value: "retry_role" } });
      fireEvent.click(screen.getByRole("button", { name: "创建音色" }));
      await waitFor(() => expect(nameInput.value).toBe(""));
      expect(createRequests).toHaveLength(2);
      expect(createRequests[1].get("role_name")).toBe("retry_role");
      expect(createRequests[1].get("prompt_text")).toBe("失败后应保留的参考音频原文。");
      expect(createRequests[1].get("audio_file")).toBe(audioFile);
      expect(textInput.value).toBe("");
      expect(screen.queryByLabelText("上传原音频试听")).toBeNull();
    } finally {
      URL.createObjectURL = originalCreateObjectUrl;
      URL.revokeObjectURL = originalRevokeObjectUrl;
    }
  });

  it("正式语音页使用模型卡片管理下载，并保留音色管理", async () => {
    const fetchMock = createConsoleFetchMock();
    vi.stubGlobal("fetch", fetchMock);
    render(<App />);
    fireEvent.click(await screen.findByText("StreamVox"));
    expect(await screen.findByText("语音模型")).toBeTruthy();
    expect(await screen.findByRole("article", { name: "Qwen3 TTS 0.6B" })).toBeTruthy();
    expect(screen.getByRole("article", { name: "MOSS TTS Nano" })).toBeTruthy();
    expect(await screen.findByLabelText("模型保存目录")).toBeTruthy();
    expect(screen.queryByLabelText("StreamVox 设备")).toBeNull();
    expect(screen.queryByRole("button", { name: "更换语音模型" })).toBeNull();
    expect(await screen.findByLabelText("新音色名称")).toBeTruthy();
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(expect.stringMatching(/\/api\/bootstrap\/tts\/configuration$/)));
  });

  it("下载其他模型保留当前语音，只有准备并试听后才暂停语音且留在管理页", async () => {
    const sockets = installConsoleControlWebSocketMock();
    const baseFetch = createConsoleFetchMock();
    const originalCreateObjectUrl = URL.createObjectURL;
    const originalRevokeObjectUrl = URL.revokeObjectURL;
    URL.createObjectURL = vi.fn(() => "blob:voice-preview");
    URL.revokeObjectURL = vi.fn();
    let mossDownloaded = false;
    let mossInUse = false;
    let voicePreparing = false;
    const readyStatus = createBootstrapStatusPayload(true);
    const awaitingStatus = {
      ...readyStatus, is_initialized: false,
      voice: { ...readyStatus.voice, is_ready: false, phase: "awaiting_confirmation" }
    };
    const pendingTask = createTtsTaskFixture({
      task_id: "prepare-moss", model_id: "moss-tts-nano-onnx", phase: "awaiting_confirmation", can_confirm: true
    });
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = input.toString();
      if (url.endsWith("/api/bootstrap/tts/models")) return new Response(JSON.stringify({
        provider: "local_streamvox", configured_model: mossInUse ? "moss-tts-nano-onnx" : readyStatus.voice.model_id,
        models: ttsModelsFixture.map((model, index) => ({ ...model, local_state: index === 0 || mossDownloaded ? "downloaded" : "not_downloaded" })),
        download_task: createTtsTaskFixture({ phase: "idle" })
      }));
      if (url.endsWith("/api/bootstrap/tts/downloads") && init?.method === "POST") {
        mossDownloaded = true;
        return new Response(JSON.stringify(createTtsTaskFixture({ task_id: "download-moss", model_id: "moss-tts-nano-onnx", phase: "completed" })));
      }
      if (url.endsWith("/api/bootstrap/tts/tasks") && init?.method === "POST") {
        voicePreparing = true;
        sockets[0].emitMessage({ type: "bootstrap.status_changed", payload: awaitingStatus });
        return new Response(JSON.stringify(pendingTask));
      }
      if (url.endsWith("/api/bootstrap/tts/tasks/prepare-moss/preview")) return new Response(new Blob(["preview"], { type: "audio/wav" }));
      if (url.endsWith("/api/bootstrap/tts/tasks/prepare-moss/confirm")) {
        mossInUse = true;
        voicePreparing = false;
        // 确认 HTTP 已返回权威状态，测试刻意不再广播一次，验证丢事件时仍能刷新当前音色。
        return new Response(JSON.stringify({ ...readyStatus, voice: { ...readyStatus.voice, model_id: "moss-tts-nano-onnx", role_name: "moss_role" } }));
      }
      if (url.endsWith("/api/bootstrap/tts/status")) return new Response(JSON.stringify(voicePreparing ? pendingTask : createTtsTaskFixture({ phase: "ready", is_ready: true })));
      if (url.endsWith("/api/streamvox/runtime") && mossInUse) return new Response(JSON.stringify({
        ...createStreamVoxRuntimePayload(), model: "moss-tts-nano-onnx", role_name: "moss_role", roles: ["moss_role"]
      }));
      return baseFetch(input, init);
    });
    vi.stubGlobal("fetch", fetchMock);
    try {
      render(<App />);
      fireEvent.click(await screen.findByText("StreamVox"));
      const qwenCard = await screen.findByRole("article", { name: "Qwen3 TTS 0.6B" });
      const mossCard = screen.getByRole("article", { name: "MOSS TTS Nano" });
      expect(within(qwenCard).getByText("已下载")).toBeTruthy();
      expect(within(qwenCard).getByText("1.00 GiB")).toBeTruthy();
      expect(within(mossCard).getByText("未下载")).toBeTruthy();
      expect(within(mossCard).getByText("2.00 GiB")).toBeTruthy();
      fireEvent.click(screen.getByRole("button", { name: "下载 MOSS TTS Nano" }));

      const prepareButton = await screen.findByRole("button", { name: "准备并试听 MOSS TTS Nano" });
      expect(within(qwenCard).getByRole("button", { name: "当前使用" })).toBeTruthy();
      expect(screen.getByLabelText("StreamVox 当前模型音色列表")).toBeTruthy();
      expect(fetchMock.mock.calls.some(([url]) => String(url).endsWith("/api/bootstrap/tts/tasks"))).toBe(false);
      expect(fetchMock).toHaveBeenCalledWith(expect.stringMatching(/\/api\/bootstrap\/tts\/downloads$/), expect.objectContaining({
        method: "POST", body: JSON.stringify({ model_id: "moss-tts-nano-onnx" })
      }));

      fireEvent.click(prepareButton);
      expect(await screen.findByRole("region", { name: "语音任务" })).toBeTruthy();
      expect(screen.getByRole("status").textContent).toContain("语音已准备好");
      expect(screen.queryByRole("region", { name: "首次初始化" })).toBeNull();
      expect(screen.queryByLabelText("StreamVox 当前模型音色列表")).toBeNull();
      expect(fetchMock).toHaveBeenCalledWith(expect.stringMatching(/\/api\/bootstrap\/tts\/tasks$/), expect.objectContaining({
        method: "POST", body: JSON.stringify({ model_id: "moss-tts-nano-onnx" })
      }));

      fireEvent.click(screen.getByRole("button", { name: "加载并恢复试听" }));
      fireEvent.ended(await screen.findByLabelText("角色语音试听"));
      fireEvent.click(screen.getByRole("button", { name: "确认并使用这套音色" }));
      expect(await screen.findByRole("heading", { name: "moss_role" })).toBeTruthy();
      expect(screen.queryByRole("region", { name: "初始化完成" })).toBeNull();
      expect(fetchMock.mock.calls.some(([url]) => String(url).endsWith("/api/streamvox/config"))).toBe(false);
    } finally {
      URL.createObjectURL = originalCreateObjectUrl;
      URL.revokeObjectURL = originalRevokeObjectUrl;
    }
  });

  it("普通设置页不包含模型管理表单", async () => {
    vi.stubGlobal("fetch", createConsoleFetchMock());
    render(<App />);
    expect(await screen.findByRole("heading", { name: "设置" })).toBeTruthy();
    expect(screen.queryByLabelText("语音模型")).toBeNull();
  });
  it("可以创建自定义音色、试听上传原音频并试听已创建角色", async () => {
      const objectUrls: string[] = [];
      const originalCreateObjectUrl = URL.createObjectURL;
      const originalRevokeObjectUrl = URL.revokeObjectURL;
      URL.createObjectURL = vi.fn((value: Blob | MediaSource) => {
        const url = `blob:test-${objectUrls.length}`;
        objectUrls.push(url);
        expect(value).toBeTruthy();
        return url;
      });
      URL.revokeObjectURL = vi.fn();
      const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = input.toString();

        if (url.endsWith("/api/bootstrap/status")) {
          return new Response(
            JSON.stringify(createBootstrapStatusPayload(true)),
            {
              headers: {
                "Content-Type": "application/json"
              }
            }
          );
        }

        if (url.endsWith("/api/llm/config")) {
          return new Response(JSON.stringify(createLlmConfigPayload(true)), {
            headers: {
              "Content-Type": "application/json"
            }
          });
        }

        if (url.endsWith("/api/llm/supported-models")) {
          return new Response(JSON.stringify(createSupportedLlmModelsPayload()), {
            headers: {
              "Content-Type": "application/json"
            }
          });
        }

        if (url.endsWith("/api/settings")) {
          return new Response(JSON.stringify(createConsoleSettingsPayload()), {
            headers: {
              "Content-Type": "application/json"
            }
          });
        }

        if (url.endsWith("/api/settings/shortcut-status")) {
          return createShortcutStatusResponse();
        }

        if (url.endsWith("/api/streamvox/runtime")) {
          return new Response(JSON.stringify(createStreamVoxRuntimePayload()), {
            headers: {
              "Content-Type": "application/json"
            }
          });
        }

        if (url.endsWith("/api/streamvox/roles") && init?.method === "POST") {
          return new Response(
            JSON.stringify({
              ...createStreamVoxRuntimePayload(),
              roles: ["demo_role", "trial_role", "custom_role"]
            }),
            {
              status: 202,
              headers: {
                "Content-Type": "application/json"
              }
            }
          );
        }

        if (url.endsWith("/api/streamvox/roles/trial_role/preview")) {
          return new Response(new Blob(["wav"], { type: "audio/wav" }), {
            headers: {
              "Content-Type": "audio/wav"
            }
          });
        }

        if (url.endsWith("/api/character-cards/card-1/wake-response")) {
          return new Response(JSON.stringify(createWakeResponseConfigPayload()), {
            headers: {
              "Content-Type": "application/json"
            }
          });
        }

        return new Response(JSON.stringify([]), {
          headers: {
            "Content-Type": "application/json"
          }
        });
      });

      vi.stubGlobal("fetch", fetchMock);

      try {
        render(<App />);

        fireEvent.click(await screen.findByText("StreamVox"));
        fireEvent.change(await screen.findByLabelText("新音色名称"), {
          target: {
            value: "custom_role"
          }
        });
        fireEvent.change(screen.getByLabelText("参考音频文本"), {
          target: {
            value: "这是一段参考音频文本。"
          }
        });
        const audioFile = new File(["audio"], "reference.wav", { type: "audio/wav" });
        fireEvent.change(screen.getByLabelText("参考音频文件"), {
          target: {
            files: [audioFile]
          }
        });

        expect(await screen.findByLabelText("上传原音频试听")).toBeTruthy();
        fireEvent.click(screen.getByRole("button", { name: "创建音色" }));

        await waitFor(() => {
          expect(fetchMock).toHaveBeenCalledWith(
            expect.stringMatching(/\/api\/streamvox\/roles$/),
            expect.objectContaining({
              method: "POST",
              body: expect.any(FormData)
            })
          );
        });

        fireEvent.click(screen.getByRole("button", { name: "试听 trial_role" }));

        await waitFor(() => {
          expect(fetchMock).toHaveBeenCalledWith(
            expect.stringMatching(/\/api\/streamvox\/roles\/trial_role\/preview$/),
            expect.objectContaining({
              method: "POST"
            })
          );
        });
        expect(await screen.findByLabelText("推理试听音频")).toBeTruthy();
        expect(URL.createObjectURL).toHaveBeenCalled();
      } finally {
        URL.createObjectURL = originalCreateObjectUrl;
        URL.revokeObjectURL = originalRevokeObjectUrl;
      }
    });

  it("可以把指定角色设为默认音色并删除非当前角色", async () => {
      const fetchMock = createConsoleFetchMock();
      vi.stubGlobal("fetch", fetchMock);

      render(<App />);

      fireEvent.click(await screen.findByText("StreamVox"));
      fireEvent.click(await screen.findByRole("button", { name: "设为默认音色 trial_role" }));

      await waitFor(() => {
        expect(fetchMock).toHaveBeenCalledWith(
          expect.stringMatching(/\/api\/streamvox\/config$/),
          expect.objectContaining({
            method: "POST",
            body: JSON.stringify({
              model: "qwen3-tts-clone-0.6b-gguf",
              device: "auto",
              llama_backend: null,
              role_name: "trial_role",
              streamvox_seed: 42,
              inference_options: {
                language: "chinese",
                icl: false
              }
            })
          })
        );
      });

      fireEvent.click(screen.getByRole("button", { name: "删除 trial_role" }));

      await waitFor(() => {
        expect(fetchMock).toHaveBeenCalledWith(
          expect.stringMatching(/\/api\/streamvox\/roles\/trial_role$/),
          expect.objectContaining({
            method: "DELETE"
          })
        );
      });
    });

  it("可以把当前 StreamVox 运行语音更新到当前角色卡", async () => {
      const fetchMock = createConsoleFetchMock();
      vi.stubGlobal("fetch", fetchMock);

      render(<App />);

      fireEvent.click(await screen.findByText("StreamVox"));
      fireEvent.click(await screen.findByRole("button", { name: "更新到当前角色卡" }));

      await waitFor(() => {
        expect(fetchMock).toHaveBeenCalledWith(
          expect.stringMatching(/\/api\/character-cards\/card-1\/voice\/capture-from-runtime$/),
          expect.objectContaining({
            method: "POST"
          })
        );
      });
      expect(fetchMock.mock.calls.some(([calledInput]) => calledInput.toString().endsWith("/api/streamvox/config"))).toBe(false);
    });

  it("默认音色标签跟随当前运行角色，而不是固定显示在 demo_role", async () => {
      const fetchMock = createConsoleFetchMock({
        streamVoxRuntime: {
          ...createStreamVoxRuntimePayload(),
          role_name: "trial_role"
        }
      });
      vi.stubGlobal("fetch", fetchMock);

      render(<App />);

      fireEvent.click(await screen.findByText("StreamVox"));
      const defaultRoleRow = (await screen.findByRole("heading", { name: "demo_role" })).closest("article");
      const trialRoleRow = screen.getByRole("heading", { name: "trial_role" }).closest("article");

      expect(defaultRoleRow?.textContent).not.toContain(" · 默认音色");
      expect(trialRoleRow?.textContent).toContain(" · 默认音色");
    });
});
