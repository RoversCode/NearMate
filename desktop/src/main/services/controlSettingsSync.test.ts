import { describe, expect, it, vi } from "vitest";

import {
  createControlSettingsSyncService,
  type ControlSettingsSocketHandlers
} from "./controlSettingsSync";

type FakeSocket = {
  url: string;
  close: ReturnType<typeof vi.fn>;
  handlers: ControlSettingsSocketHandlers;
};

function createFakeSocketHarness() {
  const sockets: FakeSocket[] = [];
  const socketFactory = vi.fn((url: string, handlers: ControlSettingsSocketHandlers) => {
    const socket: FakeSocket = {
      url,
      close: vi.fn(),
      handlers
    };
    sockets.push(socket);
    return {
      close: socket.close
    };
  });

  return {
    sockets,
    socketFactory
  };
}

function emitSettingsChanged(socket: FakeSocket, payload: Record<string, unknown>): void {
  socket.handlers.onMessage(
    JSON.stringify({
      type: "settings.changed",
      payload
    })
  );
}

function settingsPayload(input: {
  revision: number;
  followupEnabled?: boolean;
  followupAccelerator?: string;
  avatarVisibilityEnabled?: boolean;
  avatarVisibilityAccelerator?: string;
  selectionCaptureEnabled?: boolean;
  selectionCaptureAccelerator?: string;
}): Record<string, unknown> {
  return {
    settings_revision: input.revision,
    followup_toggle_shortcut_enabled: input.followupEnabled ?? true,
    followup_toggle_shortcut: input.followupAccelerator ?? "CommandOrControl+Alt+Space",
    avatar_visibility_shortcut_enabled: input.avatarVisibilityEnabled ?? true,
    avatar_visibility_shortcut: input.avatarVisibilityAccelerator ?? "CommandOrControl+Alt+P",
    selection_capture_shortcut_enabled: input.selectionCaptureEnabled ?? true,
    selection_capture_shortcut: input.selectionCaptureAccelerator ?? "CommandOrControl+Alt+C"
  };
}

function createLoggerMock() {
  return {
    info: vi.fn(),
    warn: vi.fn()
  };
}

