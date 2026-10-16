import { contextBridge, ipcRenderer } from "electron";
import {
  RENDERER_DIAGNOSTIC_CHANNEL,
  type RendererDiagnosticLevel,
  type RendererDiagnosticPayload
} from "../shared/rendererDiagnostics";
import type { Live2dModelSwitchState } from "../shared/live2dModelTypes";
import type { ConsoleTarget } from "../shared/shellNavigation";
import { readWindowBootstrapArgument } from "../shared/windowBootstrap";

const bootstrap = readWindowBootstrapArgument(process.argv, "panel");

if (bootstrap) {
/**
 * panel 窗口 bridge 为控制面板壳层提供一组可审计的桌面能力。
 * 当前只保留后续 UI 启动必须用到的最小字段，避免过早复制旧主窗口全部接口。
 */
contextBridge.exposeInMainWorld("companionPanel", {
  /**
   * panel 自己的中文诊断通过显式 IPC 上报给主进程终端。
   * 这样可以绕开 console-message 在 Windows 开发终端上的编码问题。
   */
  reportDiagnostic(level: RendererDiagnosticLevel, message: string, details?: unknown): void {
    ipcRenderer.send(RENDERER_DIAGNOSTIC_CHANNEL, {
      level,
      message,
      details
    } satisfies RendererDiagnosticPayload);
  },
  /**
   * 面板关闭行为交给主进程决定，renderer 只表达用户意图。
   */
  closePanel(): void {
    ipcRenderer.send("panel:close");
  },
  /**
   * renderer 只能提交结构化 Console 目标，地址拼接与安全校验全部留在主进程。
   */
  openConsole(target: ConsoleTarget): void {
    ipcRenderer.send("desktop:open-console", target);
  },
  /**
   * 提交人物切换意图，并原样转发可选请求编号以区分重试。
   *
   * @param modelId 目标人物目录名。
   * @param requestId 当前点击的请求编号，旧调用方可省略。
   * @returns 不返回值；实际结果通过人物切换状态订阅回传。
   */
  requestModelSwitch(modelId: string, requestId?: string): void {
    ipcRenderer.send("panel:request-model-switch", { modelId, ...(requestId === undefined ? {} : { requestId }) });
  },
  /**
   * 请求主进程把当前人物恢复到默认窗口位置与尺寸。
   */
  resetAvatarLayout(): void {
    ipcRenderer.send("panel:reset-avatar-layout");
  },
  /**
   * panel 订阅 avatar 侧实际切换状态，用于后续更新 pending 状态与失败提示。
   */
  onModelSwitchStateChanged(
    listener: (state: Live2dModelSwitchState) => void
  ): () => void {
    const channel = "panel:model-switch-state-changed";
    const wrappedListener = (_event: unknown, payload: Live2dModelSwitchState) => {
      listener(payload);
    };

    ipcRenderer.on(channel, wrappedListener);

    return () => {
      ipcRenderer.removeListener(channel, wrappedListener);
    };
  },
  runtime: {
    /**
     * 控制网关地址集中暴露，保证 renderer 在开发与测试环境都使用同一组默认值。
     * HTTP 地址会被归一化为控制服务根地址，避免误填 /console 页面后让 renderer 拼出错误接口路径。
     */
    controlGateway: bootstrap.runtime.controlGateway,
    live2d: {
      /**
       * panel 只消费只读 catalog，不接触单独模型路径，避免把“当前显示哪个模型”的职责提前拉进面板窗口。
       */
      catalog: bootstrap.runtime.live2d.catalog
    }
  }
});
}
