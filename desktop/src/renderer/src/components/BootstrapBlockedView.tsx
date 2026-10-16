import type { BootstrapBlockedReason } from "../bootstrap/useBootstrapStatus";

export type BootstrapBlockedViewProps = {
  /**
   * reason 表示当前 bootstrap 被阻断的业务原因。
   * 输入由 useBootstrapStatus 收窄，输出用于选择阻断文案；边界异常是未知 reason 不应传入该组件。
   */
  reason: BootstrapBlockedReason;
  /**
   * windowLabel 是当前窗口展示名。
   * 输入为调用方提供的短文案，输出显示在阻断页眉；边界异常是空字符串只会影响可读性，不改变业务状态。
   */
  windowLabel: string;
  /**
   * onRetry 负责重新触发 bootstrap 探测。
   * 核心输入为空，预期输出由调用方刷新状态；边界异常是回调抛错时由 React 事件系统向上暴露。
   */
  onRetry(): void;
  /**
   * onOpenConsole 负责请求桌面壳打开本地 console。
   * 核心输入为空，预期输出是触发 preload bridge；边界异常是缺失该回调时不展示按钮。
   */
  onOpenConsole?(): void;
};

const BOOTSTRAP_BLOCKED_COPY: Record<
  BootstrapBlockedReason,
  { title: string; description: string }
> = {
  uninitialized: {
    title: "当前还不能进入桌宠主流程",
    description: "请先完成 必需的 LLM 职责配置和当前角色语音的试听确认。桌宠会在确认初始化完成后继续启动。"
  },
  backend_unavailable: {
    title: "本地服务当前不可用",
    description: "桌宠暂时无法连接本地后端服务。请确认服务已启动，再点击重试。"
  }
};

/**
 * BootstrapBlockedView 展示桌面窗口在 bootstrap 未就绪时的硬阻断界面。
 * 核心入参是阻断原因、窗口标签与重试回调；预期输出是可读的阻断说明和重试按钮；边界异常是调用方必须传入已收窄的 reason。
 */
export function BootstrapBlockedView({
  reason,
  windowLabel,
  onRetry,
  onOpenConsole
}: BootstrapBlockedViewProps) {
  const copy = BOOTSTRAP_BLOCKED_COPY[reason];
  /**
   * 只有未初始化才需要把用户引导到 console 完成向导。
   * 后端不可用时展示该按钮没有业务价值，因为本地服务不可达时 console 也大概率无法打开。
   */
  const shouldShowOpenConsole = reason === "uninitialized" && !!onOpenConsole;

  return (
    <main className="panel-shell" data-window="bootstrap-blocked">
      <section className="bootstrap-blocked-panel" role="status" aria-live="polite">
        <div className="bootstrap-blocked-header">
          <div>
            <p className="bootstrap-blocked-eyebrow">{windowLabel}</p>
            <h1>{copy.title}</h1>
          </div>
        </div>
        <div className="bootstrap-blocked-card">
          <span className="bootstrap-blocked-pill">Bootstrap blocked</span>
          <p className="bootstrap-blocked-summary">{copy.description}</p>
          {shouldShowOpenConsole ? (
            <button className="bootstrap-blocked-action" type="button" onClick={onOpenConsole}>
              打开 console
            </button>
          ) : null}
          <button className="bootstrap-blocked-action" type="button" onClick={onRetry}>
            重试
          </button>
        </div>
      </section>
    </main>
  );
}
