import { useEffect, type MutableRefObject, type RefObject } from "react";
import type { AvatarPresentationProfile } from "../../../shared/avatarPresentationProfile";
import { syncLoadedAvatarStageModelLayout } from "./avatarStageLayout";
import type {
  AvatarStageLive2dModel,
  AvatarStageProps,
  PixiAvatarStageApp
} from "./avatarStageTypes";

type UseLive2dStageModelLayoutInput = {
  activeModelLayoutVersion: number;
  activeModelRef: MutableRefObject<AvatarStageLive2dModel | null>;
  hostRef: RefObject<HTMLDivElement | null>;
  mode: "pet" | "panel";
  onPetBoundsChangeRef: MutableRefObject<AvatarStageProps["onPetBoundsChange"]>;
  pixiAppRef: RefObject<PixiAvatarStageApp | null>;
  pixiReadyVersion: number;
  presentationProfile?: AvatarPresentationProfile;
};

/**
 * useLive2dStageModelLayout 订阅已提交 Live2D 模型的尺寸重布局。
 *
 * Args:
 *   input: 已提交模型引用、Pixi app 引用、宿主元素和展示档案。
 */
export function useLive2dStageModelLayout({
  activeModelLayoutVersion,
  activeModelRef,
  hostRef,
  mode,
  onPetBoundsChangeRef,
  pixiAppRef,
  pixiReadyVersion,
  presentationProfile
}: UseLive2dStageModelLayoutInput): void {
  useEffect(() => {
    const hostElement = hostRef.current;

    /**
     * 只有在 Pixi 舞台与当前模型都已经存在时，窗口尺寸变化才有“重布局”意义。
     * 否则直接跳过，避免在初始化阶段注册无效监听。
     */
    if (
      !hostElement ||
      !pixiAppRef.current ||
      !activeModelRef.current ||
      activeModelLayoutVersion === 0
    ) {
      return;
    }

    /**
     * 这里把“已加载模型重布局”收敛成独立函数，供首次挂载后的各种尺寸变化场景复用。
     * 这样 Electron 窗口真实 resize 不需要依赖重新加载模型文件，也不会和模型切换链路耦在一起。
     */
    const syncActiveModelLayout = () => {
      const app = pixiAppRef.current;
      const model = activeModelRef.current;

      if (!app || !model) {
        return;
      }

      syncLoadedAvatarStageModelLayout({
        app,
        mode,
        model,
        presentationProfile,
        onPetBoundsChange: onPetBoundsChangeRef.current ?? undefined
      });
    };

    const resizeObserver = new ResizeObserver(() => {
      syncActiveModelLayout();
    });

    resizeObserver.observe(hostElement);
    window.addEventListener("resize", syncActiveModelLayout);

    /**
     * 档案切换或 effect 重挂时，先主动同步一次当前模型布局。
     * 这样即使 ResizeObserver 还没收到下一帧尺寸变化，也能保证当前舞台立即收敛到最新档案与窗口尺寸。
     */
    syncActiveModelLayout();

    return () => {
      resizeObserver.disconnect();
      window.removeEventListener("resize", syncActiveModelLayout);
    };
  }, [activeModelLayoutVersion, mode, pixiReadyVersion, presentationProfile]);
}
