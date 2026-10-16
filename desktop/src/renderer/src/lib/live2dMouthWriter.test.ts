// @vitest-environment jsdom

import { describe, expect, it } from "vitest";

import type { Live2dLipSyncModel } from "./live2dLipSync";
import {
  readLive2dLipSyncParameterIds,
  writeLive2dMouthOpenValue
} from "./live2dMouthWriter";
import { createLegacyModel, createModel } from "./live2dLipSync.testUtils";

describe("Live2D lip sync model helpers", () => {
  it("优先读取模型 motionManager 中的 LipSync 参数", () => {
    const { model } = createModel(["ParamA", "ParamB"]);

    expect(readLive2dLipSyncParameterIds(model)).toEqual(["ParamA", "ParamB"]);
  });

  it("模型缺少 LipSync 参数时回退到常见嘴部参数", () => {
    const { model } = createModel([]);

    expect(readLive2dLipSyncParameterIds(model)).toEqual([
      "ParamMouthOpenY",
      "PARAM_MOUTH_OPEN_Y"
    ]);
  });

  it("Cubism 3/4/5 模型优先直接设定嘴部参数，避免外部音轨口型被动作基准稀释", () => {
    const {
      addParameterValueById,
      getIdSafe,
      model,
      setParameterValueById
    } = createModel(["ParamA"]);

    const result = writeLive2dMouthOpenValue(model, 0.7);

    expect(result).toEqual({
      written: true,
      writerKind: "cubism"
    });
    expect(getIdSafe).toHaveBeenCalledWith("ParamA");
    expect(setParameterValueById).toHaveBeenCalledWith("safe:ParamA", 0.7, 1);
    expect(addParameterValueById).not.toHaveBeenCalled();
  });

  it("Cubism 2 legacy 模型优先直接设定嘴部参数", () => {
    const {
      addToParamFloat,
      getParamIndex,
      model,
      setParamFloat
    } = createLegacyModel(["PARAM_MOUTH_OPEN_Y"]);

    const result = writeLive2dMouthOpenValue(model, 0.7);

    expect(result).toEqual({
      written: true,
      writerKind: "legacy"
    });
    expect(getParamIndex).toHaveBeenCalledWith("PARAM_MOUTH_OPEN_Y");
    expect(setParamFloat).toHaveBeenCalledWith("PARAM_MOUTH_OPEN_Y".length, 0.7, 1);
    expect(addToParamFloat).not.toHaveBeenCalled();
  });

  it("Cubism 3/4/5 写入时保留 coreModel 方法的 this 上下文", () => {
    const coreModel = {
      parameterValues: new Map<string, number>(),
      getParameterValueById(id: string) {
        return this.parameterValues.get(id) ?? 0;
      },
      setParameterValueById(id: string, value: number) {
        this.parameterValues.set(id, value);
      }
    };
    const model: Live2dLipSyncModel = {
      internalModel: {
        coreModel,
        getIdSafe: (id: string) => `safe:${id}`,
        motionManager: {
          lipSyncIds: ["ParamA"]
        }
      }
    };

    expect(() => writeLive2dMouthOpenValue(model, 0.6)).not.toThrow();
    expect(coreModel.parameterValues.get("safe:ParamA")).toBe(0.6);
  });

  it("Cubism 2 legacy 写入时保留 coreModel 方法的 this 上下文", () => {
    const coreModel = {
      parameterValues: new Map<number, number>(),
      getParamFloat(index: number) {
        return this.parameterValues.get(index) ?? 0;
      },
      getParamIndex(id: string) {
        return id.length;
      },
      setParamFloat(index: number, value: number) {
        this.parameterValues.set(index, value);
      }
    };
    const model: Live2dLipSyncModel = {
      internalModel: {
        coreModel,
        motionManager: {
          lipSyncIds: ["PARAM_MOUTH_OPEN_Y"]
        }
      }
    };

    expect(() => writeLive2dMouthOpenValue(model, 0.6)).not.toThrow();
    expect(coreModel.parameterValues.get("PARAM_MOUTH_OPEN_Y".length)).toBe(0.6);
  });

  it("模型缺少可用写入 API 时返回失败结果", () => {
    const model: Live2dLipSyncModel = {
      internalModel: {
        coreModel: {},
        motionManager: {
          lipSyncIds: ["ParamMouthOpenY"]
        }
      }
    };

    expect(writeLive2dMouthOpenValue(model, 0.7)).toEqual({
      reason: "missing_mouth_parameter_writer",
      written: false,
      writerKind: "none"
    });
  });
});
