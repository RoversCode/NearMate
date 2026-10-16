import type { BrowserWindow } from "electron";

/**
 * AvatarWindowPresentationMode 描述桌宠窗口在系统桌面上的展示策略。
 * passive 用于默认展示，interactive 用于无面板时的人物交互；
 * panel-adjacent-interactive 用于面板打开期间的人物交互，避免头像透明矩形盖住侧边板。
 * summoned 用于用户显式召回时置顶展示，hidden 用于用户显式收起桌宠。
 */
export type AvatarWindowPresentationMode =
  | "passive"
  | "interactive"
  | "panel-adjacent-interactive"
  | "summoned"
  | "hidden";

/**
 * AvatarWindowPresentationPort 是展示模式投影所需的最小 BrowserWindow 能力。
 */
type AvatarWindowPresentationPort = Pick<
  BrowserWindow,
  "setAlwaysOnTop" | "setIgnoreMouseEvents" | "show" | "hide"
>;

/**
 * applyAvatarWindowPresentationMode 将桌宠展示模式投影到 Electron 原生窗口。
 * @param window 需要更新展示模式的角色窗。
 * @param mode 目标展示模式。
 * @returns 无返回值，所有结果通过窗口副作用体现。
 */
export function applyAvatarWindowPresentationMode(
  window: AvatarWindowPresentationPort,
  mode: AvatarWindowPresentationMode
): void {
  if (mode === "summoned") {
    /**
     * 召回态表达用户显式要看到桌宠，因此保持置顶；
     * 鼠标仍保持穿透，让小热区 hover 再单独接管交互。
     */
    window.show();
    window.setAlwaysOnTop(true, "floating");
    window.setIgnoreMouseEvents(true, { forward: true });
    return;
  }

  if (mode === "hidden") {
    /**
     * 收起桌宠前先恢复穿透并取消置顶，避免隐藏过程中短暂拦截其它窗口点击。
     */
    window.setIgnoreMouseEvents(true, { forward: true });
    window.setAlwaysOnTop(false);
    window.hide();
    return;
  }

  if (mode === "interactive") {
    /**
     * 无面板交互态先临时提升层级，再收回穿透，确保人物本体、加号和缩放 handle 可以点击。
     */
    window.setAlwaysOnTop(true, "floating");
    window.setIgnoreMouseEvents(false, { forward: true });
    return;
  }

  if (mode === "panel-adjacent-interactive") {
    /**
     * 面板打开时，角色窗只允许在自身可见人物区域接收操作，
     * 不再置顶到侧边板上方，避免透明窗口矩形吃掉侧边板按钮点击。
     */
    window.setAlwaysOnTop(false);
    window.setIgnoreMouseEvents(false, { forward: true });
    return;
  }

  /**
   * 被动态先恢复鼠标穿透，再取消置顶，避免透明窗口矩形在降级过程中短暂拦截桌面图标。
   */
  window.setIgnoreMouseEvents(true, { forward: true });
  window.setAlwaysOnTop(false);
}
