import { afterAll, beforeEach, describe, expect, it, vi } from "vitest";

const originalProcessArgv = [...process.argv];
const panelBootstrapArgument = `--companion-window-bootstrap=${encodeURIComponent(JSON.stringify({
  kind: "panel",
  runtime: {
    controlGateway: {
      httpBaseUrl: "http://127.0.0.1:8765",
      websocketUrl: "ws://127.0.0.1:8765/ws/control"
    },
    live2d: {
      catalog: []
    }
  }
}))}`;

type ExposedBridge = {
  runtime: {
    controlGateway: {
      httpBaseUrl: string;
    };
  };
  openConsole(target: { section: "settings" }): void;
  requestModelSwitch(modelId: string, requestId?: string): void;
};

describe("panel preload bridge", () => {
  beforeEach(() => {
    vi.resetModules();
    vi.restoreAllMocks();
    process.argv = [...originalProcessArgv, panelBootstrapArgument];
  });

  afterAll(() => {
    process.argv = originalProcessArgv;
  });

  it("openConsole 会发出打开本地 console 的桌面 channel", async () => {
    const send = vi.fn();
    let exposedBridge: ExposedBridge | null = null;

    vi.doMock("electron", () => ({
      contextBridge: {
        exposeInMainWorld: vi.fn((_key: string, bridge: ExposedBridge) => {
          exposedBridge = bridge;
        })
      },
      ipcRenderer: {
        send,
        invoke: vi.fn(),
        on: vi.fn(),
        removeListener: vi.fn()
      }
    }));

    await import("./panel");

    exposedBridge!.openConsole({ section: "settings" });

    expect(send).toHaveBeenCalledWith("desktop:open-console", {
      section: "settings"
    });
    exposedBridge!.requestModelSwitch("Natori", "a51c4163-5fab-49d6-a369-3e428ad14b22");
    expect(send).toHaveBeenCalledWith("panel:request-model-switch", {
      modelId: "Natori", requestId: "a51c4163-5fab-49d6-a369-3e428ad14b22"
    });
  });

  it("只向 renderer 暴露 main 进程准备好的本机控制地址", async () => {
    const send = vi.fn();
    let exposedBridge: ExposedBridge | null = null;

    vi.doMock("electron", () => ({
      contextBridge: {
        exposeInMainWorld: vi.fn((_key: string, bridge: ExposedBridge) => {
          exposedBridge = bridge;
        })
      },
      ipcRenderer: {
        send,
        invoke: vi.fn(),
        on: vi.fn(),
        removeListener: vi.fn()
      }
    }));

    await import("./panel");

    expect(exposedBridge!.runtime.controlGateway.httpBaseUrl).toBe("http://127.0.0.1:8765");
  });

});
