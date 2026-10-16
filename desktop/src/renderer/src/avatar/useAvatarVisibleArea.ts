import { useEffect, useState } from "react";
import type { AvatarVisibleArea } from "../../../shared/live2dModelTypes";
import type { CompanionAvatarBridge } from "./avatarBridge";

type AvatarVisibleAreaBridge = Pick<
  CompanionAvatarBridge,
  "onVisibleAreaChanged" | "requestVisibleArea"
>;

/**
 * useAvatarVisibleArea 订阅主进程推送的头像窗口可见区。
 *
 * Args:
 *   avatarBridge: preload 暴露的可见区订阅与主动拉取能力。
 *
 * Returns:
 *   当前窗口内仍可见的区域；缺少 bridge 能力时返回 null。
 */
export function useAvatarVisibleArea(
  avatarBridge?: AvatarVisibleAreaBridge
): AvatarVisibleArea | null {
  const [visibleArea, setVisibleArea] = useState<AvatarVisibleArea | null>(null);

  useEffect(() => {
    /**
     * 订阅主进程推送的窗口可见区，并在挂载时主动拉取一次初始值。
     * 业务意图：renderer 不知道窗口在物理屏幕上的位置，必须由主进程告知「窗口内仍可见的底边」，
     * 脚部气泡才能在下半身被任务栏/屏幕下边缘切掉时贴到可见身体最底部。
     * 边界异常：preload 缺失这两个能力时（旧 preload 或浏览器预览）直接跳过，气泡回退到按窗口高度兜底。
     */
    if (!avatarBridge?.onVisibleAreaChanged) {
      return;
    }

    let active = true;
    const unsubscribe = avatarBridge.onVisibleAreaChanged(nextVisibleArea => {
      setVisibleArea(nextVisibleArea);
    });

    void avatarBridge.requestVisibleArea?.().then(initialVisibleArea => {
      /**
       * pull 结果迟到时如果组件已卸载就丢弃，避免对已卸载组件 setState。
       * 同时 pull 只用于补齐初始值，若期间已经收到 push，则不让较旧的初始值覆盖更新。
       */
      if (active && initialVisibleArea) {
        setVisibleArea(current => current ?? initialVisibleArea);
      }
    });

    return () => {
      active = false;
      unsubscribe();
    };
  }, [avatarBridge]);

  return visibleArea;
}
