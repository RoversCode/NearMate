import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  assertLive2dModelSupported,
  UnsupportedLive2dModelVersionError,
  waitForCubismCoreReady
} from "./live2dRuntime";

describe("waitForCubismCoreReady", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it("会等待 Cubism Core 真正返回版本号，而不是只等 script load", async () => {
    vi.useFakeTimers();
    const globalTarget: {
      Live2DCubismCore?: {
        Version?: {
          csmGetVersion?: () => number;
        };
      };
    } = {};

    const readyTask = waitForCubismCoreReady({
      timeoutMs: 1000,
      intervalMs: 10,
      globalTarget,
      schedule: setTimeout
    });

    /**
     * 模拟 Emscripten runtime 比 script.onload 晚一点完成初始化。
     * 业务上必须等 csmGetVersion 可调用后，才允许导入 Live2D 引擎入口。
     */
    setTimeout(() => {
      globalTarget.Live2DCubismCore = {
        Version: {
          csmGetVersion: () => 0x05030000
        }
      };
    }, 30);

    await vi.advanceTimersByTimeAsync(40);

    await expect(readyTask).resolves.toBeUndefined();
  });

  it("Cubism Core 超时不可用时给出明确错误", async () => {
    vi.useFakeTimers();

    const readyTask = waitForCubismCoreReady({
      timeoutMs: 30,
      intervalMs: 10,
      globalTarget: {},
      schedule: setTimeout
    });
    const expectedRejection = expect(readyTask).rejects.toThrowError(
      "Cubism Core runtime 初始化超时"
    );

    await vi.advanceTimersByTimeAsync(40);

    await expectedRejection;
  });
});

describe("assertLive2dModelSupported", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("moc 版本超出当前 bundled core 支持上限时，会抛出明确错误", async () => {
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          FileReferences: {
            Moc: "Ren.moc3"
          }
        })
      } as Response)
      .mockResolvedValueOnce({
        ok: true,
        arrayBuffer: async () => {
          const mocHeader = new Uint8Array(8);
          mocHeader.set([0x4d, 0x4f, 0x43, 0x33, 0x06, 0x00, 0x00, 0x00]);
          return mocHeader.buffer;
        }
      } as Response);

    vi.stubGlobal("fetch", fetchMock);

    await expect(
      assertLive2dModelSupported("/live2d/resources/Ren/Ren.model3.json", {
        assetBaseUrl: "http://127.0.0.1:5173/avatar.html"
      })
    ).rejects.toBeInstanceOf(UnsupportedLive2dModelVersionError);
  });

  it("moc 版本在支持范围内时，不会阻断后续模型加载", async () => {
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          FileReferences: {
            Moc: "Haru.moc3"
          }
        })
      } as Response)
      .mockResolvedValueOnce({
        ok: true,
        arrayBuffer: async () => {
          const mocHeader = new Uint8Array(8);
          mocHeader.set([0x4d, 0x4f, 0x43, 0x33, 0x05, 0x00, 0x00, 0x00]);
          return mocHeader.buffer;
        }
      } as Response);

    vi.stubGlobal("fetch", fetchMock);

    await expect(
      assertLive2dModelSupported("/live2d/resources/Haru/Haru.model3.json", {
        assetBaseUrl: "http://127.0.0.1:5173/avatar.html"
      })
    ).resolves.toBeUndefined();
  });
});
