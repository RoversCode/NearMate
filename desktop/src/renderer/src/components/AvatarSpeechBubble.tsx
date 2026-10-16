import type { CSSProperties } from "react";

type AvatarSpeechBubbleProps = {
  text: string;
  style?: CSSProperties;
  onClick?(): void;
};

/**
 * AvatarSpeechBubble 渲染普通语音或通知气泡。
 * 核心入参是文本、可选定位与点击动作；普通通知输出轻量提示块，待答请求输出可点击按钮。
 * 边界异常：未提供 style 时回退到样式表里的窗口底边默认锚点；没有点击动作时保持鼠标穿透。
 */
export function AvatarSpeechBubble({ text, style, onClick }: AvatarSpeechBubbleProps) {
  if (onClick) {
    return (
      <button
        type="button"
        className="avatar-speech-bubble"
        data-avatar-interactive="true"
        aria-label={text}
        style={style}
        onClick={onClick}
      >
        {text}
      </button>
    );
  }

  return (
    <div className="avatar-speech-bubble" role="status" aria-live="polite" style={style}>
      {text}
    </div>
  );
}
