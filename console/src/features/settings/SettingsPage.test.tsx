import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { App } from "../../App";
import { SettingsPage } from "./SettingsPage";
import type { LlmConfig, SupportedLlmModelsResponse } from "../../api/contracts/llm";
import {
  createBootstrapStatusPayload,
  createConsoleFetchMock,
  createConsoleSettingsPayload,
  createLlmConfigPayload,
  createSupportedLlmModelsPayload,
  resetConsoleAppTestEnvironment
} from "../../test/consoleAppTestHarness";

vi.mock("../runtime-initialization/RuntimeInitializationGate", () => ({ RuntimeInitializationGate: ({ children }: { children: import("react").ReactNode }) => children }));
afterEach(resetConsoleAppTestEnvironment);

describe("设置分类", () => {
  it("把独立后端基址传给所有设置子面板", async () => {
    const backendBaseUrl = "http://127.0.0.1:8765";
    const fetchMock = createConsoleFetchMock();
    vi.stubGlobal("fetch", fetchMock);

    render(
      <SettingsPage
        audioDeviceApplyResult={null}
        baseUrl={backendBaseUrl}
        bootstrapSaveErrorMessage={null}
        bootstrapStatus={createBootstrapStatusPayload(true)}
        isSaving={false}
        isSavingBootstrap={false}
        llmConfig={createLlmConfigPayload(true) as LlmConfig}
        onBootstrapSubmit={async () => true}
        onLlmConfigSubmit={async () => true}
        onQWeatherSubmit={async () => true}
        onSave={async () => undefined}
        onSettingsSnapshotRefresh={async () => undefined}
        saveErrorMessage={null}
        settings={createConsoleSettingsPayload()}
        shortcutStatus={null}
        supportedLlmModels={createSupportedLlmModelsPayload() as SupportedLlmModelsResponse}
      />
    );

    await waitFor(() => {
      const requestUrls = fetchMock.mock.calls.map(([input]) => input.toString());
      expect(requestUrls).toEqual(expect.arrayContaining([
        `${backendBaseUrl}/api/audio/devices`,
        `${backendBaseUrl}/api/background-agent/config`,
        `${backendBaseUrl}/api/speaker-verification/profile`,
        `${backendBaseUrl}/api/tool-waiting-policies`
      ]));
    });
  });

  it("切换分类保留个人草稿，而且当前分类之外的控件不会进入可访问树", async () => {
    const fetchMock = createConsoleFetchMock();
    vi.stubGlobal("fetch", fetchMock);
    render(<App />);
    const profileTab = await screen.findByRole("tab", { name: "我的信息" });
    expect(screen.queryByRole("button", { name: "保存我的信息" })).toBeNull();
    fireEvent.click(profileTab);
    fireEvent.change(screen.getByLabelText("自我简介"), { target: { value: "保留这份未保存的草稿" } });
    fireEvent.click(screen.getByRole("tab", { name: "声音与语音" }));
    expect(screen.queryByRole("textbox", { name: "自我简介" })).toBeNull();
    fireEvent.click(profileTab);
    expect((screen.getByLabelText("自我简介") as HTMLTextAreaElement).value).toBe("保留这份未保存的草稿");
    expect(fetchMock.mock.calls.some(([, init]) => init?.method === "POST")).toBe(false);
  });

  it("支持方向键和 Home/End 切换分类，并同步焦点与可见面板", async () => {
    vi.stubGlobal("fetch", createConsoleFetchMock());
    render(<App />);
    const general = await screen.findByRole("tab", { name: "常用" });
    fireEvent.keyDown(general, { key: "ArrowRight" });
    expect(document.activeElement).toBe(screen.getByRole("tab", { name: "声音与语音" }));
    expect(screen.getByRole("tabpanel").getAttribute("aria-labelledby")).toBe("settings-tab-voice");
    fireEvent.keyDown(document.activeElement!, { key: "End" });
    await waitFor(() => expect(screen.getByRole("tabpanel").getAttribute("aria-labelledby")).toBe("settings-tab-profile"));
    fireEvent.keyDown(document.activeElement!, { key: "Home" });
    expect(document.activeElement).toBe(general);
  });
});
