import { useEffect, type MutableRefObject, type RefObject } from "react";
import type { AvatarPresentationProfile } from "../../../shared/avatarPresentationProfile";
import { reportRendererDiagnostic } from "../lib/rendererDiagnostics";
import { reportAvatarStagePetBounds } from "./avatarStageLayout";
import type {
  AvatarStageLive2dModel,
  AvatarStageProps,
  PixiAvatarStageApp
} from "./avatarStageTypes";
import {
  commitLive2dStageModel,
  destroyPendingLive2dStageModel,
  removeCommittedPreviousLive2dStageModel
} from "./live2dStageModelCommit";
import { loadLive2dStageModel } from "./live2dStageModelLoader";
import { useLive2dStageModelLayout } from "./useLive2dStageModelLayout";

type UseLive2dStageModelInput = {
  activeModelLayoutVersion: number;
  activeModelPathRef: MutableRefObject<string | null>;
  activeModelRef: MutableRefObject<AvatarStageLive2dModel | null>;
  diagnosticsBridge: AvatarStageProps["diagnosticsBridge"];
  hostRef: RefObject<HTMLDivElement | null>;
  mode: "pet" | "panel";
  modelPath: string | null;
  onActiveLive2dModelChangedRef: MutableRefObject<AvatarStageProps["onActiveLive2dModelChanged"]>;
  onModelLoadFailedRef: MutableRefObject<AvatarStageProps["onModelLoadFailed"]>;
  onModelReadyRef: MutableRefObject<AvatarStageProps["onModelReady"]>;
  onPetBoundsChangeRef: MutableRefObject<AvatarStageProps["onPetBoundsChange"]>;
  pixiAppRef: RefObject<PixiAvatarStageApp | null>;
  pixiReadyVersion: number;
  presentationProfile?: AvatarPresentationProfile;
  setActiveModelLayoutVersion: React.Dispatch<React.SetStateAction<number>>;
  setRenderHint: React.Dispatch<React.SetStateAction<string>>;
  setRenderState: React.Dispatch<React.SetStateAction<"fallback" | "loading" | "ready" | "failed">>;
};

/**
 * useLive2dStageModel 管理 Live2D 模型加载、提交、失败回退与重布局。
 *
 * Args:
 *   input: Pixi 舞台、模型路径、展示档案和父层回调引用。
 */
