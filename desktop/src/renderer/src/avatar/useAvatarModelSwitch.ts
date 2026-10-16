import { useEffect, useRef } from "react";
import type { MutableRefObject } from "react";
import { isSelectedLive2dModelAvailable } from "../../../shared/live2dModelPath";
import type { CompanionAvatarBridge } from "./avatarBridge";
import type { AvatarRuntime } from "./useAvatarRuntime";

type UseAvatarModelSwitchInput = {
  appliedPresentationProfileModelIdRef: MutableRefObject<string | null>;
  avatarBridge?: CompanionAvatarBridge;
  avatarRuntime: AvatarRuntime;
  committedPresentationModelId: string | null;
  committedSelectedModelId: string | undefined;
  modelCatalog: CompanionAvatarBridge["runtime"]["live2d"]["catalog"];
  pendingModelId: string | null;
  rememberedModelUnavailable: boolean;
  resolvedModelPath: string;
  setPendingModelId(modelId: string | null): void;
  targetModelId: string | undefined | null;
};

/**
 * useAvatarModelSwitch 管理头像人物切换请求、pending 状态和 ready/load 结果收口。
 *
 * Args:
 *   input: 当前模型选择上下文、运行时设置和 preload bridge。
 *
 * Returns:
 *   交给 AvatarStage 的 ready/failed 回调；状态回执保留已接纳请求的编号。
 */