describe("createControlSettingsSyncService", () => {
  it("启动后只建立控制 WebSocket，等待 settings.changed 初始快照", async () => {
    const harness = createFakeSocketHarness();
    const listener = vi.fn();
    const service = createControlSettingsSyncService({
      httpBaseUrl: "http://127.0.0.1:8765",
      socketFactory: harness.socketFactory,
      logger: createLoggerMock()
    });

    service.subscribe(listener);
    await service.start();

    expect(harness.socketFactory).toHaveBeenCalledWith("ws://127.0.0.1:8765/ws/control", expect.any(Object));
    expect(listener).not.toHaveBeenCalled();
  });

  it("收到 settings.changed 初始快照后推送桌面快捷键设置", async () => {
    const harness = createFakeSocketHarness();
    const listener = vi.fn();
    const service = createControlSettingsSyncService({
      httpBaseUrl: "http://127.0.0.1:8765",
      socketFactory: harness.socketFactory,
      logger: createLoggerMock()
    });

    service.subscribe(listener);
    await service.start();
    emitSettingsChanged(harness.sockets[0], settingsPayload({ revision: 0 }));

    expect(listener).toHaveBeenCalledWith({
      revision: 0,
      followupShortcut: {
        enabled: true,
        accelerator: "CommandOrControl+Alt+Space"
      },
      avatarVisibilityShortcut: {
        enabled: true,
        accelerator: "CommandOrControl+Alt+P"
      },
      selectionCaptureShortcut: {
        enabled: true,
        accelerator: "CommandOrControl+Alt+C"
      }
    });
  });

  it("接受 JavaScript 安全整数上限的设置版本", async () => {
    const harness = createFakeSocketHarness();
    const listener = vi.fn();
    const service = createControlSettingsSyncService({
      httpBaseUrl: "http://127.0.0.1:8765",
      socketFactory: harness.socketFactory,
      logger: createLoggerMock()
    });

    service.subscribe(listener);
    await service.start();
    emitSettingsChanged(
      harness.sockets[0],
      settingsPayload({ revision: Number.MAX_SAFE_INTEGER })
    );

    expect(listener).toHaveBeenCalledWith(expect.objectContaining({
      revision: Number.MAX_SAFE_INTEGER
    }));
  });

  it("收到保存后的 settings.changed 快照后按最新 payload 通知", async () => {
    const harness = createFakeSocketHarness();
    const listener = vi.fn();
    const service = createControlSettingsSyncService({
      httpBaseUrl: "http://127.0.0.1:8765",
      socketFactory: harness.socketFactory,
      logger: createLoggerMock()
    });

    service.subscribe(listener);
    await service.start();
    emitSettingsChanged(harness.sockets[0], settingsPayload({ revision: 0 }));
    emitSettingsChanged(
      harness.sockets[0],
      settingsPayload({
        revision: 1,
        followupAccelerator: "CommandOrControl+Alt+F",
        avatarVisibilityAccelerator: "CommandOrControl+Alt+V",
        selectionCaptureAccelerator: "CommandOrControl+Alt+S"
      })
    );

    expect(listener).toHaveBeenCalledTimes(2);
    expect(listener).toHaveBeenLastCalledWith({
      revision: 1,
      followupShortcut: {
        enabled: true,
        accelerator: "CommandOrControl+Alt+F"
      },
      avatarVisibilityShortcut: {
        enabled: true,
        accelerator: "CommandOrControl+Alt+V"
      },
      selectionCaptureShortcut: {
        enabled: true,
        accelerator: "CommandOrControl+Alt+S"
      }
    });
  });

  it("WebSocket 重连后依靠后端初始 settings.changed 补齐断线期间变化", async () => {
    vi.useFakeTimers();
    const harness = createFakeSocketHarness();
    const listener = vi.fn();
    const service = createControlSettingsSyncService({
      httpBaseUrl: "http://127.0.0.1:8765",
      socketFactory: harness.socketFactory,
      reconnectDelayMs: 1000,
      random: () => 0.5,
      logger: createLoggerMock()
    });

    try {
      service.subscribe(listener);
      await service.start();
      emitSettingsChanged(harness.sockets[0], settingsPayload({ revision: 0 }));
      harness.sockets[0].handlers.onClose();
      await vi.advanceTimersByTimeAsync(1000);
      emitSettingsChanged(harness.sockets[1], settingsPayload({ revision: 1, followupEnabled: false }));
    } finally {
      service.stop();
      vi.useRealTimers();
    }

    expect(harness.sockets).toHaveLength(2);
    expect(listener).toHaveBeenLastCalledWith({
      revision: 1,
      followupShortcut: {
        enabled: false,
        accelerator: "CommandOrControl+Alt+Space"
      },
      avatarVisibilityShortcut: {
        enabled: true,
        accelerator: "CommandOrControl+Alt+P"
      },
      selectionCaptureShortcut: {
        enabled: true,
        accelerator: "CommandOrControl+Alt+C"
      }
    });
  });

  it("连接提前断开时清除旧稳定计时器", async () => {
    vi.useFakeTimers();
    const harness = createFakeSocketHarness();
    const service = createControlSettingsSyncService({
      httpBaseUrl: "http://127.0.0.1:8765",
      socketFactory: harness.socketFactory,
      random: () => 0.5,
      logger: createLoggerMock()
    });

    try {
      await service.start();
      harness.sockets[0].handlers.onOpen?.();
      harness.sockets[0].handlers.onClose();

      expect(vi.getTimerCount()).toBe(1);
    } finally {
      service.stop();
      vi.useRealTimers();
    }
  });

  it("旧连接迟到回调不能污染设置或关闭替代连接", async () => {
    vi.useFakeTimers();
    const harness = createFakeSocketHarness();
    const listener = vi.fn();
    const service = createControlSettingsSyncService({
      httpBaseUrl: "http://127.0.0.1:8765",
      socketFactory: harness.socketFactory,
      reconnectDelayMs: 1000,
      random: () => 0.5,
      logger: createLoggerMock()
    });

    try {
      service.subscribe(listener);
      await service.start();
      emitSettingsChanged(harness.sockets[0], settingsPayload({ revision: 0 }));
      harness.sockets[0].handlers.onClose();
      await vi.advanceTimersByTimeAsync(1000);
      emitSettingsChanged(harness.sockets[1], settingsPayload({
        revision: 1,
        followupEnabled: false
      }));

      emitSettingsChanged(harness.sockets[0], settingsPayload({
        revision: 99,
        followupEnabled: true
      }));
      harness.sockets[0].handlers.onClose();

      expect(listener).toHaveBeenLastCalledWith(expect.objectContaining({ revision: 1 }));
      expect(harness.sockets[1].close).not.toHaveBeenCalled();
      expect(vi.getTimerCount()).toBe(0);
    } finally {
      service.stop();
      vi.useRealTimers();
    }
  });

  it("只有 revision 变化时也通知订阅者，让快捷键状态对齐最新版本", async () => {
    const harness = createFakeSocketHarness();
    const listener = vi.fn();
    const lateListener = vi.fn();
    const service = createControlSettingsSyncService({
      httpBaseUrl: "http://127.0.0.1:8765",
      socketFactory: harness.socketFactory,
      logger: createLoggerMock()
    });

    service.subscribe(listener);
    await service.start();
    emitSettingsChanged(harness.sockets[0], settingsPayload({ revision: 0 }));
    emitSettingsChanged(harness.sockets[0], settingsPayload({ revision: 1 }));
    service.subscribe(lateListener);

    expect(listener).toHaveBeenCalledTimes(2);
    expect(listener).toHaveBeenLastCalledWith(expect.objectContaining({ revision: 1 }));
    expect(lateListener).toHaveBeenCalledWith({
      revision: 1,
      followupShortcut: {
        enabled: true,
        accelerator: "CommandOrControl+Alt+Space"
      },
      avatarVisibilityShortcut: {
        enabled: true,
        accelerator: "CommandOrControl+Alt+P"
      },
      selectionCaptureShortcut: {
        enabled: true,
        accelerator: "CommandOrControl+Alt+C"
      }
    });
  });

  it("忽略迟到的低 revision 快照，不回退快捷键设置", async () => {
    const harness = createFakeSocketHarness();
    const logger = createLoggerMock();
    const listener = vi.fn();
    const service = createControlSettingsSyncService({
      httpBaseUrl: "http://127.0.0.1:8765",
      socketFactory: harness.socketFactory,
      logger
    });

    service.subscribe(listener);
    await service.start();
    emitSettingsChanged(harness.sockets[0], settingsPayload({
      revision: 4,
      followupAccelerator: "CommandOrControl+Alt+F"
    }));
    emitSettingsChanged(harness.sockets[0], settingsPayload({
      revision: 3,
      followupAccelerator: "CommandOrControl+Alt+X"
    }));

    expect(listener).toHaveBeenCalledTimes(1);
    expect(listener).toHaveBeenLastCalledWith(expect.objectContaining({
      revision: 4,
      followupShortcut: expect.objectContaining({ accelerator: "CommandOrControl+Alt+F" })
    }));
    expect(logger.warn).not.toHaveBeenCalled();
  });

  it("选中内容捕获快捷键变化时会通知订阅者", async () => {
    const harness = createFakeSocketHarness();
    const listener = vi.fn();
    const service = createControlSettingsSyncService({
      httpBaseUrl: "http://127.0.0.1:8765",
      socketFactory: harness.socketFactory,
      logger: createLoggerMock()
    });

    service.subscribe(listener);
    await service.start();
    emitSettingsChanged(harness.sockets[0], settingsPayload({ revision: 0 }));
    emitSettingsChanged(
      harness.sockets[0],
      settingsPayload({
        revision: 1,
        selectionCaptureEnabled: false,
        selectionCaptureAccelerator: "CommandOrControl+Alt+X"
      })
    );

    expect(listener).toHaveBeenCalledTimes(2);
    expect(listener).toHaveBeenLastCalledWith(expect.objectContaining({
      revision: 1,
      selectionCaptureShortcut: {
        enabled: false,
        accelerator: "CommandOrControl+Alt+X"
      }
    }));
  });

  it("静默忽略非 settings.changed 事件", async () => {
    const harness = createFakeSocketHarness();
    const logger = createLoggerMock();
    const listener = vi.fn();
    const service = createControlSettingsSyncService({
      httpBaseUrl: "http://127.0.0.1:8765",
      socketFactory: harness.socketFactory,
      logger
    });

    service.subscribe(listener);
    await service.start();
    harness.sockets[0].handlers.onMessage(JSON.stringify({
      type: "turn.state_changed",
      payload: { turn_state: "idle" }
    }));

    expect(listener).not.toHaveBeenCalled();
    expect(logger.warn).not.toHaveBeenCalled();
  });

  it("忽略非法 JSON 并记录 warn", async () => {
    const harness = createFakeSocketHarness();
    const logger = createLoggerMock();
    const listener = vi.fn();
    const service = createControlSettingsSyncService({
      httpBaseUrl: "http://127.0.0.1:8765",
      socketFactory: harness.socketFactory,
      logger
    });

    service.subscribe(listener);
    await service.start();
    harness.sockets[0].handlers.onMessage("{bad json");

    expect(listener).not.toHaveBeenCalled();
    expect(logger.warn).toHaveBeenCalledWith("[companion-main] control settings event ignored", "invalid_json");
  });

  it("忽略非法 settings.changed payload 并记录 warn", async () => {
    const harness = createFakeSocketHarness();
    const logger = createLoggerMock();
    const listener = vi.fn();
    const service = createControlSettingsSyncService({
      httpBaseUrl: "http://127.0.0.1:8765",
      socketFactory: harness.socketFactory,
      logger
    });

    service.subscribe(listener);
    await service.start();
    harness.sockets[0].handlers.onMessage(
      JSON.stringify({
        type: "settings.changed",
        payload: {
          settings_revision: 0,
          followup_toggle_shortcut_enabled: true
        }
      })
    );

    expect(listener).not.toHaveBeenCalled();
    expect(logger.warn).toHaveBeenCalledWith(
      "[companion-main] control settings event ignored",
      "invalid_settings_payload",
      expect.any(Error)
    );
  });

  it("拒绝超出 JavaScript 安全整数范围的设置版本", async () => {
    const harness = createFakeSocketHarness();
    const logger = createLoggerMock();
    const listener = vi.fn();
    const service = createControlSettingsSyncService({
      httpBaseUrl: "http://127.0.0.1:8765",
      socketFactory: harness.socketFactory,
      logger
    });

    service.subscribe(listener);
    await service.start();
    emitSettingsChanged(
      harness.sockets[0],
      settingsPayload({ revision: Number.MAX_SAFE_INTEGER + 1 })
    );

    expect(listener).not.toHaveBeenCalled();
    expect(logger.warn).toHaveBeenCalledWith(
      "[companion-main] control settings event ignored",
      "invalid_settings_payload",
      expect.any(Error)
    );
  });
});
