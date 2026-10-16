import { beforeEach, describe, expect, it, vi } from "vitest";
import { loadLive2dStageModel } from "./live2dStageModelLoader";

const assertLive2dModelSupportedMock = vi.fn();
const loadCubism5Live2dEngineMock = vi.fn();

vi.mock("../lib/live2dRuntime", () => ({
  assertLive2dModelSupported: (...args: unknown[]) =>
    assertLive2dModelSupportedMock(...args),
  loadCubism5Live2dEngine: (...args: unknown[]) =>
    loadCubism5Live2dEngineMock(...args)
}));

describe("live2dStageModelLoader", () => {
  beforeEach(() => {
    assertLive2dModelSupportedMock.mockReset();
    loadCubism5Live2dEngineMock.mockReset();
  });

  it("会先做兼容性预检，再用 Pixi ticker 创建 Live2D 模型", async () => {
    const model = { id: "live2d-model" };
    const fromMock = vi.fn().mockResolvedValue(model);
    const ticker = { id: "ticker" };

    assertLive2dModelSupportedMock.mockResolvedValue(undefined);
    loadCubism5Live2dEngineMock.mockResolvedValue({
      Live2DModel: {
        from: fromMock
      }
    });

    await expect(loadLive2dStageModel({
      app: { ticker } as unknown as Parameters<typeof loadLive2dStageModel>[0]["app"],
      modelPath: "/live2d/Hiyori.model3.json"
    })).resolves.toBe(model);

    expect(assertLive2dModelSupportedMock).toHaveBeenCalledWith("/live2d/Hiyori.model3.json");
    expect(fromMock).toHaveBeenCalledWith("/live2d/Hiyori.model3.json", {
      ticker,
      textureOptions: { lod: "single-auto" }
    });
  });

  it("兼容性预检失败时不会继续初始化 Cubism 引擎", async () => {
    assertLive2dModelSupportedMock.mockRejectedValue(new Error("unsupported moc"));

    await expect(loadLive2dStageModel({
      app: { ticker: {} } as unknown as Parameters<typeof loadLive2dStageModel>[0]["app"],
      modelPath: "/live2d/Ren.model3.json"
    })).rejects.toThrow("unsupported moc");

    expect(loadCubism5Live2dEngineMock).not.toHaveBeenCalled();
  });
});
