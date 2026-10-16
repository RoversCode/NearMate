import type { Application } from "pixi.js";
import type { AvatarPresentationProfile } from "../../../shared/avatarPresentationProfile";
import type { Live2dLipSyncModelBinding } from "../lib/live2dLipSync";
import type { RendererDiagnosticBridge } from "../lib/rendererDiagnostics";
import type { AvatarDragRegion } from "../avatar/avatarDragRegion";
import type { loadCubism5Live2dEngine } from "../lib/live2dRuntime";

/**
 * 舞台组件只负责“展示形象与状态”，不负责决定业务逻辑。
 * 真实 Live2D 模型存在时尝试加载；不存在时稳定显示占位舞台。
 */
export type AvatarStageProps = {
  modelPath: string | null;
  assistantStateLabel: string;
  controlStatusLabel: string;
  mediaStatusLabel: string;
  onAvatarTap(): void;
  onPetBoundsChange?(bounds: AvatarDragRegion | null): void;
  onActiveLive2dModelChanged?(binding: Live2dLipSyncModelBinding | null): void;
  onModelReady?(modelPath: string): boolean | void | Promise<boolean | void>;
  onModelLoadFailed?(modelPath: string, error: unknown): void;
  mode?: "pet" | "panel";
  presentationProfile?: AvatarPresentationProfile;
  diagnosticsBridge?: RendererDiagnosticBridge;
};

/**
 * 渲染态单独抽出来，便于未来把“加载中 / 已就绪 / 失败”映射到不同动画表现。
 */
export type StageRenderState = "fallback" | "loading" | "ready" | "failed";

export type AvatarStageLayoutApp = {
  renderer: {
    width: number;
    height: number;
  };
  screen: {
    width: number;
    height: number;
  };
};

export type AvatarStageLayoutModel = {
  internalModel: {
    width: number;
    height: number;
  };
};

export type AvatarStageLoadedModel = AvatarStageLayoutModel & {
  scale: {
    set(value: number): void;
  };
  position: {
    set(x: number, y: number): void;
  };
};

export type AvatarStageLive2dModel =
  Awaited<ReturnType<Awaited<ReturnType<typeof loadCubism5Live2dEngine>>["Live2DModel"]["from"]>>;

export type PixiAvatarStageApp = Application;
