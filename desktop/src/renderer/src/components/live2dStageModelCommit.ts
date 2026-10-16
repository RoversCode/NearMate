import type { Dispatch, MutableRefObject, SetStateAction } from "react";
import type { Live2dLipSyncModelBinding } from "../lib/live2dLipSync";
import type { AvatarStageLive2dModel, PixiAvatarStageApp } from "./avatarStageTypes";

type CommitLive2dStageModelInput = {
  activeModelPathRef: MutableRefObject<string | null>;
  activeModelRef: MutableRefObject<AvatarStageLive2dModel | null>;
  app: PixiAvatarStageApp;
  modelPath: string;
  nextModel: AvatarStageLive2dModel;
  onActiveLive2dModelChanged?(binding: Live2dLipSyncModelBinding | null): void;
  setActiveModelLayoutVersion: Dispatch<SetStateAction<number>>;
};

/**
 * destroyPendingLive2dStageModel 清理尚未提交到舞台的新模型。
 *
 * Args:
 *   pendingModel: 当前加载事务创建出的临时模型。
 *   activeModel: 舞台上已经提交的活跃模型。
 *
 * Returns:
 *   始终返回 null，方便调用方同步清空 pending 引用。
 */
export function destroyPendingLive2dStageModel(
  pendingModel: AvatarStageLive2dModel | null,
  activeModel: AvatarStageLive2dModel | null
): null {
  /**
   * pendingModel 可能已经被提交为 activeModel。
   * 这种情况下销毁会误杀正在显示的人物，因此只清空 pending 引用，不调用 destroy。
   */
  if (!pendingModel || activeModel === pendingModel) {
    return null;
  }

  pendingModel.destroy();
  return null;
}

/**
 * commitLive2dStageModel 把父层确认后的新模型提交到 Pixi stage。
 *
 * Args:
 *   input: Pixi app、新模型、当前活跃模型引用、模型路径引用和唇形绑定回调。
 *
 * Returns:
 *   被新模型替换下来的旧模型；调用方可在完成边界上报后再移除销毁。
 */
export function commitLive2dStageModel({
  activeModelPathRef,
  activeModelRef,
  app,
  modelPath,
  nextModel,
  onActiveLive2dModelChanged,
  setActiveModelLayoutVersion
}: CommitLive2dStageModelInput): AvatarStageLive2dModel | null {
  const previousModel = activeModelRef.current;

  if (previousModel) {
    onActiveLive2dModelChanged?.(null);
  }

  app.stage.addChild(nextModel);
  activeModelRef.current = nextModel;
  activeModelPathRef.current = modelPath;
  setActiveModelLayoutVersion(version => version + 1);
  onActiveLive2dModelChanged?.({
    model: nextModel,
    ticker: app.ticker
  });

  return previousModel;
}

/**
 * removeCommittedPreviousLive2dStageModel 移除已被替换的旧模型。
 *
 * Args:
 *   app: 当前 Pixi app。
 *   previousModel: 已经被新模型替换下来的旧模型。
 */
export function removeCommittedPreviousLive2dStageModel(
  app: PixiAvatarStageApp,
  previousModel: AvatarStageLive2dModel | null
): void {
  if (!previousModel) {
    return;
  }

  app.stage.removeChild(previousModel);
  previousModel.destroy();
}
