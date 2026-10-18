import { useLayoutEffect, useRef, type CSSProperties } from "react";

export type AvatarBootstrapBubbleMode = "loading" | "uninitialized" | "backend_unavailable";

type AvatarBootstrapBubbleProps = {
  mode: AvatarBootstrapBubbleMode;
  message?: string;
  onOpenConsole?(): void;
  onRetry?(): void;
  onHeightChange(height: number): void;
  style?: CSSProperties;
};

const AVATAR_BOOTSTRAP_COPY: Record<
  AvatarBootstrapBubbleMode,
  { title: string; description: string }
> = {
  loading: {
    title: "正在检查初始化状态",
    description: "桌宠资源会先显示出来，但唤醒、聊天和工具能力会等配置确认后再启动。"
  },
  uninitialized: {
    title: "需要先完成初始化",
    description: "请先打开 console 配置必需的 LLM 职责，并完成当前角色语音的试听确认。初始化完成前，桌宠不会响应唤醒、聊天或工具调用。"
  },
  backend_unavailable: {
    title: "本地服务当前不可用",
    description: "请确认 backend core 已经启动。服务恢复前，桌宠只保留展示和引导，不启动任何能力。"
  }
};

/**
 * 显示初始化说明和可执行按钮，并把实际高度交给桌宠壳用于避开窗口边缘。
 *
 * Args:
 *   mode: 当前初始化状态，用来选择说明和操作。
 *   message: Backend 确认的运行时进度；没有进度时显示首次配置说明。
 *   onOpenConsole: 打开配置页面；未提供时不显示失效按钮。
 *   onRetry: 重新检查本地服务。
 *   onHeightChange: 内容或宽度变化后接收包含按钮的完整高度。
 *   style: 桌宠壳根据脚部位置与可见区域计算的定位样式。
 * Returns:
 *   不自动消失的初始化引导气泡。
 */
export function AvatarBootstrapBubble({
  mode,
  message,
  onOpenConsole,
  onRetry,
  onHeightChange,
  style
}: AvatarBootstrapBubbleProps) {
  const copy = AVATAR_BOOTSTRAP_COPY[mode];
  const bubbleRef = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    const bubble = bubbleRef.current;
    if (!bubble) return;
    // 说明换行和按钮都会增加高度，不能再按固定行数定位，否则按钮会掉到窗口外。
    const measureHeight = () => {
      const height = Math.ceil(bubble.getBoundingClientRect().height);
      if (height > 0) onHeightChange(height);
    };
    measureHeight();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measureHeight);
    observer.observe(bubble);
    return () => observer.disconnect();
  }, [mode, message, onHeightChange]);
  /**
   * 未初始化时必须把用户引导到 console；后端不可用时 console 不一定可达，因此只给重试。
   * loading 态没有立即可执行动作，保持纯状态提示，避免用户误以为需要手动处理。
   */
  const shouldShowOpenConsole = mode === "uninitialized" && !!onOpenConsole;
  const shouldShowRetry = mode === "backend_unavailable" && !!onRetry;

  return (
    <div
      ref={bubbleRef}
      className="avatar-bootstrap-bubble"
      data-avatar-interactive="true"
      role="dialog"
      aria-label="桌宠初始化提示"
      style={style}
    >
      <div>
        <strong>{message ? "运行时初始化" : copy.title}</strong>
        <p>{message || copy.description}</p>
      </div>
      {shouldShowOpenConsole || shouldShowRetry ? (
        <div className="avatar-bootstrap-actions">
          {shouldShowOpenConsole ? (
            <button className="avatar-bootstrap-action" type="button" onClick={onOpenConsole}>
              打开 console
            </button>
          ) : null}
          {shouldShowRetry ? (
            <button className="avatar-bootstrap-action" type="button" onClick={onRetry}>
              重试
            </button>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
