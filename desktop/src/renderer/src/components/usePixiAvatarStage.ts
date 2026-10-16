import { useEffect, useRef, useState, type MutableRefObject, type RefObject } from "react";
import { Application } from "pixi.js";
import { reportRendererDiagnostic } from "../lib/rendererDiagnostics";
import type { AvatarStageLive2dModel, PixiAvatarStageApp } from "./avatarStageTypes";
import type { AvatarStageProps } from "./avatarStageTypes";

type UsePixiAvatarStageInput = {
  activeModelRef: MutableRefObject<AvatarStageLive2dModel | null>;
  activeModelPathRef: MutableRefObject<string | null>;
  diagnosticsBridge: AvatarStageProps["diagnosticsBridge"];
  hostRef: RefObject<HTMLDivElement | null>;
  onActiveLive2dModelChangedRef: MutableRefObject<AvatarStageProps["onActiveLive2dModelChanged"]>;
  onPetBoundsChangeRef: MutableRefObject<AvatarStageProps["onPetBoundsChange"]>;
  setActiveModelLayoutVersion: React.Dispatch<React.SetStateAction<number>>;
  setRenderHint: React.Dispatch<React.SetStateAction<string>>;
  setRenderState: React.Dispatch<React.SetStateAction<"fallback" | "loading" | "ready" | "failed">>;
};

/**
 * usePixiAvatarStage 管理 Pixi Application 的创建、挂载和销毁。
 *
 * Args:
 *   input: 舞台宿主元素、模型引用、诊断桥和清理回调引用。
 *
 * Returns:
 *   Pixi app 引用与初始化版本号。
 */
export function usePixiAvatarStage({
  activeModelRef,
  activeModelPathRef,
  diagnosticsBridge,
  hostRef,
  onActiveLive2dModelChangedRef,
  onPetBoundsChangeRef,
  setActiveModelLayoutVersion,
  setRenderHint,
  setRenderState
}: UsePixiAvatarStageInput): {
  pixiAppRef: RefObject<PixiAvatarStageApp | null>;
  pixiReadyVersion: number;
} {
  const pixiAppRef = useRef<PixiAvatarStageApp | null>(null);
  const [pixiReadyVersion, setPixiReadyVersion] = useState(0);

  useEffect(() => {
    let cancelled = false;
    const hostElement = hostRef.current;

    /**
     * Pixi 舞台本身在组件生命周期内只初始化一次。
     * 后续模型切换只替换舞台中的 Live2D 实例，不销毁整个渲染上下文，避免切换过程中闪白或清空旧模型。
     */
    if (!hostElement) {
      return;
    }

    void (async () => {
      try {
        const app = new Application();

        await app.init({
          backgroundAlpha: 0,
          resizeTo: hostElement,
          antialias: true,
          preference: "webgl",
          autoDensity: true,
          resolution: window.devicePixelRatio
        });

        if (cancelled) {
          app.destroy(true, true);
          return;
        }

        pixiAppRef.current = app;
        hostElement.appendChild(app.canvas);
        reportRendererDiagnostic(diagnosticsBridge, "info", "live2d.pixi_stage_initialized", {
          rendererWidth: app.renderer.width,
          rendererHeight: app.renderer.height,
          screenWidth: app.screen.width,
          screenHeight: app.screen.height,
          canvasWidth: app.canvas.width,
          canvasHeight: app.canvas.height,
          canvasStyleWidth: app.canvas.style.width,
          canvasStyleHeight: app.canvas.style.height
        });
        setPixiReadyVersion(version => version + 1);
      } catch (error) {
        if (cancelled) {
          return;
        }
        reportRendererDiagnostic(diagnosticsBridge, "error", "live2d.model_load_failed", error);
        setRenderState("failed");
        setRenderHint("桌宠舞台初始化失败，当前仍保留占位壳层供联调。");
      }
    })();

    return () => {
      cancelled = true;
      onPetBoundsChangeRef.current?.(null);
      onActiveLive2dModelChangedRef.current?.(null);
      activeModelRef.current?.destroy();
      activeModelRef.current = null;
      activeModelPathRef.current = null;
      setActiveModelLayoutVersion(0);
      pixiAppRef.current?.destroy(true, true);
      pixiAppRef.current = null;
    };
  }, []);

  return {
    pixiAppRef,
    pixiReadyVersion
  };
}
