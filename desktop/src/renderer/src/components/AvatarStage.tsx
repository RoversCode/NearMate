import { useEffect, useRef, useState } from "react";
import type { AvatarStageLive2dModel, AvatarStageProps, StageRenderState } from "./avatarStageTypes";
import { useLive2dStageModel } from "./useLive2dStageModel";
import { usePixiAvatarStage } from "./usePixiAvatarStage";

export function AvatarStage({
  modelPath,
  assistantStateLabel,
  controlStatusLabel,
  mediaStatusLabel,
  onAvatarTap,
  onPetBoundsChange,
  onActiveLive2dModelChanged,
  onModelReady,
  onModelLoadFailed,
  mode = "panel",
  presentationProfile,
  diagnosticsBridge
}: AvatarStageProps) {
  const hostRef = useRef<HTMLDivElement | null>(null);
  const onPetBoundsChangeRef = useRef(onPetBoundsChange);
  const onActiveLive2dModelChangedRef = useRef(onActiveLive2dModelChanged);
  const onModelReadyRef = useRef(onModelReady);
  const onModelLoadFailedRef = useRef(onModelLoadFailed);
  const activeModelRef = useRef<AvatarStageLive2dModel | null>(null);
  const activeModelPathRef = useRef<string | null>(null);
  const [activeModelLayoutVersion, setActiveModelLayoutVersion] = useState(0);

  /**
   * renderState 与 renderHint 只属于展示层，不进入全局状态机。
   */
  const [renderState, setRenderState] = useState<StageRenderState>("fallback");
  const [renderHint, setRenderHint] = useState(
    "当前没有挂接真实 Live2D 资源，所以这里先展示一个可交互的占位舞台。"
  );

  useEffect(() => {
    /**
     * 父组件未来如果传入内联回调，也不应该触发 PIXI 舞台重建。
     * 这里仅保存最新回调引用，让模型加载 effect 专注响应 mode 与 modelPath 的真实资源变化。
     */
    onPetBoundsChangeRef.current = onPetBoundsChange;
  }, [onPetBoundsChange]);

  useEffect(() => {
    onActiveLive2dModelChangedRef.current = onActiveLive2dModelChanged;
  }, [onActiveLive2dModelChanged]);

  useEffect(() => {
    onModelReadyRef.current = onModelReady;
  }, [onModelReady]);

  useEffect(() => {
    onModelLoadFailedRef.current = onModelLoadFailed;
  }, [onModelLoadFailed]);

  const { pixiAppRef, pixiReadyVersion } = usePixiAvatarStage({
    activeModelRef,
    activeModelPathRef,
    diagnosticsBridge,
    hostRef,
    onActiveLive2dModelChangedRef,
    onPetBoundsChangeRef,
    setActiveModelLayoutVersion,
    setRenderHint,
    setRenderState
  });

  useLive2dStageModel({
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
  });

  if (mode === "pet") {
    return (
      <section className="avatar-stage avatar-stage-pet" aria-label="桌宠角色舞台">
        <div className="avatar-stage-content-pet">
          <div ref={hostRef} className="avatar-canvas avatar-canvas-pet" />

          {renderState !== "ready" ? (
            <div className="avatar-stage-fallback avatar-stage-fallback-pet">
              <strong>
                {renderState === "loading"
                  ? "正在加载桌宠"
                  : renderState === "failed"
                    ? "桌宠载入失败"
                    : "桌宠资源未接入"}
              </strong>
              <p>{renderHint}</p>
            </div>
          ) : null}
        </div>
      </section>
    );
  }

  return (
    <section className="avatar-stage">
      <button className="avatar-stage-button" type="button" onClick={onAvatarTap}>
        <div className="stage-meta">
          <div>
            <h1 className="stage-title">桌宠头像壳</h1>
            <p className="stage-subtitle">
              当前窗口只负责承载桌宠形象与最小交互。模型、媒体与控制链路都可以分别演进，
              不再假设这里是承载全部功能的大主界面。
            </p>
          </div>
          <div className="stage-status-list">
            <span className="status-chip">陪伴状态 · {assistantStateLabel}</span>
            <span className="status-chip">控制层 · {controlStatusLabel}</span>
            <span className="status-chip">媒体层 · {mediaStatusLabel}</span>
          </div>
        </div>

        <div className="avatar-canvas-shell">
          <div ref={hostRef} className="avatar-canvas" />

          {renderState !== "ready" ? (
            <div className="avatar-stage-fallback">
              <strong>
                {renderState === "loading"
                  ? "正在加载舞台资源"
                  : renderState === "failed"
                    ? "暂时没能载入 Live2D"
                    : "当前展示头像占位舞台"}
              </strong>
              <p>{renderHint}</p>
              <div className="fallback-pills">
                <span className="fallback-pill">点击舞台可触发头像交互入口</span>
                <span className="fallback-pill">未来可替换真实 moc3 / model3.json 资源</span>
                <span className="fallback-pill">当前保留媒体层与控制层接入边界</span>
              </div>
            </div>
          ) : null}
        </div>
      </button>
    </section>
  );
}
