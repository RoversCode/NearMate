import { useMemo } from "react";
import { useBootstrapStatus } from "../bootstrap/useBootstrapStatus";
import { BootstrapBlockedView } from "../components/BootstrapBlockedView";
import { getPanelBridge } from "./panelBridge";
import { ModelPanelContainer } from "./ModelPanelContainer";

/**
 * BootstrapPanelBridge 描述 panel bootstrap gate 直接消费的最小 preload 契约。
 * 核心输入来自 window.companionPanel，预期输出是读取 HTTP 基址并可选触发 console 打开；边界异常是浏览器预览或旧 preload 缺失该能力时保持按钮隐藏。
 */
type BootstrapPanelBridge = {
  /**
   * openConsole 用于未初始化阻断态打开本地 console。
   * 核心输入是白名单设置页目标，预期输出是通过 preload 请求主进程打开浏览器；浏览器预览缺失 bridge 时按钮隐藏。
  */
  openConsole?(target: { section: "settings" }): void;
  runtime: {
    controlGateway: {
      httpBaseUrl: string;
    };
  };
};

/**
 * PanelApp 是 panel 窗口的 bootstrap gate。
 * 核心输入来自 window.companionPanel 的 HTTP 基址；预期输出是在 bootstrap ready 前展示阻断页，ready 后才挂载真实面板；边界异常是桥接缺失或后端不可用时不启动 runtime。
 */
export function PanelApp() {
  /**
   * preload 在浏览器预览或测试环境里可能不存在。
   * 业务意图是先读取最小 bootstrap 探测地址，避免 usePanelRuntime 提前创建 socket 或拉取设置。
   */
  const panelBridge = window.companionPanel as BootstrapPanelBridge | undefined;
  const bootstrapStatus = useBootstrapStatus(panelBridge?.runtime.controlGateway.httpBaseUrl);

  /**
   * bootstrap 未 ready 前必须硬阻断正常面板。
   * 业务意图是所有 runtime 副作用都只能发生在后端初始化完成之后。
   */
  if (bootstrapStatus.status !== "ready") {
    return (
      <BootstrapBlockedView
        reason={bootstrapStatus.reason ?? "backend_unavailable"}
        windowLabel="控制面板"
        onRetry={bootstrapStatus.retry}
        onOpenConsole={
          panelBridge?.openConsole
            ? () => panelBridge.openConsole?.({ section: "settings" })
            : undefined
        }
      />
    );
  }

  return <PanelReadyApp />;
}

/**
 * PanelReadyApp 渲染独立 panel 窗口的真实界面。
 * 核心输入由 usePanelRuntime 从 preload bridge 与控制链路读取；预期输出是人物选择和窗口级动作；边界异常由 runtime hook 内部降级处理。
 */
function PanelReadyApp() {
  const panelBridge = useMemo(() => getPanelBridge(), []);
  /**
   * 当前 v1 面板只承载动作栏点开的单一视图。
   * 业务意图是避免右侧 panel 重新变成聊天、任务、人物和 console 混杂的大壳。
   */
  return (
    <main className="panel-shell" data-window="panel">
      <div className="panel-stack">
        <header className="panel-header">
          <strong>人物</strong>
          <button type="button" aria-label="关闭面板" onClick={() => panelBridge?.closePanel()}>
            ×
          </button>
        </header>
        <ModelPanelContainer />
      </div>
    </main>
  );
}