export function useLive2dStageModel({
  activeModelLayoutVersion,
  activeModelPathRef,
  activeModelRef,
  diagnosticsBridge,
  hostRef,
  mode,
  modelPath,
  onActiveLive2dModelChangedRef,
  onModelLoadFailedRef,
  onModelReadyRef,
  onPetBoundsChangeRef,
  pixiAppRef,
  pixiReadyVersion,
  presentationProfile,
  setActiveModelLayoutVersion,
  setRenderHint,
  setRenderState
}: UseLive2dStageModelInput): void {
  useEffect(() => {
    let cancelled = false;
    let pendingLoadedModel: AvatarStageLive2dModel | null = null;
    const hostElement = hostRef.current;
    const app = pixiAppRef.current;

    /**
     * 没有舞台上下文时，先保持当前展示状态。
     * 等 Pixi 初始化完成后，本 effect 会因为 pixiReadyVersion 更新而再次进入真正的模型加载分支。
     */
    if (!hostElement || !app) {
      return;
    }

    /**
     * destroyPendingLoadedModel 专门负责清理“这一次 effect 刚加载出来、但还没真正提交到舞台”的临时模型。
     * 之所以单独收敛，是为了让 cleanup、取消、veto 和 reject 这些收口路径共用同一份销毁语义，避免漏清理或重复销毁。
     */
    const destroyPendingLoadedModel = () => {
      pendingLoadedModel = destroyPendingLive2dStageModel(
        pendingLoadedModel,
        activeModelRef.current
      );
    };

    /**
     * 没有模型路径时直接停留在 fallback。
     * 这样仓库尚未接入资源文件也不会报错或白屏。
     */
    if (!modelPath) {
      setRenderState("fallback");
      setRenderHint("当前没有挂接真实 Live2D 资源，所以这里先展示一个可交互的占位舞台。");
      /**
       * 没有真实模型时无法得出可信的可见模型边界。
       * 主应用收到 null 后会移除透明拖拽层，避免把占位内容误当成模型本体。
       */
      onPetBoundsChangeRef.current?.(null);
      return;
    }

    /**
     * 如果舞台上已经保留着同一路径的已显示模型，这里直接短路。
     * 这样“新模型切换失败后回退到旧路径”不会把旧实例再加载一遍，现有重布局 effect 仍会负责同步最新档案与尺寸。
     */
    if (
      activeModelRef.current &&
      activeModelPathRef.current === modelPath
    ) {
      setRenderState("ready");
      setRenderHint("已加载 Live2D 模型占位接口。后续替换真实资源时无需改动 UI 结构。");
      return;
    }

    /**
     * 只有真正提供了模型路径，才尝试加载 Live2D 模型。
     * 新模型 ready 并通过父层确认前，旧模型会继续保留在舞台上，避免切换失败时出现桌宠瞬间消失。
     */
    void (async () => {
      try {
        setRenderState("loading");
        setRenderHint("正在尝试加载 Live2D 模型资源。");
        reportRendererDiagnostic(diagnosticsBridge, "info", "live2d.model_load_started", {
          mode,
          modelPath,
          hostRect: hostElement.getBoundingClientRect().toJSON()
        });

        const nextModel = await loadLive2dStageModel({
          app,
          modelPath
        });
        pendingLoadedModel = nextModel;

        if (cancelled) {
          destroyPendingLoadedModel();
          return;
        }

        /**
         * 头像窗与面板内预览舞台使用不同的模型构图：
         * 桌宠模式尽量突出“只有角色”，面板模式则保留较完整的舞台观感。
         */
        nextModel.anchor.set(0.5, 1);

        const nextLayout = reportAvatarStagePetBounds({
          app,
          mode,
          model: nextModel,
          presentationProfile
        });

        nextModel.scale.set(nextLayout.scale);
        nextModel.position.set(nextLayout.x, nextLayout.y);

        /**
         * 新模型资源 ready 之后，先把“是否允许提交”交给父层决定。
         * 这样像“模型文件已加载成功，但设置持久化失败”的场景，就能显式 veto，本地继续保留旧人物。
         */
        const commitDecision = await onModelReadyRef.current?.(modelPath);

        if (cancelled) {
          destroyPendingLoadedModel();
          return;
        }

        /**
         * 父层返回 false 表示本次 ready 不允许提交。
         * 这里直接销毁临时新模型并退出，旧模型仍然留在舞台上，不做任何替换。
         */
        if (commitDecision === false) {
          destroyPendingLoadedModel();

          if (activeModelRef.current) {
            setRenderState("ready");
            setRenderHint("已加载 Live2D 模型占位接口。后续替换真实资源时无需改动 UI 结构。");
          } else {
            setRenderState("fallback");
            setRenderHint("当前没有挂接真实 Live2D 资源，所以这里先展示一个可交互的占位舞台。");
            onPetBoundsChangeRef.current?.(null);
          }

          return;
        }

        /**
         * 只有父层确认这次切换可以提交后，才真正把新模型挂到舞台上并销毁旧模型。
         * 这样“模型已 ready 但持久化失败”的场景仍然会保持旧角色继续可见。
         */
        const previousModel = commitLive2dStageModel({
          activeModelPathRef,
          activeModelRef,
          app,
          modelPath,
          nextModel,
          onActiveLive2dModelChanged: onActiveLive2dModelChangedRef.current,
          setActiveModelLayoutVersion
        });
        pendingLoadedModel = null;

        reportAvatarStagePetBounds({
          app,
          mode,
          model: nextModel,
          presentationProfile,
          onPetBoundsChange: onPetBoundsChangeRef.current ?? undefined
        });

        removeCommittedPreviousLive2dStageModel(app, previousModel);

        reportRendererDiagnostic(diagnosticsBridge, "info", "live2d.model_loaded", {
          internalWidth: nextModel.internalModel.width,
          internalHeight: nextModel.internalModel.height,
          originalWidth: nextModel.internalModel.originalWidth,
          originalHeight: nextModel.internalModel.originalHeight,
          layout: nextLayout,
          textureCount: nextModel.textures.length
        });

        setRenderState("ready");
        setRenderHint("已加载 Live2D 模型占位接口。后续替换真实资源时无需改动 UI 结构。");
      } catch (error) {
        /**
         * onModelReady 可能在父层持久化阶段直接抛错或 reject。
         * 这时新模型还没真正提交到舞台，必须在进入统一失败分支前主动销毁，避免留下未挂载的临时实例。
         */
        destroyPendingLoadedModel();

        if (cancelled) {
          return;
        }

        reportRendererDiagnostic(diagnosticsBridge, "error", "live2d.model_load_failed", error);

        /**
         * 如果舞台上已经有旧模型，这次失败只需要保留旧模型继续展示；
         * 只有首个模型都没能载入成功时，才退回 failed 占位态并清空拖拽边界。
         */
        if (!activeModelRef.current) {
          onPetBoundsChangeRef.current?.(null);
          setRenderState("failed");
          setRenderHint("找到了模型路径，但当前没有成功完成 Live2D 载入。桌面壳仍保留占位舞台供联调。");
        } else {
          setRenderState("ready");
          setRenderHint("已加载 Live2D 模型占位接口。后续替换真实资源时无需改动 UI 结构。");
        }

        onModelLoadFailedRef.current?.(modelPath, error);
      }
    })();

    return () => {
      cancelled = true;
      destroyPendingLoadedModel();
    };
  }, [diagnosticsBridge, mode, modelPath, pixiReadyVersion]);

  useLive2dStageModelLayout({
    activeModelLayoutVersion,
    activeModelRef,
    hostRef,
    mode,
    onPetBoundsChangeRef,
    pixiAppRef,
    pixiReadyVersion,
    presentationProfile
  });
}
