import type { CSSProperties } from "react";

export type AvatarMaterialFeedback = {
  kind: "progress" | "success" | "warning" | "error";
  text: string;
} | null;

export type AvatarMaterialFeedbackToastProps = {
  feedback: AvatarMaterialFeedback;
  style?: CSSProperties;
};

/**
 * AvatarMaterialFeedbackToast 渲染头像窗口的材料交付反馈。
 *
 * @param props.feedback 最近一次材料交付结果。
 * @param props.style 可选定位样式；传入后跟随人物可见身体边界，而不是固定在窗口底部。
 * @returns 材料反馈短提示；无状态时不渲染。
 */
export function AvatarMaterialFeedbackToast({ feedback, style }: AvatarMaterialFeedbackToastProps) {
  if (!feedback) {
    return null;
  }

  const className = [
    "avatar-material-feedback-toast",
    `avatar-material-feedback-toast-${feedback.kind}`,
    style ? "avatar-material-feedback-toast-positioned" : null
  ].filter(Boolean).join(" ");

  return (
    <div className={className} role="status" style={style}>
      {feedback.text}
    </div>
  );
}
