import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { AudioDevicesPayload } from "../../api/contracts/audioDevices";
import type { ConsoleSettings } from "../../api/contracts/settings";
import { fetchAudioDevices, updateAudioDeviceSettings } from "../../api/services/audioDevices";
import { fetchSettings } from "../../api/services/settings";
import { createConsoleSettingsPayload } from "../../test/consoleAppTestHarness";
import { AudioDeviceSettingsPanel } from "./AudioDeviceSettingsPanel";

vi.mock("../../api/services/audioDevices", () => ({
  fetchAudioDevices: vi.fn(),
  testAudioDevice: vi.fn(),
  updateAudioDeviceSettings: vi.fn()
}));

vi.mock("../../api/services/settings", () => ({
  fetchSettings: vi.fn()
}));

const devices: AudioDevicesPayload = {
  microphones: [{ id: "mic-1", name: "USB 麦克风", is_default: true, status: "available", error_type: null, error_message: null }],
  speaker_loopbacks: [],
  selected: { mode: "system_default", microphone_id: null, microphone_name: null, speaker_loopback_id: null, speaker_loopback_name: null }
};

function renderPanel(): void {
  render(<AudioDeviceSettingsPanel applyResult={null} baseUrl="http://localhost" onSaved={vi.fn()} settings={createConsoleSettingsPayload() as ConsoleSettings} />);
}

describe("AudioDeviceSettingsPanel", () => {
  beforeEach(() => vi.mocked(fetchAudioDevices).mockResolvedValue(devices));
  afterEach(() => vi.resetAllMocks());

  it("设备读取失败提供重试，并在成功后恢复设备选择", async () => {
    vi.mocked(fetchAudioDevices).mockRejectedValueOnce(new Error("设备服务未就绪"));
    renderPanel();

    expect(await screen.findByText("设备服务未就绪")).toBeTruthy();
    expect((screen.getByRole("button", { name: "开始 3 秒测试" }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "重新读取设备" }));
    await waitFor(() => expect((screen.getByLabelText("麦克风") as HTMLSelectElement).value).toBe("mic-1"));
    expect(screen.queryByText("设备服务未就绪")).toBeNull();
    expect(vi.mocked(fetchAudioDevices)).toHaveBeenCalledTimes(2);
  });

  it("刷新设备保留尚未保存的手动选择，并显示新增设备", async () => {
    renderPanel();
    await waitFor(() => expect((screen.getByLabelText("麦克风") as HTMLSelectElement).value).toBe("mic-1"));
    fireEvent.change(screen.getByLabelText("音频设备选择模式"), { target: { value: "manual" } });
    fireEvent.change(screen.getByLabelText("麦克风"), { target: { value: "mic-1" } });
    vi.mocked(fetchAudioDevices).mockResolvedValueOnce({
      ...devices,
      microphones: [...devices.microphones, { ...devices.microphones[0], id: "mic-2", name: "新麦克风", is_default: false }]
    });

    fireEvent.click(screen.getByRole("button", { name: "刷新设备" }));
    await screen.findByRole("option", { name: /新麦克风/ });
    expect((screen.getByLabelText("音频设备选择模式") as HTMLSelectElement).value).toBe("manual");
    expect((screen.getByLabelText("麦克风") as HTMLSelectElement).value).toBe("mic-1");
  });

  it("采用音频提交返回的权威设置版本，避免后续快捷键携带旧 revision", async () => {
    const onSaved = vi.fn();
    const committedSettings = {
      ...createConsoleSettingsPayload(),
      settings_revision: 1,
      audio_device_mode: "manual" as const,
      audio_input_device_id: "mic-1",
      audio_input_device_name: "USB 麦克风"
    } as ConsoleSettings;
    vi.mocked(updateAudioDeviceSettings).mockResolvedValue({
      accepted: true,
      request_id: "audio-change-1",
      settings: committedSettings
    });
    render(
      <AudioDeviceSettingsPanel
        applyResult={null}
        baseUrl="http://localhost"
        onSaved={onSaved}
        settings={createConsoleSettingsPayload() as ConsoleSettings}
      />
    );

    await waitFor(() => expect((screen.getByLabelText("麦克风") as HTMLSelectElement).value).toBe("mic-1"));
    fireEvent.change(screen.getByLabelText("音频设备选择模式"), { target: { value: "manual" } });
    fireEvent.change(screen.getByLabelText("麦克风"), { target: { value: "mic-1" } });
    fireEvent.click(screen.getByRole("button", { name: "更改音频设备" }));

    await waitFor(() => expect(onSaved).toHaveBeenCalledWith(committedSettings));
    expect(fetchSettings).not.toHaveBeenCalled();
  });

  it("旧 Backend 未返回快照时主动读取最新 revision", async () => {
    const onSaved = vi.fn();
    const committedSettings = {
      ...createConsoleSettingsPayload(),
      settings_revision: 1,
      audio_device_mode: "manual" as const,
      audio_input_device_id: "mic-1",
      audio_input_device_name: "USB 麦克风"
    } as ConsoleSettings;
    vi.mocked(updateAudioDeviceSettings).mockResolvedValue({ accepted: true, request_id: "audio-change-2" });
    vi.mocked(fetchSettings).mockResolvedValue(committedSettings);
    render(
      <AudioDeviceSettingsPanel
        applyResult={null}
        baseUrl="http://localhost"
        onSaved={onSaved}
        settings={createConsoleSettingsPayload() as ConsoleSettings}
      />
    );

    await waitFor(() => expect((screen.getByLabelText("麦克风") as HTMLSelectElement).value).toBe("mic-1"));
    fireEvent.change(screen.getByLabelText("音频设备选择模式"), { target: { value: "manual" } });
    fireEvent.change(screen.getByLabelText("麦克风"), { target: { value: "mic-1" } });
    fireEvent.click(screen.getByRole("button", { name: "更改音频设备" }));

    await waitFor(() => expect(onSaved).toHaveBeenCalledWith(committedSettings));
    expect(fetchSettings).toHaveBeenCalledWith("http://localhost");
  });

  it("保存已成功但旧 Backend 的快照回读失败时不误报设备写入失败", async () => {
    const onSaved = vi.fn();
    vi.mocked(updateAudioDeviceSettings).mockResolvedValue({ accepted: true, request_id: "audio-change-3" });
    vi.mocked(fetchSettings).mockRejectedValue(new Error("设置接口暂时不可用"));
    render(
      <AudioDeviceSettingsPanel
        applyResult={null}
        baseUrl="http://localhost"
        onSaved={onSaved}
        settings={createConsoleSettingsPayload() as ConsoleSettings}
      />
    );

    await waitFor(() => expect((screen.getByLabelText("麦克风") as HTMLSelectElement).value).toBe("mic-1"));
    fireEvent.change(screen.getByLabelText("音频设备选择模式"), { target: { value: "manual" } });
    fireEvent.change(screen.getByLabelText("麦克风"), { target: { value: "mic-1" } });
    fireEvent.click(screen.getByRole("button", { name: "更改音频设备" }));

    expect(await screen.findByText(/已保存，但页面状态刷新失败：设置接口暂时不可用/)).toBeTruthy();
    expect(onSaved).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "应用中" })).toBeTruthy();
  });
});
