import { describe, expect, it, vi } from "vitest";
import type { Dispatch, SetStateAction } from "react";
import {
  commitLive2dStageModel,
  destroyPendingLive2dStageModel,
  removeCommittedPreviousLive2dStageModel
} from "./live2dStageModelCommit";
import type { AvatarStageLive2dModel, PixiAvatarStageApp } from "./avatarStageTypes";

function createModel(): AvatarStageLive2dModel {
  return {
    destroy: vi.fn()
  } as unknown as AvatarStageLive2dModel;
}

describe("live2dStageModelCommit", () => {
  it("销毁未提交的临时模型，但不会误销毁已经成为 active 的模型", () => {
    const pendingModel = createModel();
    const activeModel = createModel();

    expect(destroyPendingLive2dStageModel(pendingModel, activeModel)).toBeNull();
    expect(pendingModel.destroy).toHaveBeenCalledTimes(1);

    expect(destroyPendingLive2dStageModel(activeModel, activeModel)).toBeNull();
    expect(activeModel.destroy).not.toHaveBeenCalled();
  });

  it("提交新模型时会清空旧绑定、挂载新模型并更新路径引用", () => {
    const previousModel = createModel();
    const nextModel = createModel();
    const activeModelRef = { current: previousModel };
    const activeModelPathRef = { current: "/live2d/old.model3.json" };
    const addChild = vi.fn();
    const onActiveLive2dModelChanged = vi.fn();
    const setActiveModelLayoutVersion: Dispatch<SetStateAction<number>> = vi.fn(
      (nextValue: SetStateAction<number>) => {
        if (typeof nextValue === "function") {
          nextValue(1);
        }
      }
    );
    const app = {
      stage: {
        addChild,
        removeChild: vi.fn()
      },
      ticker: { id: "ticker" }
    } as unknown as PixiAvatarStageApp;

    expect(commitLive2dStageModel({
      activeModelPathRef,
      activeModelRef,
      app,
      modelPath: "/live2d/new.model3.json",
      nextModel,
      onActiveLive2dModelChanged,
      setActiveModelLayoutVersion
    })).toBe(previousModel);

    expect(onActiveLive2dModelChanged).toHaveBeenNthCalledWith(1, null);
    expect(addChild).toHaveBeenCalledWith(nextModel);
    expect(activeModelRef.current).toBe(nextModel);
    expect(activeModelPathRef.current).toBe("/live2d/new.model3.json");
    expect(onActiveLive2dModelChanged).toHaveBeenNthCalledWith(2, {
      model: nextModel,
      ticker: app.ticker
    });
  });

  it("移除旧模型时会同时从 stage 移除并销毁资源", () => {
    const previousModel = createModel();
    const app = {
      stage: {
        removeChild: vi.fn()
      }
    } as unknown as PixiAvatarStageApp;

    removeCommittedPreviousLive2dStageModel(app, previousModel);

    expect(app.stage.removeChild).toHaveBeenCalledWith(previousModel);
    expect(previousModel.destroy).toHaveBeenCalledTimes(1);
  });
});
