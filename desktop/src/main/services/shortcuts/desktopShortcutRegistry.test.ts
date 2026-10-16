import { describe, expect, it, vi } from "vitest";

import type { DesktopRuntimeSettingsSnapshot } from "../controlSettingsSync";
import { createDesktopShortcutRegistryService } from "./desktopShortcutRegistry";

function createSettingsSyncHarness() {
  let listener: ((settings: DesktopRuntimeSettingsSnapshot) => void) | null = null;
  const unsubscribe = vi.fn();
  return {
    settingsSync: {
      subscribe: vi.fn((nextListener: (settings: DesktopRuntimeSettingsSnapshot) => void) => {
        listener = nextListener;
        return unsubscribe;
      })
    },
    emit(settings: DesktopRuntimeSettingsSnapshot): void {
      if (listener === null) {
        throw new Error("settings listener was not registered");
      }
      listener(settings);
    },
    unsubscribe
  };
}

function snapshot(input: {
  revision?: number;
  followupAccelerator?: string;
  avatarAccelerator?: string;
  selectionAccelerator?: string;
  selectionEnabled?: boolean;
} = {}): DesktopRuntimeSettingsSnapshot {
  return {
    revision: input.revision ?? 0,
    followupShortcut: {
      enabled: true,
      accelerator: input.followupAccelerator ?? "CommandOrControl+Alt+Space"
    },
    avatarVisibilityShortcut: {
      enabled: true,
      accelerator: input.avatarAccelerator ?? "CommandOrControl+Alt+P"
    },
    selectionCaptureShortcut: {
      enabled: input.selectionEnabled ?? true,
      accelerator: input.selectionAccelerator ?? "CommandOrControl+Alt+C"
    }
  };
}

function createHarness(input?: {
  register?: (accelerator: string, callback: () => void) => boolean;
}) {
  const settingsHarness = createSettingsSyncHarness();
  const register = vi.fn(input?.register ?? (() => true));
  const unregister = vi.fn();
  const reportShortcutStatus = vi.fn(async () => undefined);
  const actions = {
    toggleFollowup: vi.fn(async () => undefined),
    toggleAvatarVisibility: vi.fn(),
    captureSelection: vi.fn(async () => undefined)
  };
  const onShortcutPressed = vi.fn();
  const service = createDesktopShortcutRegistryService({
    globalShortcut: { register, unregister },
    settingsSync: settingsHarness.settingsSync,
    statusClient: { reportShortcutStatus },
    actions,
    onShortcutPressed,
    now: () => "2026-07-09T14:00:00+08:00",
    logger: { info: vi.fn(), warn: vi.fn() }
  });

  return { ...settingsHarness, service, register, unregister, reportShortcutStatus, actions, onShortcutPressed };
}

