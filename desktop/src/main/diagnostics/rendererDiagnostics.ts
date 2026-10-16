import type { BrowserWindow } from "electron";
import type { RendererDiagnosticPayload } from "../../shared/rendererDiagnostics";
import type { WindowKind } from "../contracts/windowKinds";

/**
 * DIAGNOSTIC_MESSAGE_LABELS 统一维护 renderer 侧 ASCII message code 到终端中文文案的映射。
 * 这样跨进程链路只传稳定 ASCII，真正展示给 PowerShell 的中文在 main 进程本地生成，避开开发态日志乱码。
 */
const DIAGNOSTIC_MESSAGE_LABELS: Record<string, string> = {
  "live2d.pixi_stage_initialized": "[companion-live2d] Pixi 舞台已初始化",
  "live2d.model_load_started": "[companion-live2d] 准备加载桌宠模型",
  "live2d.model_loaded": "[companion-live2d] Live2D 模型已加载",
  "live2d.model_load_failed": "[companion-live2d] 桌宠 Live2D 载入失败",
  "avatar.live2d_lip_sync_audio_close_failed": "[companion-live2d] 唇形同步音频分析关闭失败",
  "avatar.live2d_lip_sync_audio_bound": "[companion-live2d] 唇形同步音频源已绑定",
  "avatar.live2d_lip_sync_audio_failed": "[companion-live2d] 唇形同步音频分析创建失败",
  "avatar.live2d_lip_sync_audio_resume_failed": "[companion-live2d] 唇形同步音频上下文恢复失败",
  "avatar.live2d_lip_sync_close_failed": "[companion-live2d] 唇形同步闭嘴写入失败",
  "avatar.live2d_lip_sync_frame": "[companion-live2d] 唇形同步音频帧",
  "avatar.live2d_lip_sync_model_bound": "[companion-live2d] 唇形同步已绑定模型",
  "avatar.live2d_lip_sync_model_unsupported": "[companion-live2d] 唇形同步无法写入模型",
  "avatar.live2d_lip_sync_update_failed": "[companion-live2d] 唇形同步更新失败",
  "control.settings_update_unreachable": "[companion-control] 设置同步接口不可达，保留真实连接并等待用户重试。"
};

/**
 * attachRendererDiagnostics 负责把指定窗口的 renderer 诊断事件转发到主进程终端。
 * 核心入参是 BrowserWindow 与窗口类型，输出为空；它只注册监听器，不改变窗口业务行为。
 * 边界异常：部分 Electron 事件在不同版本里参数形态可能不同，因此这里只做宽松记录，避免诊断代码影响启动。
 */
export function attachRendererDiagnostics(window: BrowserWindow, windowKind: WindowKind): void {
  const logPrefix = `[companion-renderer:${windowKind}]`;
  const webContents = window.webContents;

  /**
   * 单元测试里的假窗口只模拟 shell 生命周期，不承载真实 webContents。
   * 诊断能力不是业务必需依赖，遇到这种窄 fake 时直接跳过，避免调试代码污染控制器测试。
   */
  if (!webContents) {
    return;
  }

  /**
   * console-message 是 renderer 内最关键的诊断入口。
   * 业务意图是让 Live2D 加载、资源请求和布局日志都能回到根目录 `pnpm desktop:dev` 终端。
   */
  webContents.on("console-message", (_event, level, message, line, sourceId) => {
    console.log(
      `${logPrefix} console level=${level} source=${sourceId}:${line} ${message}`
    );
  });

  /**
   * 页面资源加载失败时，Electron 默认不会把失败 URL 直接展示在终端。
   * 这里记录失败码与 URL，方便区分是 model3、moc3、贴图还是 runtime 脚本缺失。
   */
  webContents.on(
    "did-fail-load",
    (_event, errorCode, errorDescription, validatedURL, isMainFrame) => {
      console.error(
        `${logPrefix} load-failed code=${errorCode} mainFrame=${isMainFrame} url=${validatedURL} ${errorDescription}`
      );
    }
  );

  /**
   * preload 出错会导致 renderer 拿不到模型路径与 IPC bridge。
   * 这类错误必须在主进程显式记录，否则头像窗口可能只表现为空白或只剩静态按钮。
   */
  webContents.on("preload-error", (_event, preloadPath, error) => {
    console.error(`${logPrefix} preload-error path=${preloadPath}`, error);
  });

  /**
   * WebGL 或 renderer 进程崩溃时，Live2D 画面会直接消失。
   * 记录 gone reason 可以把图形层问题和资源层问题分开排查。
   */
  webContents.on("render-process-gone", (_event, details) => {
    console.error(`${logPrefix} render-process-gone`, details);
  });
}

/**
 * readRendererDiagnosticPayload 从未知 IPC 载荷中提取稳定的诊断结构。
 * 这里显式校验等级与消息文本，避免 renderer 误发空消息或未知等级污染主进程终端。
 */
export function readRendererDiagnosticPayload(
  payload: unknown
): RendererDiagnosticPayload | null {
  if (!payload || typeof payload !== "object") {
    return null;
  }

  const candidate = payload as Partial<RendererDiagnosticPayload>;

  if (
    candidate.level !== "info" &&
    candidate.level !== "warn" &&
    candidate.level !== "error"
  ) {
    return null;
  }

  if (typeof candidate.message !== "string") {
    return null;
  }

  const normalizedMessage = candidate.message.trim();

  if (!normalizedMessage) {
    return null;
  }

  return {
    level: candidate.level,
    message: normalizedMessage,
    details: candidate.details
  };
}

/**
 * logRendererDiagnostic 负责把显式诊断打印到主进程终端。
 * 这里保留窗口前缀，方便把 avatar 与 panel 的中文诊断和原有 console-message 日志区分开。
 */
export function logRendererDiagnostic(
  windowKind: WindowKind,
  payload: RendererDiagnosticPayload
): void {
  /**
   * renderer 诊断消息优先按约定 code 映射为主进程本地中文文案。
   * 如果未来有未登记的新 code，仍然回退输出原 message，保证调试链路不断。
   */
  const resolvedMessage = DIAGNOSTIC_MESSAGE_LABELS[payload.message] ?? payload.message;
  const terminalMessage = `[companion-renderer:${windowKind}] diagnostic level=${payload.level} ${resolvedMessage}`;

  /**
   * details 只在 renderer 主动提供上下文时追加打印。
   * 这样终端输出既保留中文主线，又不会被无意义的 undefined 参数污染。
   */
  if (payload.details === undefined) {
    if (payload.level === "info") {
      console.info(terminalMessage);
      return;
    }

    if (payload.level === "warn") {
      console.warn(terminalMessage);
      return;
    }

    console.error(terminalMessage);
    return;
  }

  /**
   * 带详情的诊断需要把对象原样交给终端，便于开发态直接展开查看上下文。
   */
  if (payload.level === "info") {
    console.info(terminalMessage, payload.details);
    return;
  }

  if (payload.level === "warn") {
    console.warn(terminalMessage, payload.details);
    return;
  }

  console.error(terminalMessage, payload.details);
}
