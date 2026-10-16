import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { SpeakerVerificationProfile } from "../../api/contracts/speakerVerification";
import { createSpeakerVerificationSample, deleteSpeakerVerificationSample, fetchSpeakerVerificationProfile, updateSpeakerVerificationProfile } from "../../api/services/speakerVerification";
import { SpeakerVerificationSettingsForm } from "./SpeakerVerificationSettingsForm";

vi.mock("../../api/services/speakerVerification", () => ({
  createSpeakerVerificationSample: vi.fn(),
  deleteSpeakerVerificationSample: vi.fn(),
  fetchSpeakerVerificationProfile: vi.fn(),
  updateSpeakerVerificationProfile: vi.fn()
}));

const profile: SpeakerVerificationProfile = {
  enabled: false,
  samples: [{ id: "sample-1", audio_url: "/sample.wav", created_at: 100 }],
  max_sample_count: 3,
  required_speech_seconds: 3,
  can_enable: true
};

const originalMediaDevices = navigator.mediaDevices;

function renderPanel(onSettingsSnapshotRefresh?: () => Promise<unknown>): void {
  render(
    <SpeakerVerificationSettingsForm
      baseUrl="http://localhost"
      isSavingSettings={false}
      onSettingsSnapshotRefresh={onSettingsSnapshotRefresh}
    />
  );
}

function installRecordingDevices() {
  const stop = vi.fn();
  const closeAudioContext = vi.fn(async () => undefined);
  Object.defineProperty(navigator, "mediaDevices", {
    configurable: true,
    value: { getUserMedia: vi.fn(async () => ({ getTracks: () => [{ stop }] })) }
  });
  vi.stubGlobal("AudioContext", vi.fn(() => ({
    sampleRate: 16000,
    destination: {},
    createMediaStreamSource: () => ({ connect: vi.fn(), disconnect: vi.fn() }),
    createScriptProcessor: () => ({ connect: vi.fn(), disconnect: vi.fn(), onaudioprocess: null }),
    close: closeAudioContext
  })));
  const sockets: RecordingSocket[] = [];
  class RecordingSocket {
    static OPEN = 1;
    readyState = 1;
    binaryType = "blob";
    onopen: (() => void) | null = null;
    onmessage: ((event: { data: string }) => void) | null = null;
    onerror: (() => void) | null = null;
    onclose: (() => void) | null = null;
    send = vi.fn();
    close = vi.fn(() => { this.onclose?.(); });
    constructor() { sockets.push(this); }
  }
  vi.stubGlobal("WebSocket", RecordingSocket);
  return { sockets, stop, closeAudioContext };
}

