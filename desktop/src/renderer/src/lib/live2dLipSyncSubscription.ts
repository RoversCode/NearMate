import type {
  Live2dLipSyncInternalModel,
  Live2dLipSyncModelBinding,
  Live2dLipSyncUpdateSubscription
} from "./live2dLipSyncTypes";

/**
 * 按当前模型能力创建逐帧嘴型更新订阅。
 *
 * Args:
 *   modelBinding: 当前模型和 ticker。
 *   updateMouth: 每帧写入嘴型的回调。
 *
 * Returns:
 *   可用于后续解绑的订阅描述。
 */
export function attachLive2dLipSyncUpdateSubscription(
  modelBinding: Live2dLipSyncModelBinding,
  updateMouth: () => void
): Live2dLipSyncUpdateSubscription {
  const internalModel = modelBinding.model.internalModel;
  if (isModelEventTarget(internalModel)) {
    /**
     * 业务意图：嘴型参数要在 Live2D 已完成 motion、眨眼、呼吸之后，
     * 但还没执行最终 model.update 前写入，避免被保存成下一帧的参数基准。
     */
    internalModel.on("beforeModelUpdate", updateMouth);
    return {
      kind: "model_event",
      internalModel
    };
  }

  modelBinding.ticker.add(updateMouth);
  return {
    kind: "ticker",
    ticker: modelBinding.ticker
  };
}

/**
 * 解绑逐帧嘴型更新订阅。
 *
 * Args:
 *   subscription: 当前订阅描述；为空时安全返回。
 *   updateMouth: 注册订阅时使用的同一个回调。
 */
export function detachLive2dLipSyncUpdateSubscription(
  subscription: Live2dLipSyncUpdateSubscription | null,
  updateMouth: () => void
): void {
  if (!subscription) {
    return;
  }

  if (subscription.kind === "model_event") {
    const internalModel = subscription.internalModel;
    if (isModelEventTarget(internalModel)) {
      internalModel.off("beforeModelUpdate", updateMouth);
    }
    return;
  }

  subscription.ticker.remove(updateMouth);
}

function isModelEventTarget(
  internalModel: Live2dLipSyncInternalModel | undefined
): internalModel is Live2dLipSyncInternalModel & {
  off(eventName: "beforeModelUpdate", listener: () => void): void;
  on(eventName: "beforeModelUpdate", listener: () => void): void;
} {
  return typeof internalModel?.on === "function" && typeof internalModel.off === "function";
}