export function useAvatarModelSwitch({
  appliedPresentationProfileModelIdRef,
  avatarBridge,
  avatarRuntime,
  committedPresentationModelId,
  committedSelectedModelId,
  modelCatalog,
  pendingModelId,
  rememberedModelUnavailable,
  resolvedModelPath,
  setPendingModelId,
  targetModelId
}: UseAvatarModelSwitchInput) {
  const pendingModelIdRef = useRef<string | null>(null);
  const pendingRequestIdRef = useRef<string | undefined>(undefined);
  const settingsRef = useRef(avatarRuntime.settings);
  const settingsHydratedRef = useRef(avatarRuntime.settingsHydrated);

  /**
   * 这三个 ref 充当模型切换监听里的同步真相源。
   * 同一拍内如果连续收到两条切换请求，监听回调还没等到 React 重渲染就会再次执行，
   * 这时必须读取这里的最新值，才能避免第二条请求继续看见旧闭包并穿透 guard。
   */
  pendingModelIdRef.current = pendingModelId;
  settingsRef.current = avatarRuntime.settings;
  settingsHydratedRef.current = avatarRuntime.settingsHydrated;

  useEffect(() => {
    /**
     * 如果上次记住的人物目录这次启动时已经失效，尽早把失败原因投影给 panel。
     * 这样用户一打开面板就能看到“为什么现在回退成默认人物”，而不是等到某次额外加载失败后才知道。
     */
    if (!rememberedModelUnavailable || !avatarRuntime.settings?.selected_live2d_model_id) {
      return;
    }

    avatarBridge?.reportModelSwitchState({
      status: "failed",
      modelId: avatarRuntime.settings.selected_live2d_model_id,
      reason: "上次选择的人物当前不可用，已回退到默认人物。"
    });
  }, [avatarBridge, avatarRuntime.settings, rememberedModelUnavailable]);

  useEffect(() => {
    /**
     * 当前人物发生变化后，需要允许下一次 ready 再次触发一次默认展示档案应用。
     * 这样首显与切人后的“恢复人物默认尺寸”都能落到主进程，而不会被上一次人物的去重状态误挡住。
     */
    if (appliedPresentationProfileModelIdRef.current === committedPresentationModelId) {
      return;
    }

    appliedPresentationProfileModelIdRef.current = null;
  }, [appliedPresentationProfileModelIdRef, committedPresentationModelId]);

  useEffect(() => {
    /**
     * 当前 committed 人物一旦在设置水合后已知，就尽早请求主进程应用它的默认展示档案。
     * 这样首显不需要等 Live2D 模型 fully ready，窗口可以更早拿到默认尺寸与位置，避免先闪出一轮小窗。
     */
    if (!avatarRuntime.settingsHydrated || !committedPresentationModelId) {
      return;
    }

    /**
     * 同一人物在当前运行期内只主动 apply 一次默认档案。
     * 这样既保留“切人成功后恢复新人物默认档案”，也不会把已经手调过尺寸的同一人物再次重置。
     */
    if (appliedPresentationProfileModelIdRef.current === committedPresentationModelId) {
      return;
    }

    avatarBridge?.applyPresentationProfile(committedPresentationModelId);
    appliedPresentationProfileModelIdRef.current = committedPresentationModelId;
  }, [
    appliedPresentationProfileModelIdRef,
    avatarBridge,
    avatarRuntime.settingsHydrated,
    committedPresentationModelId
  ]);

  useEffect(() => {
    const acceptedPendingModelId = pendingModelIdRef.current;

    /**
     * 模型切换成功后，不要立刻清掉 pending。
     * 必须等 committed settings 真的追上这次成功的人物 id，再统一收口；
     * 否则会出现“pending 先清空、settings 还没更新，渲染短暂回跳旧模型路径”的竞态。
     */
    if (!acceptedPendingModelId || committedSelectedModelId !== acceptedPendingModelId) {
      return;
    }

    pendingModelIdRef.current = null;
    pendingRequestIdRef.current = undefined;
    setPendingModelId(null);
  }, [committedSelectedModelId]);

  useEffect(() => {
    if (!avatarBridge) {
      return;
    }

    if (typeof avatarBridge.onModelSwitchRequested !== "function") {
      return;
    }

    return avatarBridge.onModelSwitchRequested(({ modelId, requestId }) => {
      /**
       * 拒绝的请求也必须回执，人物面板才能解除等待并告诉用户原因。
       * 目标已经生效时回报成功，让面板重新读取权威设置完成同步。
       */
      const latestSettings = settingsRef.current;
      const requestIdentity = requestId === undefined ? {} : { requestId };

      if (!settingsHydratedRef.current || !latestSettings) {
        avatarBridge.reportModelSwitchState({
          status: "failed", modelId, ...requestIdentity, reason: "人物设置正在加载，请稍后重试。"
        });
        return;
      }

      if (pendingModelIdRef.current) {
        avatarBridge.reportModelSwitchState({
          status: "failed", modelId, ...requestIdentity, reason: "另一个人物正在切换，请完成后重试。"
        });
        return;
      }

      if (modelId === latestSettings.selected_live2d_model_id) {
        avatarBridge.reportModelSwitchState({ status: "succeeded", modelId, ...requestIdentity });
        return;
      }

      if (!isSelectedLive2dModelAvailable({ catalog: modelCatalog, selectedModelId: modelId })) {
        avatarBridge.reportModelSwitchState({
          status: "failed", modelId, ...requestIdentity, reason: "该人物资源当前不可用，请选择其他人物。"
        });
        return;
      }

      /**
       * 先同步写入 ref，再触发 React state。
       * 这样就算第二条请求在同一拍里立刻跟进，也会立刻看到“已经有 pending”这一最新事实。
       */
      pendingModelIdRef.current = modelId;
      pendingRequestIdRef.current = requestId;
      setPendingModelId(modelId);
      avatarBridge.reportModelSwitchState({
        status: "switching",
        modelId,
        ...requestIdentity
      });
    });
  }, [
    avatarBridge,
    avatarRuntime.settings,
    avatarRuntime.settingsHydrated,
    modelCatalog,
    pendingModelId
  ]);

  return {
    async handleModelReady(loadedModelPath: string) {
      /**
       * 普通首次加载不会触发设置写回。
       * 只有存在 pendingModelId，且这次 ready 的确实是 pending 对应路径时，才进入“成功后持久化”链路。
       */
      if (!targetModelId || loadedModelPath !== resolvedModelPath) {
        return;
      }

      const acceptedPendingModelId = pendingModelIdRef.current;
      // 设置写回期间可能先收到 committed 快照，提前收口 pending；回执仍使用接纳时冻结的编号。
      const acceptedRequestId = pendingRequestIdRef.current;
      const requestIdentity = acceptedRequestId === undefined ? {} : { requestId: acceptedRequestId };

      if (!acceptedPendingModelId) {
        return true;
      }

      const latestSettings = settingsRef.current;

      if (!latestSettings) {
        return false;
      }

      try {
        await avatarRuntime.updateSettings({
          selected_live2d_model_id: acceptedPendingModelId
        });
        avatarBridge?.applyPresentationProfile(acceptedPendingModelId);
        appliedPresentationProfileModelIdRef.current = acceptedPendingModelId;
        avatarBridge?.reportModelSwitchState({
          status: "succeeded",
          modelId: acceptedPendingModelId,
          ...requestIdentity
        });
        return true;
      } catch {
        avatarBridge?.reportModelSwitchState({
          status: "failed",
          modelId: acceptedPendingModelId,
          ...requestIdentity,
          reason: "人物保存失败，请稍后重试。"
        });
        pendingModelIdRef.current = null;
        pendingRequestIdRef.current = undefined;
        setPendingModelId(null);
        return false;
      }
    },
    handleModelLoadFailed() {
      const acceptedPendingModelId = pendingModelIdRef.current;

      if (!acceptedPendingModelId) {
        return;
      }

      avatarBridge?.reportModelSwitchState({
        status: "failed",
        modelId: acceptedPendingModelId,
        ...(pendingRequestIdRef.current === undefined ? {} : { requestId: pendingRequestIdRef.current }),
        reason: "人物资源加载失败。"
      });
      pendingModelIdRef.current = null;
      pendingRequestIdRef.current = undefined;
      setPendingModelId(null);
    }
  };
}
