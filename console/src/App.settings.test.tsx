import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, it, expect, vi } from "vitest";
import { App } from "./App";
import { SettingsPage } from "./features/settings/SettingsPage";
import type { LlmConfig, SupportedLlmModelsResponse } from "./api/contracts/llm";

// 此套用例覆盖普通控制台的领域行为，运行时边界由 RuntimeInitializationGate 测试单独验证。
vi.mock("./features/runtime-initialization/RuntimeInitializationGate", () => ({
  RuntimeInitializationGate: ({ children }: { children: import("react").ReactNode }) => children
}));
import type { SpeakerVerificationProfile } from "./api/contracts/speakerVerification";
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
  chooseManualUsbMicrophone,
  resetConsoleAppTestEnvironment
} from "./test/consoleAppTestHarness";

afterEach(resetConsoleAppTestEnvironment);

describe("console app settings", () => {
  it("已保存的后台主模型独立保留，修改高级映射不会改变主模型", async () => {
    vi.stubGlobal(
      "fetch",
      createConsoleFetchMock({
        backgroundAgentConfig: {
          environment: {
            ANTHROPIC_BASE_URL: "https://endpoint.example.com",
            ANTHROPIC_DEFAULT_OPUS_MODEL: " model-opus ",
            ANTHROPIC_DEFAULT_SONNET_MODEL: "model-sonnet",
            ANTHROPIC_DEFAULT_HAIKU_MODEL: " model-haiku "
          },
          model: "model-current",
          effort_level: null,
          max_turns: 30,
          permission_mode: "acceptEdits",
          credential_type: "api_key",
          credential_configured: true
        }
      })
    );

    render(<App />);
      fireEvent.click(await screen.findByRole("tab", { name: "AI 与服务" }));

    const modelInput = (await screen.findByLabelText("后台工作主模型")) as HTMLInputElement;
    expect(modelInput.value).toBe("model-current");

    fireEvent.change(modelInput, { target: { value: "custom-background-model" } });

    fireEvent.change(screen.getByLabelText("ANTHROPIC_DEFAULT_OPUS_MODEL"), {
      target: { value: " model-current " }
    });

    expect(modelInput.value).toBe("custom-background-model");
  });

  it("后台模型未配置时保持空白，即使 Foreground 已配置也不能直接保存", async () => {
    const fetchMock = createConsoleFetchMock({
      backgroundAgentConfig: {
        environment: {
          ANTHROPIC_BASE_URL: "https://endpoint.example.com",
          ANTHROPIC_DEFAULT_OPUS_MODEL: " ",
          ANTHROPIC_DEFAULT_SONNET_MODEL: "",
          ANTHROPIC_DEFAULT_HAIKU_MODEL: "  "
        },
        model: "",
        effort_level: null,
        max_turns: 30,
        permission_mode: "acceptEdits",
        credential_type: "api_key",
        credential_configured: false
      }
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<App />);
      fireEvent.click(await screen.findByRole("tab", { name: "AI 与服务" }));

    const modelInput = (await screen.findByLabelText("后台工作主模型")) as HTMLInputElement;
    const saveButton = screen.getByRole("button", { name: "保存后台工作配置" });
    expect(modelInput.disabled).toBe(false);
    expect(modelInput.value).toBe("");
    expect((saveButton as HTMLButtonElement).disabled).toBe(true);

    fireEvent.click(saveButton);
    expect(
      fetchMock.mock.calls.some(
        ([input, init]) =>
          input.toString().endsWith("/api/background-agent/config") && init?.method === "PUT"
      )
    ).toBe(false);
  });

  it("后台配置读取到更新快照时同步普通设置版本", async () => {
    const committedSettings = {
      ...createConsoleSettingsPayload(),
      settings_revision: 4,
      user_extra_notes: "后台配置读取期间更新的资料"
    };
    const fetchMock = createConsoleFetchMock({
      backgroundAgentConfig: {
        environment: {
          ANTHROPIC_BASE_URL: "https://endpoint.example.com",
          ANTHROPIC_DEFAULT_OPUS_MODEL: "model-opus",
          ANTHROPIC_DEFAULT_SONNET_MODEL: "model-sonnet",
          ANTHROPIC_DEFAULT_HAIKU_MODEL: "model-haiku"
        },
        model: "deepseek-flash",
        effort_level: null,
        max_turns: 30,
        permission_mode: "acceptEdits",
        credential_type: "api_key",
        credential_configured: true,
        settings: committedSettings
      }
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<App />);
    fireEvent.click(await screen.findByRole("tab", { name: "AI 与服务" }));
    await screen.findByLabelText("后台工作主模型");

    fireEvent.click(screen.getByRole("tab", { name: "常用" }));
    fireEvent.click(await screen.findByRole("button", { name: "录入快捷键" }));
    const dialog = screen.getByRole("dialog", { name: "修改追问快捷键" });
    fireEvent.keyDown(within(dialog).getByLabelText("快捷键录入区"), {
      key: "f",
      code: "KeyF",
      ctrlKey: true,
      altKey: true
    });
    fireEvent.click(within(dialog).getByRole("button", { name: "使用此快捷键" }));

    await waitFor(() => {
      const settingsSave = fetchMock.mock.calls.find(
        ([input, init]) => input.toString().endsWith("/api/settings") && init?.method === "POST"
      );
      expect(settingsSave).toBeDefined();
      const requestBody = JSON.parse(String(settingsSave?.[1]?.body)) as Record<string, unknown>;
      expect(requestBody.expected_revision).toBe(4);
      expect(requestBody.user_extra_notes).toBe(committedSettings.user_extra_notes);
    });
  });

  it("LLM 配置晚到时不会给未配置的后台模型补默认值或覆盖草稿", async () => {
    vi.stubGlobal("fetch", createConsoleFetchMock({
      backgroundAgentConfig: {
        environment: {},
        model: "",
        effort_level: null,
        max_turns: 30,
        permission_mode: "acceptEdits",
        credential_type: "api_key",
        credential_configured: false
      }
    }));
    const props = {
      settings: createConsoleSettingsPayload(),
      shortcutStatus: null,
      isSaving: false,
      saveErrorMessage: null,
      audioDeviceApplyResult: null,
      bootstrapStatus: createBootstrapStatusPayload(true),
      isSavingBootstrap: false,
      bootstrapSaveErrorMessage: null,
      onSave: vi.fn(async () => true),
      onBootstrapSubmit: vi.fn(async () => true),
      onLlmConfigSubmit: vi.fn(async () => true),
      onQWeatherSubmit: vi.fn(async () => true)
    };
    const { rerender } = render(<SettingsPage {...props} llmConfig={null} supportedLlmModels={null} />);
    fireEvent.click(screen.getByRole("tab", { name: "AI 与服务" }));
    const modelInput = (await screen.findByLabelText("后台工作主模型")) as HTMLInputElement;
    expect(modelInput.value).toBe("");

    const llmConfig = createLlmConfigPayload() as LlmConfig;
    const supportedModels = createSupportedLlmModelsPayload() as SupportedLlmModelsResponse;
    rerender(<SettingsPage {...props} llmConfig={llmConfig} supportedLlmModels={supportedModels} />);
    expect(modelInput.value).toBe("");
    fireEvent.change(modelInput, { target: { value: "my-background-model" } });
    const changedLlmModels = { ...supportedModels, integrations: supportedModels.integrations.map((integration) => ({
      ...integration, model: "another-foreground-model"
    })) };
    rerender(<SettingsPage {...props} llmConfig={llmConfig} supportedLlmModels={changedLlmModels} />);
    expect(modelInput.value).toBe("my-background-model");
  });

  it("后台高级映射不会自动填主模型，用户能填写接入方模型并独立保存", async () => {
    const defaultFetch = createConsoleFetchMock({
      backgroundAgentConfig: {
        environment: {
          ANTHROPIC_BASE_URL: "https://endpoint.example.com",
          ANTHROPIC_DEFAULT_OPUS_MODEL: "glm-opus",
          ANTHROPIC_DEFAULT_SONNET_MODEL: "glm-sonnet",
          ANTHROPIC_DEFAULT_HAIKU_MODEL: ""
        },
        model: "",
        effort_level: null,
        max_turns: 30,
        permission_mode: "acceptEdits",
        credential_type: "api_key",
        credential_configured: false
      }
    });
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (input.toString().endsWith("/api/background-agent/config") && init?.method === "PUT") {
        const submitted = JSON.parse(String(init.body));
        return new Response(JSON.stringify({
          ...submitted.settings,
          credential_type: submitted.credential_type,
          credential_configured: true
        }), { status: 202, headers: { "Content-Type": "application/json" } });
      }
      return defaultFetch(input, init);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<App />);
    fireEvent.click(await screen.findByRole("tab", { name: "AI 与服务" }));

    const modelInput = (await screen.findByLabelText("后台工作主模型")) as HTMLInputElement;
    expect(modelInput.value).toBe("");
    expect((screen.getByRole("button", { name: "保存后台工作配置" }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.change(modelInput, { target: { value: " glm-5 " } });
    fireEvent.change(screen.getByLabelText("Claude Code 凭据"), { target: { value: "test-background-key" } });
    fireEvent.click(screen.getByRole("button", { name: "保存后台工作配置" }));

    await waitFor(() => {
      const savedRequest = fetchMock.mock.calls.find(
        ([input, init]) => input.toString().endsWith("/api/background-agent/config") && init?.method === "PUT"
      );
      expect(JSON.parse(String(savedRequest?.[1]?.body))).toMatchObject({
        settings: {
          model: "glm-5",
          environment: {
            ANTHROPIC_BASE_URL: "https://endpoint.example.com",
            ANTHROPIC_DEFAULT_OPUS_MODEL: "glm-opus",
            ANTHROPIC_DEFAULT_SONNET_MODEL: "glm-sonnet"
          }
        },
        credential_value: "test-background-key"
      });
    });
    await waitFor(() => expect(modelInput.value).toBe("glm-5"));
    expect(fetchMock.mock.calls.filter(
      ([input, init]) => input.toString().endsWith("/api/background-agent/config") && init?.method === "PUT"
    )).toHaveLength(1);
    expect(fetchMock.mock.calls.some(
      ([input, init]) => input.toString().endsWith("/api/llm/config") && init?.method === "PUT"
    )).toBe(false);
  });

  it("最大执行轮数输入小数时禁止提交", async () => {
    const fetchMock = createConsoleFetchMock();
    vi.stubGlobal("fetch", fetchMock);

    render(<App />);
    fireEvent.click(await screen.findByRole("tab", { name: "AI 与服务" }));
    const maxTurnsInput = (await screen.findByLabelText("后台工作最大轮数")) as HTMLInputElement;
    fireEvent.change(maxTurnsInput, { target: { value: "1.5" } });

    const saveButton = screen.getByRole("button", { name: "保存后台工作配置" }) as HTMLButtonElement;
    expect(saveButton.disabled).toBe(true);
    fireEvent.click(saveButton);
    expect(
      fetchMock.mock.calls.some(
        ([input, init]) => input.toString().endsWith("/api/background-agent/config") && init?.method === "PUT"
      )
    ).toBe(false);
  });

  it("保存返回 422 时展示字段校验原因", async () => {
    const defaultFetch = createConsoleFetchMock();
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      if (input.toString().endsWith("/api/background-agent/config") && init?.method === "PUT") {
        return Promise.resolve(new Response(JSON.stringify({
          detail: [{ loc: ["body", "settings", "model"], msg: "后台工作主模型不能为空" }]
        }), {
          status: 422,
          headers: { "Content-Type": "application/json" }
        }));
      }
      return defaultFetch(input, init);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<App />);
    fireEvent.click(await screen.findByRole("tab", { name: "AI 与服务" }));
    fireEvent.click(await screen.findByRole("button", { name: "保存后台工作配置" }));

    expect((await screen.findByRole("alert")).textContent).toContain("body.settings.model: 后台工作主模型不能为空");
  });

  it("后台配置已保存但设置快照回读失败时仍明确提示保存成功", async () => {
    const defaultFetch = createConsoleFetchMock();
    let settingsReadCount = 0;
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = input.toString();
      if (url.endsWith("/api/settings") && init?.method === undefined) {
        settingsReadCount += 1;
        // 首屏读取必须成功；后台配置保存后触发的第二次读取模拟临时不可用。
        if (settingsReadCount >= 2) {
          return new Response(JSON.stringify({ detail: "设置服务暂时不可用" }), {
            status: 503,
            headers: { "Content-Type": "application/json" }
          });
        }
      }
      return defaultFetch(input, init);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<App />);
    fireEvent.click(await screen.findByRole("tab", { name: "AI 与服务" }));
    fireEvent.click(await screen.findByRole("button", { name: "保存后台工作配置" }));

    expect(await screen.findByText(/已保存；之后创建的新任务会使用这套配置/)).toBeTruthy();
    expect(await screen.findByText(/已保存，但页面状态刷新失败：/)).toBeTruthy();
    expect(screen.queryByText(/保存后台工作配置失败/)).toBeNull();
  });

  it("模型默认 high 说明不会把默认强度固化到保存配置", async () => {
    const fetchMock = createConsoleFetchMock();
    vi.stubGlobal("fetch", fetchMock);

    render(<App />);
      fireEvent.click(await screen.findByRole("tab", { name: "AI 与服务" }));

    const effortSelect = (await screen.findByLabelText("后台工作推理强度")) as HTMLSelectElement;
    expect(effortSelect.selectedOptions[0]?.textContent).toBe("模型默认（high）");
    fireEvent.click(screen.getByRole("button", { name: "保存后台工作配置" }));

    await waitFor(() => {
      const savedRequest = fetchMock.mock.calls.find(
        ([input, init]) => input.toString().endsWith("/api/background-agent/config") && init?.method === "PUT"
      );
      const savedBody = JSON.parse(String(savedRequest?.[1]?.body)) as {
        settings: Record<string, unknown>;
      };
      expect(savedBody.settings.effort_level).toBeNull();
    });
  });

  it("后台工作配置保存显式主模型、无限轮次和全权限模式", async () => {
    const fetchMock = createConsoleFetchMock();
    vi.stubGlobal("fetch", fetchMock);

    render(<App />);
      fireEvent.click(await screen.findByRole("tab", { name: "AI 与服务" }));

    const permissionSelect = await screen.findByLabelText("后台工作权限模式");
    fireEvent.change(permissionSelect, { target: { value: "bypassPermissions" } });
    fireEvent.change(screen.getByLabelText("ANTHROPIC_BASE_URL"), {
      target: { value: "https://new-endpoint.example.com" }
    });
    fireEvent.change(screen.getByLabelText("后台工作主模型"), {
      target: { value: "deepseek-flash" }
    });
    fireEvent.change(screen.getByLabelText("后台工作推理强度"), {
      target: { value: "high" }
    });
    fireEvent.change(screen.getByLabelText("后台工作最大轮数"), {
      target: { value: "0" }
    });
    expect(screen.getByText(/全权限模式不会为 Bash/)).toBeTruthy();
    expect(screen.getByRole("alert").textContent).toContain("执行 Bash 等操作时不会再等待确认");
    fireEvent.click(screen.getByRole("button", { name: "保存后台工作配置" }));

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringMatching(/\/api\/background-agent\/config$/),
        expect.objectContaining({
          method: "PUT",
          body: expect.stringContaining('"model":"deepseek-flash"')
        })
      );
    });
    const savedRequest = fetchMock.mock.calls.find(
      ([input, init]) =>
        input.toString().endsWith("/api/background-agent/config") &&
        init?.method === "PUT"
    );
    const savedBody = JSON.parse(String(savedRequest?.[1]?.body)) as {
      settings: Record<string, unknown>;
    };
    expect(savedBody.settings).toMatchObject({
      model: "deepseek-flash",
      effort_level: "high",
      max_turns: 0,
      permission_mode: "bypassPermissions"
    });
    expect(savedBody.settings).not.toHaveProperty("max_budget_usd");
  });

  it("可以在设置页保存我的信息", async () => {
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

        if (url.endsWith("/api/settings") && init?.method === "POST") {
          const { expected_revision: _expectedRevision, ...submittedSettings } = JSON.parse(
            String(init.body)
          ) as Record<string, unknown>;
          return new Response(
            JSON.stringify({
              ...createConsoleSettingsPayload(),
              ...submittedSettings,
              settings_revision: 1
            }),
            {
              status: 202,
              headers: {
                "Content-Type": "application/json"
              }
            }
          );
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

        if (url.endsWith("/api/character-cards/card-1/wake-response")) {
          return new Response(JSON.stringify(createWakeResponseConfigPayload()), {
            headers: {
              "Content-Type": "application/json"
            }
          });
        }

        if (url.endsWith("/api/streamvox/runtime")) {
          return new Response(JSON.stringify(createStreamVoxRuntimePayload()), {
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

      render(<App />);
      fireEvent.click(await screen.findByRole("tab", { name: "我的信息" }));

      fireEvent.change(await screen.findByLabelText("自我简介"), {
        target: {
          value: "我在开发本地虚拟伴侣。"
        }
      });
      fireEvent.change(screen.getByLabelText("自由补充"), {
        target: {
          value: "我喜欢直接、清楚的技术讨论。"
        }
      });
      fireEvent.click(screen.getByRole("button", { name: "保存我的信息" }));

      await waitFor(() => {
        const savedRequest = fetchMock.mock.calls.find(
          ([input, init]) => input.toString().endsWith("/api/settings") && init?.method === "POST"
        );
        expect(savedRequest).toBeTruthy();
        expect(JSON.parse(String(savedRequest?.[1]?.body))).toEqual({
          expected_revision: 0,
          show_speech_bubbles: true,
          selected_live2d_model_id: "Haru",
          user_self_intro: "我在开发本地虚拟伴侣。",
          user_extra_notes: "我喜欢直接、清楚的技术讨论。",
          followup_toggle_shortcut_enabled: true,
          followup_toggle_shortcut: "CommandOrControl+Alt+Space",
          avatar_visibility_shortcut_enabled: true,
          avatar_visibility_shortcut: "CommandOrControl+Q",
          selection_capture_shortcut_enabled: true,
          selection_capture_shortcut: "CommandOrControl+Alt+C"
        });
      });
    });

  it("连续保存普通设置时使用上一次响应的新版本", async () => {
      const fetchMock = createConsoleFetchMock();
      vi.stubGlobal("fetch", fetchMock);

      render(<App />);
      fireEvent.click(await screen.findByRole("tab", { name: "我的信息" }));

      fireEvent.change(await screen.findByLabelText("自我简介"), {
        target: {
          value: "第一次保存"
        }
      });
      const saveButton = screen.getByRole("button", { name: "保存我的信息" });
      fireEvent.click(saveButton);

      await waitFor(() => {
        const savedRequests = fetchMock.mock.calls.filter(
          ([input, init]) => input.toString().endsWith("/api/settings") && init?.method === "POST"
        );
        expect(savedRequests).toHaveLength(1);
      });

      fireEvent.change(screen.getByLabelText("自由补充"), {
        target: {
          value: "第二次保存"
        }
      });
      fireEvent.click(saveButton);

      await waitFor(() => {
        const savedRequests = fetchMock.mock.calls.filter(
          ([input, init]) => input.toString().endsWith("/api/settings") && init?.method === "POST"
        );
        expect(savedRequests).toHaveLength(2);
        const firstBody = JSON.parse(String(savedRequests[0]?.[1]?.body)) as Record<string, unknown>;
        const secondBody = JSON.parse(String(savedRequests[1]?.[1]?.body)) as Record<string, unknown>;
        expect(firstBody.expected_revision).toBe(0);
        expect(secondBody.expected_revision).toBe(1);
      });
    });

  it("其它设置入口广播新快照后，普通设置保存使用最新版本", async () => {
      const websocketInstances = installConsoleControlWebSocketMock();
      const fetchMock = createConsoleFetchMock();
      vi.stubGlobal("fetch", fetchMock);

      render(<App />);
      fireEvent.click(await screen.findByRole("tab", { name: "我的信息" }));

      const selfIntroInput = await screen.findByLabelText("自我简介");
      expect(websocketInstances).toHaveLength(1);
      act(() => {
        websocketInstances[0].emitMessage({
          type: "settings.changed",
          payload: {
            settings_revision: 8,
            user_self_intro: "缺少其它字段的非法快照"
          }
        });
      });
      expect((selfIntroInput as HTMLTextAreaElement).value).toBe("");

      act(() => {
        websocketInstances[0].emitMessage({
          type: "settings.changed",
          payload: {
            ...createConsoleSettingsPayload(),
            settings_revision: 9,
            user_self_intro: "来自其它设置入口的新快照"
          }
        });
        websocketInstances[0].emitMessage({
          type: "settings.changed",
          payload: {
            ...createConsoleSettingsPayload(),
            settings_revision: 8,
            user_self_intro: "迟到的旧快照"
          }
        });
      });

      await waitFor(() => {
        expect((selfIntroInput as HTMLTextAreaElement).value).toBe("来自其它设置入口的新快照");
      });
      fireEvent.change(selfIntroInput, {
        target: {
          value: "使用最新版本保存"
        }
      });
      fireEvent.click(screen.getByRole("button", { name: "保存我的信息" }));

      await waitFor(() => {
        const savedRequest = fetchMock.mock.calls.find(
          ([input, init]) => input.toString().endsWith("/api/settings") && init?.method === "POST"
        );
        expect(savedRequest).toBeDefined();
        const requestBody = JSON.parse(String(savedRequest?.[1]?.body)) as Record<string, unknown>;
        expect(requestBody.expected_revision).toBe(9);
      });
    });

  it("迟到的首次 HTTP 设置快照不能覆盖已经到达的控制事件", async () => {
      const websocketInstances = installConsoleControlWebSocketMock();
      const defaultFetch = createConsoleFetchMock();
      let resolveInitialSettings!: (response: Response) => void;
      const delayedSettingsResponse = new Promise<Response>((resolve) => {
        resolveInitialSettings = resolve;
      });
      const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
        if (input.toString().endsWith("/api/settings") && init?.method !== "POST") {
          return delayedSettingsResponse;
        }
        return defaultFetch(input, init);
      });
      vi.stubGlobal("fetch", fetchMock);

      render(<App />);

      await waitFor(() => {
        expect(websocketInstances).toHaveLength(1);
      });
      act(() => {
        websocketInstances[0].emitMessage({
          type: "settings.changed",
          payload: {
            ...createConsoleSettingsPayload(),
            settings_revision: 9,
            user_self_intro: "先到达的实时快照"
          }
        });
      });
      resolveInitialSettings(
        new Response(
          JSON.stringify({
            ...createConsoleSettingsPayload(),
            settings_revision: 8,
            user_self_intro: "迟到的首次 HTTP 快照"
          }),
          { headers: { "Content-Type": "application/json" } }
        )
      );

      const selfIntroInput = await screen.findByLabelText("自我简介");
      expect((selfIntroInput as HTMLTextAreaElement).value).toBe("先到达的实时快照");
    });

  it("设置页可以录入并保存追问快捷键", async () => {
      const fetchMock = createConsoleFetchMock();
      vi.stubGlobal("fetch", fetchMock);

      render(<App />);

      expect(await screen.findByText("追问快捷键")).toBeTruthy();
      fireEvent.click(screen.getByRole("button", { name: "录入快捷键" }));
      fireEvent.keyDown(screen.getByLabelText("快捷键录入区"), {
        key: "f",
        code: "KeyF",
        ctrlKey: true,
        altKey: true
      });
      fireEvent.click(screen.getByRole("button", { name: "使用此快捷键" }));

      await waitFor(() => {
        expect(fetchMock).toHaveBeenCalledWith(
          expect.stringMatching(/\/api\/settings$/),
          expect.objectContaining({
            method: "POST",
            body: expect.stringContaining('"followup_toggle_shortcut":"CommandOrControl+Alt+F"')
          })
        );
      });
    });

  it("快捷键 HTTP 保存失败保留弹窗候选并可重试，保存中不能关闭", async () => {
    const defaultFetch = createConsoleFetchMock();
    const settingsRequests: RequestInit[] = [];
    let finishFirstSave!: (response: Response) => void;
    const firstSaveResponse = new Promise<Response>((resolve) => { finishFirstSave = resolve; });
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      if (input.toString().endsWith("/api/settings") && init?.method === "POST") {
        settingsRequests.push(init);
        if (settingsRequests.length === 1) return firstSaveResponse;
      }
      return defaultFetch(input, init);
    }));

    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "录入快捷键" }));
    const dialog = screen.getByRole("dialog", { name: "修改追问快捷键" });
    fireEvent.keyDown(within(dialog).getByLabelText("快捷键录入区"), {
      key: "k", code: "KeyK", ctrlKey: true, altKey: true
    });
    fireEvent.click(within(dialog).getByRole("button", { name: "使用此快捷键" }));

    await waitFor(() => expect(settingsRequests).toHaveLength(1));
    const cancelButton = within(dialog).getByRole("button", { name: "取消" }) as HTMLButtonElement;
    expect(cancelButton.disabled).toBe(true);
    fireEvent.click(cancelButton);
    fireEvent.keyDown(dialog, { key: "Escape", code: "Escape" });
    fireEvent.click(within(dialog).getByRole("button", { name: "关闭" }));
    expect(screen.getByRole("dialog", { name: "修改追问快捷键" })).toBe(dialog);

    await act(async () => finishFirstSave(new Response(JSON.stringify({ detail: "设置版本冲突，请重新保存" }), {
      status: 409, headers: { "Content-Type": "application/json" }
    })));
    expect((await within(dialog).findByRole("alert")).textContent).toContain("设置已被其他操作更新");
    expect(within(dialog).getByLabelText("快捷键录入区").textContent).toContain("K");
    expect((within(dialog).getByRole("button", { name: "使用此快捷键" }) as HTMLButtonElement).disabled).toBe(false);

    // 重试直接使用留下的候选，用户不需要再次录入组合键。
    fireEvent.click(within(dialog).getByRole("button", { name: "使用此快捷键" }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "修改追问快捷键" })).toBeNull());
    expect(settingsRequests).toHaveLength(2);
    for (const request of settingsRequests) {
      expect(JSON.parse(String(request.body)).followup_toggle_shortcut).toBe("CommandOrControl+Alt+K");
    }
    expect(screen.getByRole("button", { name: "录入快捷键" }).textContent).toContain("K");
  });

  it("设置页可以关闭追问快捷键", async () => {
      const fetchMock = createConsoleFetchMock();
      vi.stubGlobal("fetch", fetchMock);

      render(<App />);

      fireEvent.click(await screen.findByRole("switch", { name: "关闭追问快捷键" }));

      await waitFor(() => {
        expect(fetchMock).toHaveBeenCalledWith(
          expect.stringMatching(/\/api\/settings$/),
          expect.objectContaining({
            method: "POST",
            body: expect.stringContaining('"followup_toggle_shortcut_enabled":false')
          })
        );
      });
    });

  it("设置页可以录入并保存桌宠召回快捷键", async () => {
      const fetchMock = createConsoleFetchMock();
      vi.stubGlobal("fetch", fetchMock);

      render(<App />);

      expect(await screen.findByText("桌宠召回快捷键")).toBeTruthy();
      fireEvent.click(screen.getByRole("button", { name: "录入桌宠召回快捷键" }));
      fireEvent.keyDown(screen.getByLabelText("快捷键录入区"), {
        key: "v",
        code: "KeyV",
        ctrlKey: true,
        altKey: true
      });
      fireEvent.click(screen.getByRole("button", { name: "使用此快捷键" }));

      await waitFor(() => {
        expect(fetchMock).toHaveBeenCalledWith(
          expect.stringMatching(/\/api\/settings$/),
          expect.objectContaining({
            method: "POST",
            body: expect.stringContaining('"avatar_visibility_shortcut":"CommandOrControl+Alt+V"')
          })
        );
      });
    });

  it("设置页可以关闭桌宠召回快捷键", async () => {
      const fetchMock = createConsoleFetchMock();
      vi.stubGlobal("fetch", fetchMock);

      render(<App />);

      fireEvent.click(await screen.findByRole("switch", { name: "关闭桌宠召回快捷键" }));

      await waitFor(() => {
        expect(fetchMock).toHaveBeenCalledWith(
          expect.stringMatching(/\/api\/settings$/),
          expect.objectContaining({
            method: "POST",
            body: expect.stringContaining('"avatar_visibility_shortcut_enabled":false')
          })
        );
      });
    });

  it("设置页可以录入并保存收集选中内容快捷键", async () => {
      const fetchMock = createConsoleFetchMock();
      vi.stubGlobal("fetch", fetchMock);

      render(<App />);

      expect(await screen.findByText("收集选中内容快捷键")).toBeTruthy();
      fireEvent.click(screen.getByRole("button", { name: "录入收集快捷键" }));
      fireEvent.keyDown(screen.getByLabelText("快捷键录入区"), {
        key: "x",
        code: "KeyX",
        ctrlKey: true,
        altKey: true
      });
      fireEvent.click(screen.getByRole("button", { name: "使用此快捷键" }));

      await waitFor(() => {
        expect(fetchMock).toHaveBeenCalledWith(
          expect.stringMatching(/\/api\/settings$/),
          expect.objectContaining({
            method: "POST",
            body: expect.stringContaining('"selection_capture_shortcut":"CommandOrControl+Alt+X"')
          })
        );
      });
    });

  it("设置页可以关闭收集选中内容快捷键", async () => {
      const fetchMock = createConsoleFetchMock();
      vi.stubGlobal("fetch", fetchMock);

      render(<App />);

      fireEvent.click(await screen.findByRole("switch", { name: "关闭收集选中内容快捷键" }));

      await waitFor(() => {
        expect(fetchMock).toHaveBeenCalledWith(
          expect.stringMatching(/\/api\/settings$/),
          expect.objectContaining({
            method: "POST",
            body: expect.stringContaining('"selection_capture_shortcut_enabled":false')
          })
        );
      });
    });

  it("设置页展示系统或其它应用占用的快捷键警告", async () => {
      const fetchMock = createConsoleFetchMock({
        shortcutStatus: {
          items: [
            {
              shortcut_id: "followup_toggle",
              accelerator: "CommandOrControl+Alt+Space",
              enabled: true,
              status: "unavailable",
              settings_revision: 0,
              checked_at: "2026-07-09T14:00:00+08:00",
              message: "可能被系统或其他应用占用"
            },
            {
              shortcut_id: "avatar_visibility",
              accelerator: "CommandOrControl+Q",
              enabled: true,
              status: "registered",
              settings_revision: 0,
              checked_at: "2026-07-09T14:00:00+08:00",
              message: "快捷键可用"
            },
            {
              shortcut_id: "selection_capture",
              accelerator: "CommandOrControl+Alt+C",
              enabled: true,
              status: "registered",
              settings_revision: 0,
              checked_at: "2026-07-09T14:00:00+08:00",
              message: "快捷键可用"
            }
          ]
        }
      });
      vi.stubGlobal("fetch", fetchMock);

      render(<App />);

      expect(await screen.findByText("可能被系统或其他应用占用")).toBeTruthy();
      expect(screen.getByText("当前设备不可用")).toBeTruthy();
    });

  it("录入 NearMate 内部重复快捷键时提示冲突但仍允许保存", async () => {
      const fetchMock = createConsoleFetchMock();
      vi.stubGlobal("fetch", fetchMock);

      render(<App />);

      await screen.findByText("追问快捷键");
      fireEvent.click(screen.getByRole("button", { name: "录入桌宠召回快捷键" }));
      fireEvent.keyDown(screen.getByLabelText("快捷键录入区"), {
        key: " ",
        code: "Space",
        ctrlKey: true,
        altKey: true
      });
      fireEvent.click(screen.getByRole("button", { name: "使用此快捷键" }));

      expect(await screen.findAllByText("与 NearMate 其它快捷键重复")).toHaveLength(2);
      await waitFor(() => {
        expect(fetchMock).toHaveBeenCalledWith(
          expect.stringMatching(/\/api\/settings$/),
          expect.objectContaining({
            method: "POST",
            body: expect.stringContaining('"avatar_visibility_shortcut":"CommandOrControl+Alt+Space"')
          })
        );
      });
    });

  it("设置页不再暴露全局唤醒词和角色名设置源", async () => {
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

        if (url.endsWith("/api/settings") && init?.method === "POST") {
          return new Response(null, { status: 202 });
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

        if (url.endsWith("/api/character-cards/card-1/wake-response")) {
          return new Response(JSON.stringify(createWakeResponseConfigPayload()), {
            headers: {
              "Content-Type": "application/json"
            }
          });
        }

        if (url.endsWith("/api/streamvox/runtime")) {
          return new Response(JSON.stringify(createStreamVoxRuntimePayload()), {
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

      render(<App />);

      expect(await screen.findByRole("heading", { name: "设置" })).toBeTruthy();
      expect(screen.queryByText("角色体验包")).toBeNull();
      expect(screen.queryByLabelText("唤醒词 1")).toBeNull();
      expect(screen.queryByText("当前角色")).toBeNull();
      expect(screen.queryByLabelText("桌宠名字")).toBeNull();
      expect(fetchMock.mock.calls.some(([input, init]) => input.toString().endsWith("/api/settings") && init?.method === "POST")).toBe(false);
    });

  it("音频设备测试失败且后端错误信息为空时会显示失败状态", async () => {
      vi.stubGlobal(
        "fetch",
        createConsoleFetchMock({
          audioDeviceTestResult: {
            status: "format_incompatible",
            peak_level: 0,
            rms_level: 0,
            duration_ms: 0,
            sample_rate: 48000,
            level_status: "silent",
            audio_wav_b64: null,
            error_type: "AssertionError",
            error_message: ""
          }
        })
      );

      render(<App />);
      fireEvent.click(await screen.findByRole("tab", { name: "声音与语音" }));

      expect(await screen.findByText("音频设备")).toBeTruthy();
      fireEvent.click(screen.getByRole("button", { name: "开始 3 秒测试" }));

      expect(await screen.findByText("麦克风不可用：format_incompatible")).toBeTruthy();
    });

  it("音频设备测试会提示用户说话并展示回放音频", async () => {
      let resolveAudioTest: (response: Response) => void = () => undefined;
      const audioTestResponse = new Promise<Response>((resolve) => {
        resolveAudioTest = resolve;
      });
      vi.stubGlobal(
        "fetch",
        createConsoleFetchMock({
          audioDeviceTestResponse: audioTestResponse
        })
      );

      render(<App />);
      fireEvent.click(await screen.findByRole("tab", { name: "声音与语音" }));

      expect(await screen.findByText("音频设备")).toBeTruthy();
      fireEvent.click(screen.getByRole("button", { name: "开始 3 秒测试" }));

      expect(await screen.findByText("请现在说话")).toBeTruthy();
      expect(screen.getByText("正在录音 3 秒")).toBeTruthy();

      await act(async () => {
        resolveAudioTest(
          new Response(
            JSON.stringify({
              status: "available",
              peak_level: 0.3,
              rms_level: 0.08,
              duration_ms: 3000,
              sample_rate: 48000,
              level_status: "good",
              audio_wav_b64: "UklGRgAAAAA=",
              error_type: null,
              error_message: null
            }),
            {
              headers: {
                "Content-Type": "application/json"
              }
            }
          )
        );
      });

      expect(await screen.findByText("麦克风音量正常")).toBeTruthy();
      const audio = screen.getByLabelText("麦克风测试录音") as HTMLAudioElement;
      expect(audio.src).toContain("data:audio/wav;base64,UklGRgAAAAA=");
    });

  it("系统默认模式会显示当前默认麦克风和扬声器参考", async () => {
      vi.stubGlobal("fetch", createConsoleFetchMock());

      render(<App />);
      fireEvent.click(await screen.findByRole("tab", { name: "声音与语音" }));

      const microphoneSelect = (await screen.findByLabelText("麦克风")) as HTMLSelectElement;
      const speakerLoopbackSelect = screen.getByLabelText("扬声器参考") as HTMLSelectElement;

      await waitFor(() => {
        expect(microphoneSelect.value).toBe("mic-k66");
        expect(speakerLoopbackSelect.value).toBe("speaker-default");
      });
      expect(microphoneSelect.disabled).toBe(true);
      expect(speakerLoopbackSelect.disabled).toBe(true);
    });

  it("手动模式未选择麦克风时禁用麦克风测试", async () => {
      vi.stubGlobal("fetch", createConsoleFetchMock());

      render(<App />);
      fireEvent.click(await screen.findByRole("tab", { name: "声音与语音" }));

      const modeSelect = (await screen.findByLabelText("音频设备选择模式")) as HTMLSelectElement;
      fireEvent.change(modeSelect, {
        target: {
          value: "manual"
        }
      });

      const testButton = screen.getByRole("button", { name: "开始 3 秒测试" }) as HTMLButtonElement;
      const submitButton = screen.getByRole("button", { name: "更改音频设备" }) as HTMLButtonElement;
      expect(testButton.disabled).toBe(true);
      expect(submitButton.disabled).toBe(true);
    });

  it("音频设备选择未变化时禁用更改按钮，变更后才允许提交", async () => {
      vi.stubGlobal("fetch", createConsoleFetchMock());

      render(<App />);
      fireEvent.click(await screen.findByRole("tab", { name: "声音与语音" }));

      const submitButton = (await screen.findByRole("button", { name: "更改音频设备" })) as HTMLButtonElement;
      await waitFor(() => {
        expect(submitButton.disabled).toBe(true);
      });

      await chooseManualUsbMicrophone();
      expect(submitButton.disabled).toBe(false);

      const modeSelect = screen.getByLabelText("音频设备选择模式") as HTMLSelectElement;
      fireEvent.change(modeSelect, {
        target: {
          value: "system_default"
        }
      });

      await waitFor(() => {
        expect(submitButton.disabled).toBe(true);
      });
    });

  it("更改音频设备后会显示正在应用，并在桌宠回报成功后显示已应用", async () => {
      const websocketInstances = installConsoleControlWebSocketMock();
      vi.stubGlobal("fetch", createConsoleFetchMock());

      render(<App />);
      fireEvent.click(await screen.findByRole("tab", { name: "声音与语音" }));

      expect(await screen.findByText("音频设备")).toBeTruthy();
      await chooseManualUsbMicrophone();
      fireEvent.click(screen.getByRole("button", { name: "更改音频设备" }));

      expect(await screen.findByText("已保存，正在应用到桌宠语音会话")).toBeTruthy();
      expect(websocketInstances).toHaveLength(1);

      act(() => {
        websocketInstances[0].emitMessage({
          type: "audio.devices_apply_result",
          payload: {
            request_id: "audio-change-1",
            status: "applied",
            mode: "system_default",
            reason: null,
            applied_at: 1
          }
        });
      });

      expect(await screen.findByText("音频设备已应用到桌宠语音会话")).toBeTruthy();
    });

  it("音频设备提交后快捷键使用后端返回的新 settings_revision", async () => {
      const fetchMock = createConsoleFetchMock();
      vi.stubGlobal("fetch", fetchMock);

      render(<App />);
      fireEvent.click(await screen.findByRole("tab", { name: "声音与语音" }));
      await chooseManualUsbMicrophone();
      fireEvent.click(screen.getByRole("button", { name: "更改音频设备" }));
      await screen.findByText("已保存，正在应用到桌宠语音会话");

      fireEvent.click(screen.getByRole("tab", { name: "常用" }));
      fireEvent.click(await screen.findByRole("button", { name: "录入快捷键" }));
      const dialog = screen.getByRole("dialog", { name: "修改追问快捷键" });
      fireEvent.keyDown(within(dialog).getByLabelText("快捷键录入区"), {
        key: "f",
        code: "KeyF",
        ctrlKey: true,
        altKey: true
      });
      fireEvent.click(within(dialog).getByRole("button", { name: "使用此快捷键" }));

      await waitFor(() => {
        const settingsPostBodies = fetchMock.mock.calls
          .filter(([input, init]) => input.toString().endsWith("/api/settings") && init?.method === "POST")
          .map(([, init]) => JSON.parse(String(init?.body)) as Record<string, unknown>);
        expect(settingsPostBodies).toHaveLength(1);
        expect(settingsPostBodies[0]?.expected_revision).toBe(1);
      });
    });

  it("桌宠回报音频设备应用失败时会显示失败原因", async () => {
      const websocketInstances = installConsoleControlWebSocketMock();
      vi.stubGlobal("fetch", createConsoleFetchMock());

      render(<App />);
      fireEvent.click(await screen.findByRole("tab", { name: "声音与语音" }));

      expect(await screen.findByText("音频设备")).toBeTruthy();
      await chooseManualUsbMicrophone();
      fireEvent.click(screen.getByRole("button", { name: "更改音频设备" }));
      expect(await screen.findByText("已保存，正在应用到桌宠语音会话")).toBeTruthy();

      act(() => {
        websocketInstances[0].emitMessage({
          type: "audio.devices_apply_result",
          payload: {
            request_id: "audio-change-1",
            status: "failed",
            mode: "system_default",
            reason: "AEC sidecar failed to start.",
            applied_at: 1
          }
        });
      });

      expect(await screen.findByText("已保存，但应用失败：AEC sidecar failed to start.")).toBeTruthy();
    });

  it("更改音频设备后未收到桌宠回报时会显示未确认提示", async () => {
      installConsoleControlWebSocketMock();
      vi.stubGlobal("fetch", createConsoleFetchMock());

      render(<App />);
      fireEvent.click(await screen.findByRole("tab", { name: "声音与语音" }));

      expect(await screen.findByText("音频设备")).toBeTruthy();
      await chooseManualUsbMicrophone();
      fireEvent.click(screen.getByRole("button", { name: "更改音频设备" }));
      vi.useFakeTimers();
      await act(async () => {
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(screen.getByText("已保存，正在应用到桌宠语音会话")).toBeTruthy();

      act(() => {
        vi.advanceTimersByTime(8000);
      });

      expect(screen.getByText("已保存，但未收到桌宠应用结果；请确认 desktop 正在运行")).toBeTruthy();
      vi.useRealTimers();
    });

  it("设置页无录音时禁用说话人确认开关", async () => {
      vi.stubGlobal("fetch", createConsoleFetchMock());

      render(<App />);
      fireEvent.click(await screen.findByRole("tab", { name: "声音与语音" }));

      expect(await screen.findByRole("switch", { name: "开启说话人确认" })).toBeTruthy();
      expect(screen.getByText("已录制 0 / 10 段")).toBeTruthy();
      const toggle = screen.getByRole("switch", { name: "开启说话人确认" }) as HTMLButtonElement;
      expect(toggle.disabled).toBe(true);
    });

  it("设置页已有录音时可以开启并删除说话人白名单样本", async () => {
      const fetchMock = createConsoleFetchMock({
        speakerProfile: {
          enabled: false,
          samples: [
            {
              id: "sample-1",
              audio_url: "/api/speaker-verification/samples/sample-1/audio",
              created_at: 123
            }
          ],
          max_sample_count: 10,
          required_speech_seconds: 3,
          can_enable: true
        }
      });
      vi.stubGlobal("fetch", fetchMock);

      render(<App />);
      fireEvent.click(await screen.findByRole("tab", { name: "声音与语音" }));

      fireEvent.click(await screen.findByRole("switch", { name: "开启说话人确认" }));

      await waitFor(() => {
        expect(fetchMock).toHaveBeenCalledWith(
          expect.stringMatching(/\/api\/speaker-verification\/profile$/),
          expect.objectContaining({
            method: "POST",
            body: JSON.stringify({ enabled: true })
          })
        );
      });

      expect(screen.getByText("已录制 1 / 10 段")).toBeTruthy();
      expect(screen.queryByLabelText("白名单片段 1 音频")).toBeNull();
      fireEvent.click(screen.getByRole("button", { name: "管理说话人样本" }));
      expect(screen.getByLabelText("白名单片段 1 音频")).toBeTruthy();
      fireEvent.click(screen.getByRole("button", { name: "删除 sample-1" }));

      await waitFor(() => {
        expect(fetchMock).toHaveBeenCalledWith(
          expect.stringMatching(/\/api\/speaker-verification\/samples\/sample-1$/),
          expect.objectContaining({
            method: "DELETE"
          })
        );
      });
    });

  it("替换说话人白名单样本时会把槽位编号随录音上传", async () => {
      const speakerProfile: SpeakerVerificationProfile = {
        enabled: false,
        samples: [
          {
            id: "sample-1",
            audio_url: "/api/speaker-verification/samples/sample-1/audio",
            created_at: 123
          }
        ],
        max_sample_count: 10,
        required_speech_seconds: 3,
        can_enable: true
      };
      const fetchMock = createConsoleFetchMock({ speakerProfile });
      vi.stubGlobal("fetch", fetchMock);
      const mediaTrackStop = vi.fn();
      Object.defineProperty(navigator, "mediaDevices", {
        configurable: true,
        value: {
          getUserMedia: vi.fn(async () => ({
            getTracks: () => [{ stop: mediaTrackStop }]
          }))
        }
      });

      type AudioProcessHandler = (event: { inputBuffer: { getChannelData: () => Float32Array } }) => void;
      let audioProcessHandler: AudioProcessHandler | null = null;
      const processorNode = {
        connect: vi.fn(),
        disconnect: vi.fn(),
        set onaudioprocess(handler: AudioProcessHandler | null) {
          audioProcessHandler = handler;
        }
      };
      const sourceNode = {
        connect: vi.fn(),
        disconnect: vi.fn()
      };
      const audioContextMock = vi.fn(() => ({
        sampleRate: 16000,
        destination: {},
        createMediaStreamSource: vi.fn(() => sourceNode),
        createScriptProcessor: vi.fn(() => processorNode),
        close: vi.fn(async () => undefined)
      }));
      vi.stubGlobal("AudioContext", audioContextMock);

      type TestWebSocketInstance = {
        binaryType: BinaryType;
        readyState: number;
        sentChunks: ArrayBuffer[];
        close: ReturnType<typeof vi.fn>;
        send: ReturnType<typeof vi.fn>;
        onopen: (() => void) | null;
        onerror: (() => void) | null;
        onmessage: ((event: MessageEvent<string>) => void) | null;
      };
      const websocketInstances: TestWebSocketInstance[] = [];
      class TestWebSocket {
        static OPEN = 1;
        binaryType: BinaryType = "blob";
        readyState = TestWebSocket.OPEN;
        sentChunks: ArrayBuffer[] = [];
        close = vi.fn();
        send = vi.fn((chunk: ArrayBuffer) => {
          this.sentChunks.push(chunk);
        });
        onopen: (() => void) | null = null;
        onerror: (() => void) | null = null;
        onmessage: ((event: MessageEvent<string>) => void) | null = null;

        constructor() {
          websocketInstances.push(this);
        }
      }
      vi.stubGlobal("WebSocket", TestWebSocket);

      render(<App />);
      fireEvent.click(await screen.findByRole("tab", { name: "声音与语音" }));

      fireEvent.click(await screen.findByRole("button", { name: "管理说话人样本" }));
      fireEvent.click(await screen.findByRole("button", { name: "替换" }));
      await waitFor(() => {
        expect(websocketInstances.length).toBeGreaterThanOrEqual(2);
      });
      const recordingSocket = websocketInstances[websocketInstances.length - 1];
      await act(async () => {
        recordingSocket.onopen?.();
        audioProcessHandler?.({
          inputBuffer: {
            getChannelData: () => new Float32Array([0, 0.25, -0.25])
          }
        });
        recordingSocket.onmessage?.(
          new MessageEvent("message", {
            data: JSON.stringify({
              type: "completed",
              speech_seconds: 3,
              required_speech_seconds: 3,
              recording_id: "recording-1"
            })
          })
        );
      });

      await waitFor(() => {
        const uploadCall = fetchMock.mock.calls.find(([calledInput]) =>
          calledInput.toString().endsWith("/api/speaker-verification/samples")
        );
        expect(uploadCall).toBeTruthy();
        const formData = uploadCall?.[1]?.body as FormData;
        expect(formData.get("replace_sample_id")).toBe("sample-1");
        expect(formData.get("recording_id")).toBe("recording-1");
      });
    });

  it("后台配置返回完整设置快照后，快捷键保存使用提交点版本", async () => {
    const committedSettings = {
      ...createConsoleSettingsPayload(),
      settings_revision: 1,
      user_extra_notes: "并发更新后的资料"
    };
    const fetchMock = createConsoleFetchMock({
      backgroundAgentConfig: {
        environment: {
          ANTHROPIC_BASE_URL: "https://endpoint.example.com",
          ANTHROPIC_DEFAULT_OPUS_MODEL: "model-opus",
          ANTHROPIC_DEFAULT_SONNET_MODEL: "model-sonnet",
          ANTHROPIC_DEFAULT_HAIKU_MODEL: "model-haiku"
        },
        model: "deepseek-flash",
        effort_level: null,
        max_turns: 30,
        permission_mode: "acceptEdits",
        credential_type: "api_key",
        credential_configured: true,
        settings: committedSettings
      }
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<App />);
    fireEvent.click(await screen.findByRole("tab", { name: "AI 与服务" }));
    fireEvent.click(await screen.findByRole("button", { name: "保存后台工作配置" }));
    await waitFor(() => {
      expect(fetchMock.mock.calls.some(
        ([input, init]) => input.toString().endsWith("/api/background-agent/config") && init?.method === "PUT"
      )).toBe(true);
    });

    fireEvent.click(screen.getByRole("tab", { name: "常用" }));
    fireEvent.click(await screen.findByRole("button", { name: "录入快捷键" }));
    fireEvent.keyDown(screen.getByLabelText("快捷键录入区"), {
      key: "f",
      code: "KeyF",
      ctrlKey: true,
      altKey: true
    });
    fireEvent.click(screen.getByRole("button", { name: "使用此快捷键" }));

    await waitFor(() => {
      const settingsSave = fetchMock.mock.calls.find(
        ([input, init]) => input.toString().endsWith("/api/settings") && init?.method === "POST"
      );
      expect(settingsSave).toBeDefined();
      const requestBody = JSON.parse(String(settingsSave?.[1]?.body)) as Record<string, unknown>;
      expect(requestBody.expected_revision).toBe(1);
      expect(requestBody.user_extra_notes).toBe("并发更新后的资料");
    });
  });

  it("迟到的快捷键 HTTP 快照不会覆盖已到达的 WebSocket 状态", async () => {
    const websocketInstances = installConsoleControlWebSocketMock();
    const defaultFetch = createConsoleFetchMock();
    let resolveShortcutStatus!: (response: Response) => void;
    const delayedShortcutStatus = new Promise<Response>((resolve) => {
      resolveShortcutStatus = resolve;
    });
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      if (input.toString().endsWith("/api/settings/shortcut-status") && init?.method === undefined) {
        return delayedShortcutStatus;
      }
      return defaultFetch(input, init);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<App />);
    await waitFor(() => expect(websocketInstances).toHaveLength(1));
    const currentSettings = {
      ...createConsoleSettingsPayload(),
      settings_revision: 9
    };
    const currentShortcutStatus = {
      items: [
        ["followup_toggle", currentSettings.followup_toggle_shortcut, currentSettings.followup_toggle_shortcut_enabled],
        ["avatar_visibility", currentSettings.avatar_visibility_shortcut, currentSettings.avatar_visibility_shortcut_enabled],
        ["selection_capture", currentSettings.selection_capture_shortcut, currentSettings.selection_capture_shortcut_enabled]
      ].map(([shortcutId, accelerator, enabled]) => ({
        shortcut_id: shortcutId,
        accelerator,
        enabled,
        status: "registered",
        settings_revision: 9,
        checked_at: "2026-10-07T19:00:00+08:00",
        message: "快捷键可用"
      }))
    };
    act(() => {
      websocketInstances[0].emitMessage({
        type: "settings.changed",
        payload: currentSettings
      });
      websocketInstances[0].emitMessage({
        type: "settings.shortcut_status_changed",
        payload: currentShortcutStatus
      });
    });

    const staleResponse = await defaultFetch("http://localhost/api/settings/shortcut-status");
    resolveShortcutStatus(staleResponse);

    await waitFor(() => {
      expect(screen.getAllByText("可用")).toHaveLength(3);
    });
  });

  it("同一设置版本的迟到 HTTP 待检测状态不会覆盖 WebSocket 实时状态", async () => {
    const websocketInstances = installConsoleControlWebSocketMock();
    const defaultFetch = createConsoleFetchMock();
    let resolveShortcutStatus!: (response: Response) => void;
    const delayedShortcutStatus = new Promise<Response>((resolve) => {
      resolveShortcutStatus = resolve;
    });
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      if (input.toString().endsWith("/api/settings/shortcut-status") && init?.method === undefined) {
        return delayedShortcutStatus;
      }
      return defaultFetch(input, init);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<App />);
    await waitFor(() => expect(websocketInstances).toHaveLength(1));
    const currentSettings = {
      ...createConsoleSettingsPayload(),
      settings_revision: 9
    };
    const currentShortcutStatus = {
      items: [
        ["followup_toggle", currentSettings.followup_toggle_shortcut, currentSettings.followup_toggle_shortcut_enabled],
        ["avatar_visibility", currentSettings.avatar_visibility_shortcut, currentSettings.avatar_visibility_shortcut_enabled],
        ["selection_capture", currentSettings.selection_capture_shortcut, currentSettings.selection_capture_shortcut_enabled]
      ].map(([shortcutId, accelerator, enabled]) => ({
        shortcut_id: shortcutId,
        accelerator,
        enabled,
        status: "registered",
        settings_revision: 9,
        checked_at: "2026-10-07T19:00:00+08:00",
        message: "快捷键可用"
      }))
    };
    act(() => {
      websocketInstances[0].emitMessage({
        type: "settings.changed",
        payload: currentSettings
      });
      websocketInstances[0].emitMessage({
        type: "settings.shortcut_status_changed",
        payload: currentShortcutStatus
      });
    });

    const staleShortcutStatus = {
      items: currentShortcutStatus.items.map((item) => ({
        ...item,
        status: "unknown",
        checked_at: "",
        message: "等待桌面端检测"
      }))
    };
    resolveShortcutStatus(new Response(JSON.stringify(staleShortcutStatus), {
      headers: { "Content-Type": "application/json" }
    }));

    await waitFor(() => {
      expect(screen.getAllByText("可用")).toHaveLength(3);
    });
  });

  it("快捷键 WebSocket 旧版本事件不会覆盖新版本状态", async () => {
    const websocketInstances = installConsoleControlWebSocketMock();
    vi.stubGlobal("fetch", createConsoleFetchMock());

    render(<App />);
    await waitFor(() => expect(websocketInstances).toHaveLength(1));
    const currentSettings = {
      ...createConsoleSettingsPayload(),
      settings_revision: 9
    };
    const currentShortcutStatus = {
      items: [
        ["followup_toggle", currentSettings.followup_toggle_shortcut, currentSettings.followup_toggle_shortcut_enabled],
        ["avatar_visibility", currentSettings.avatar_visibility_shortcut, currentSettings.avatar_visibility_shortcut_enabled],
        ["selection_capture", currentSettings.selection_capture_shortcut, currentSettings.selection_capture_shortcut_enabled]
      ].map(([shortcutId, accelerator, enabled]) => ({
        shortcut_id: shortcutId,
        accelerator,
        enabled,
        status: "registered",
        settings_revision: 9,
        checked_at: "2026-10-07T19:00:00+08:00",
        message: "快捷键可用"
      }))
    };
    const staleShortcutStatus = {
      items: currentShortcutStatus.items.map((item) => ({
        ...item,
        status: "unknown",
        settings_revision: 8,
        checked_at: "2026-10-07T18:59:00+08:00",
        message: "等待桌面端检测"
      }))
    };
    act(() => {
      websocketInstances[0].emitMessage({
        type: "settings.changed",
        payload: currentSettings
      });
      websocketInstances[0].emitMessage({
        type: "settings.shortcut_status_changed",
        payload: currentShortcutStatus
      });
      websocketInstances[0].emitMessage({
        type: "settings.shortcut_status_changed",
        payload: staleShortcutStatus
      });
    });

    await waitFor(() => {
      expect(screen.getAllByText("可用")).toHaveLength(3);
    });
  });
});