describe("createDesktopShortcutRegistryService", () => {
  it("只在收到三个真实快捷键时先反馈，再执行动作", async () => {
    const harness = createHarness();
    await harness.service.start();
    harness.emit(snapshot());
    expect(harness.onShortcutPressed).not.toHaveBeenCalled();
    harness.register.mock.calls.forEach(([, press]) => press());
    expect(harness.onShortcutPressed.mock.calls).toEqual([
      ["followup_toggle"], ["avatar_visibility"], ["selection_capture"]
    ]);
    expect(harness.onShortcutPressed.mock.invocationCallOrder[0]).toBeLessThan(
      harness.actions.toggleFollowup.mock.invocationCallOrder[0]
    );
  });
  it("注册三个可用快捷键并上报 registered 状态", async () => {
    const harness = createHarness();

    await harness.service.start();
    harness.emit(snapshot({ revision: 7 }));
    await Promise.resolve();

    expect(harness.register).toHaveBeenCalledTimes(3);
    expect(harness.reportShortcutStatus).toHaveBeenCalledWith({
      settings_revision: 7,
      items: expect.arrayContaining([
        expect.objectContaining({ shortcut_id: "followup_toggle", status: "registered" }),
        expect.objectContaining({ shortcut_id: "avatar_visibility", status: "registered" }),
        expect.objectContaining({ shortcut_id: "selection_capture", status: "registered" })
      ])
    });
  });

  it("注册失败时上报 unavailable，不识别具体占用应用", async () => {
    const harness = createHarness({
      register: (accelerator) => accelerator !== "CommandOrControl+Alt+C"
    });

    await harness.service.start();
    harness.emit(snapshot());
    await Promise.resolve();

    expect(harness.reportShortcutStatus).toHaveBeenCalledWith({
      settings_revision: 0,
      items: expect.arrayContaining([
        expect.objectContaining({
          shortcut_id: "selection_capture",
          status: "unavailable",
          message: "可能被系统或其他应用占用"
        })
      ])
    });
  });

  it("NearMate 内部重复快捷键会标记 internal_conflict 且不注册重复项", async () => {
    const harness = createHarness();

    await harness.service.start();
    harness.emit(snapshot({
      followupAccelerator: "CommandOrControl+Alt+F",
      avatarAccelerator: "CommandOrControl+Alt+F"
    }));
    await Promise.resolve();

    expect(harness.register).toHaveBeenCalledTimes(1);
    expect(harness.register).toHaveBeenCalledWith("CommandOrControl+Alt+C", expect.any(Function));
    expect(harness.reportShortcutStatus).toHaveBeenCalledWith({
      settings_revision: 0,
      items: expect.arrayContaining([
        expect.objectContaining({ shortcut_id: "followup_toggle", status: "internal_conflict" }),
        expect.objectContaining({ shortcut_id: "avatar_visibility", status: "internal_conflict" }),
        expect.objectContaining({ shortcut_id: "selection_capture", status: "registered" })
      ])
    });
  });

  it("设置变更时注销旧快捷键并按新设置重新注册", async () => {
    const harness = createHarness();

    await harness.service.start();
    harness.emit(snapshot({ revision: 0 }));
    harness.emit(snapshot({ revision: 1, selectionAccelerator: "CommandOrControl+Alt+X" }));

    expect(harness.unregister).toHaveBeenCalledWith("CommandOrControl+Alt+Space");
    expect(harness.unregister).toHaveBeenCalledWith("CommandOrControl+Alt+P");
    expect(harness.unregister).toHaveBeenCalledWith("CommandOrControl+Alt+C");
    expect(harness.register).toHaveBeenLastCalledWith("CommandOrControl+Alt+X", expect.any(Function));
  });

  it("只有设置 revision 变化时复用已注册快捷键并重新上报当前版本", async () => {
    const harness = createHarness();

    await harness.service.start();
    harness.emit(snapshot({ revision: 0 }));
    await Promise.resolve();
    harness.emit(snapshot({ revision: 1 }));
    await Promise.resolve();

    expect(harness.register).toHaveBeenCalledTimes(3);
    expect(harness.unregister).not.toHaveBeenCalled();
    expect(harness.reportShortcutStatus).toHaveBeenCalledTimes(2);
    expect(harness.reportShortcutStatus).toHaveBeenLastCalledWith(expect.objectContaining({
      settings_revision: 1,
      items: expect.arrayContaining([
        expect.objectContaining({ shortcut_id: "followup_toggle", settings_revision: 1 }),
        expect.objectContaining({ shortcut_id: "avatar_visibility", settings_revision: 1 }),
        expect.objectContaining({ shortcut_id: "selection_capture", settings_revision: 1 })
      ])
    }));
  });

  it("停止服务时注销所有已注册快捷键并取消订阅", async () => {
    const harness = createHarness();

    await harness.service.start();
    harness.emit(snapshot());
    harness.service.stop();

    expect(harness.unregister).toHaveBeenCalledWith("CommandOrControl+Alt+Space");
    expect(harness.unregister).toHaveBeenCalledWith("CommandOrControl+Alt+P");
    expect(harness.unregister).toHaveBeenCalledWith("CommandOrControl+Alt+C");
    expect(harness.unsubscribe).toHaveBeenCalledOnce();
  });
});
