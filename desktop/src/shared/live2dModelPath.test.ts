import { describe, expect, it } from "vitest";
import {
  DEFAULT_LIVE2D_MODEL_PATH,
  isSelectedLive2dModelAvailable,
  resolveLive2dModelPath,
  resolveSelectedLive2dModelPath
} from "./live2dModelPath";

describe("live2dModelPath", () => {
  it("未提供环境变量时，回退到内置 Q 版大肥鱼模型", () => {
    expect(resolveLive2dModelPath(undefined)).toBe(DEFAULT_LIVE2D_MODEL_PATH);
    expect(DEFAULT_LIVE2D_MODEL_PATH).toBe("./live2d/resources/DeepSeekChibi/c_0120.model3.json");
    expect(resolveLive2dModelPath("")).toBe(DEFAULT_LIVE2D_MODEL_PATH);
    expect(resolveLive2dModelPath("   ")).toBe(DEFAULT_LIVE2D_MODEL_PATH);
  });

  it("提供环境变量时，优先使用显式配置的模型路径", () => {
    expect(resolveLive2dModelPath("./custom/model3.json")).toBe("./custom/model3.json");
    expect(resolveLive2dModelPath(" ./override/model3.json ")).toBe(
      "./override/model3.json"
    );
  });

  it("提供 selected_live2d_model_id 且 catalog 命中时，返回对应模型路径", () => {
    expect(
      resolveSelectedLive2dModelPath({
        catalog: [
          {
            id: "Haru",
            label: "Haru",
            modelPath: "/live2d/resources/Haru/Haru.model3.json"
          },
          {
            id: "Natori",
            label: "Natori",
            modelPath: "/live2d/resources/Natori/Natori.model3.json"
          }
        ],
        selectedModelId: "Natori",
        fallbackModelPath: DEFAULT_LIVE2D_MODEL_PATH
      })
    ).toBe("/live2d/resources/Natori/Natori.model3.json");
  });

  it("默认形象更新后，仍尊重用户已经选中的 Haru", () => {
    const haruPath = "./live2d/resources/Haru/Haru.model3.json";

    expect(resolveSelectedLive2dModelPath({
      catalog: [
        { id: "DeepSeekChibi", label: "大肥鱼（Q版）", modelPath: DEFAULT_LIVE2D_MODEL_PATH },
        { id: "Haru", label: "Haru", modelPath: haruPath }
      ],
      selectedModelId: "Haru",
      fallbackModelPath: DEFAULT_LIVE2D_MODEL_PATH
    })).toBe(haruPath);
  });

  it("selected_live2d_model_id 缺失或不命中时，稳定回退到默认模型路径", () => {
    const catalog = [
      {
        id: "Haru",
        label: "Haru",
        modelPath: "/live2d/resources/Haru/Haru.model3.json"
      }
    ];

    expect(
      resolveSelectedLive2dModelPath({
        catalog,
        selectedModelId: undefined,
        fallbackModelPath: DEFAULT_LIVE2D_MODEL_PATH
      })
    ).toBe(DEFAULT_LIVE2D_MODEL_PATH);
    expect(
      resolveSelectedLive2dModelPath({
        catalog,
        selectedModelId: "Unknown",
        fallbackModelPath: DEFAULT_LIVE2D_MODEL_PATH
      })
    ).toBe(DEFAULT_LIVE2D_MODEL_PATH);
  });

  it("可以显式判断当前记住的人物目录名是否仍然存在于可用 catalog", () => {
    const catalog = [
      {
        id: "Haru",
        label: "Haru",
        modelPath: "/live2d/resources/Haru/Haru.model3.json"
      }
    ];

    expect(
      isSelectedLive2dModelAvailable({
        catalog,
        selectedModelId: "Haru"
      })
    ).toBe(true);
    expect(
      isSelectedLive2dModelAvailable({
        catalog,
        selectedModelId: "Ren"
      })
    ).toBe(false);
    expect(
      isSelectedLive2dModelAvailable({
        catalog,
        selectedModelId: "   "
      })
    ).toBe(false);
  });
});
