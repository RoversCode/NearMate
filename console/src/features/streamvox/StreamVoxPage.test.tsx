import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { createBootstrapStatusPayload, createConsoleSettingsPayload } from "../../test/consoleAppTestHarness";
import { StreamVoxPage } from "./StreamVoxPage";

vi.mock("../bootstrap/TtsSetupPanel", () => ({
  TtsSetupPanel: () => <section aria-label="模型管理">模型管理</section>
}));

const actions = {
  onBootstrapStatusChange: vi.fn(), onSaveConfig: vi.fn(), onCreateRole: vi.fn(),
  onPreviewRole: vi.fn(), onDeleteRole: vi.fn(), onCaptureCurrentVoice: vi.fn()
};

describe("StreamVox 管理页", () => {
  it("设置或运行状态尚未载入时仍提供模型管理", () => {
    render(<StreamVoxPage baseUrl="http://localhost" bootstrapStatus={createBootstrapStatusPayload(true)}
      settings={null} runtime={null} isSaving={false} errorMessage={null} {...actions} />);

    expect(screen.getByRole("region", { name: "模型管理" })).toBeTruthy();
    expect(screen.getByText("StreamVox 设置尚未可用。")).toBeTruthy();
  });

  it("语音准备时说明当前状态，并暂停依赖当前语音的操作", () => {
    const bootstrapStatus = {
      ...createBootstrapStatusPayload(true), is_initialized: false,
      voice: { ...createBootstrapStatusPayload(true).voice, is_ready: false, phase: "load" }
    };
    render(<StreamVoxPage baseUrl="http://localhost" bootstrapStatus={bootstrapStatus}
      settings={{ ...createConsoleSettingsPayload(), audio_device_mode: "system_default" }} runtime={null} isSaving={false} errorMessage={null} {...actions} />);

    expect(screen.getByRole("status").textContent).toContain("语音正在准备");
    expect((screen.getByLabelText("新音色名称") as HTMLInputElement).disabled).toBe(true);
    expect(screen.getByRole("region", { name: "模型管理" })).toBeTruthy();
  });

  it("写入成功但附属状态刷新失败时展示提醒而不是操作失败", () => {
    render(<StreamVoxPage baseUrl="http://localhost" bootstrapStatus={createBootstrapStatusPayload(true)}
      settings={{ ...createConsoleSettingsPayload(), audio_device_mode: "system_default" }} runtime={null} isSaving={false}
      errorMessage={null} warningMessage="已保存，但页面状态刷新失败：StreamVox 状态暂时不可用" {...actions} />);

    expect(screen.getByRole("status").textContent).toContain("已保存，但页面状态刷新失败");
    expect(screen.queryByText("StreamVox 操作失败：已保存，但页面状态刷新失败：StreamVox 状态暂时不可用")).toBeNull();
  });
});