describe("SpeakerVerificationSettingsForm", () => {
  beforeEach(() => vi.mocked(fetchSpeakerVerificationProfile).mockResolvedValue(profile));
  afterEach(() => {
    vi.resetAllMocks();
    vi.unstubAllGlobals();
    Object.defineProperty(navigator, "mediaDevices", { configurable: true, value: originalMediaDevices });
  });

  it("开启请求未返回时禁用重复写入和录音", async () => {
    let finishSave!: (value: SpeakerVerificationProfile) => void;
    vi.mocked(updateSpeakerVerificationProfile).mockReturnValueOnce(new Promise((resolve) => { finishSave = resolve; }));
    renderPanel();
    const toggle = await screen.findByRole("switch", { name: "开启说话人确认" });
    fireEvent.click(toggle);
    fireEvent.click(toggle);

    expect(vi.mocked(updateSpeakerVerificationProfile)).toHaveBeenCalledTimes(1);
    expect((screen.getByRole("button", { name: "开始录制" }) as HTMLButtonElement).disabled).toBe(true);
    await act(async () => finishSave({ ...profile, enabled: true }));
    expect(screen.getByRole("switch", { name: "关闭说话人确认" }).getAttribute("aria-checked")).toBe("true");
  });

  it("说话人开关保存成功后请求同步普通设置版本", async () => {
    const refreshSettingsSnapshot = vi.fn(async () => undefined);
    vi.mocked(updateSpeakerVerificationProfile).mockResolvedValueOnce({ ...profile, enabled: true });
    renderPanel(refreshSettingsSnapshot);

    fireEvent.click(await screen.findByRole("switch", { name: "开启说话人确认" }));

    await waitFor(() => expect(refreshSettingsSnapshot).toHaveBeenCalledOnce());
  });

  it("删除未完成时禁止再次删除同一录音", async () => {
    let finishDelete!: (value: SpeakerVerificationProfile) => void;
    vi.mocked(deleteSpeakerVerificationSample).mockReturnValueOnce(new Promise((resolve) => { finishDelete = resolve; }));
    renderPanel();
    fireEvent.click(await screen.findByRole("button", { name: "管理说话人样本" }));
    const deleteButton = screen.getByRole("button", { name: "删除 sample-1" });
    fireEvent.click(deleteButton);
    fireEvent.click(deleteButton);
    expect(vi.mocked(deleteSpeakerVerificationSample)).toHaveBeenCalledTimes(1);
    await act(async () => finishDelete({ ...profile, samples: [], can_enable: false }));
    expect(screen.queryByRole("button", { name: "删除 sample-1" })).toBeNull();
  });

  it("等待麦克风许可期间可取消，迟到的媒体流会立即停止", async () => {
    let finishPermission!: (value: MediaStream) => void;
    const stop = vi.fn();
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: { getUserMedia: vi.fn(() => new Promise((resolve) => { finishPermission = resolve; })) }
    });
    const audioContext = vi.fn();
    vi.stubGlobal("AudioContext", audioContext);
    renderPanel();
    fireEvent.click(await screen.findByRole("button", { name: "开始录制" }));
    fireEvent.click(screen.getByRole("button", { name: "取消录音" }));
    await act(async () => finishPermission({ getTracks: () => [{ stop }] } as unknown as MediaStream));

    expect(stop).toHaveBeenCalledOnce();
    expect(audioContext).not.toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: "取消录音" })).toBeNull();
    expect((screen.getByRole("button", { name: "开始录制" }) as HTMLButtonElement).disabled).toBe(false);
  });

  it("取消进行中的录音会释放设备和连接，迟到完成事件不会保存样本", async () => {
    const { sockets, stop, closeAudioContext } = installRecordingDevices();
    renderPanel();
    fireEvent.click(await screen.findByRole("button", { name: "开始录制" }));
    await waitFor(() => expect(sockets).toHaveLength(1));
    fireEvent.click(screen.getByRole("button", { name: "取消录音" }));
    await act(async () => sockets[0].onmessage?.({ data: JSON.stringify({ type: "completed", recording_id: "late-recording" }) }));

    expect(stop).toHaveBeenCalledOnce();
    expect(closeAudioContext).toHaveBeenCalledOnce();
    expect(sockets[0].close).toHaveBeenCalledOnce();
    expect(vi.mocked(createSpeakerVerificationSample)).not.toHaveBeenCalled();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("录音连接断开后释放设备并允许重新录制", async () => {
    const { sockets, stop } = installRecordingDevices();
    renderPanel();
    fireEvent.click(await screen.findByRole("button", { name: "开始录制" }));
    await waitFor(() => expect(sockets).toHaveLength(1));
    act(() => sockets[0].onclose?.());

    expect(screen.getByText(/录音连接已断开/)).toBeTruthy();
    expect(stop).toHaveBeenCalledOnce();
    expect(screen.queryByRole("button", { name: "取消录音" })).toBeNull();
    expect((screen.getByRole("button", { name: "开始录制" }) as HTMLButtonElement).disabled).toBe(false);
  });

  it("状态加载失败提供重试而不继续显示读取中", async () => {
    vi.mocked(fetchSpeakerVerificationProfile).mockRejectedValueOnce(new Error("说话人服务暂不可用"));
    renderPanel();
    await screen.findByText(/说话人服务暂不可用/);
    expect(screen.queryByText("正在读取说话人确认状态。")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "开始录制" })).toBeTruthy());
  });
});
