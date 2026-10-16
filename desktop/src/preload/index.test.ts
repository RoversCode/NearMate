import { afterAll, describe, expect, it, vi } from "vitest";

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

afterAll(() => {
  process.argv = originalProcessArgv;
});

describe("sandbox preload entry", () => {
  it("只安装 bootstrap 指定窗口的 bridge", async () => {
    process.argv = [...originalProcessArgv, panelBootstrapArgument];
    const exposeInMainWorld = vi.fn();

    vi.doMock("electron", () => ({
      contextBridge: {
        exposeInMainWorld
      },
      ipcRenderer: {
        invoke: vi.fn(),
        on: vi.fn(),
        removeListener: vi.fn(),
        send: vi.fn()
      },
      webUtils: {
        getPathForFile: vi.fn()
      }
    }));

    await import("./index");

    expect(exposeInMainWorld).toHaveBeenCalledOnce();
    expect(exposeInMainWorld).toHaveBeenCalledWith("companionPanel", expect.any(Object));
  });
});
