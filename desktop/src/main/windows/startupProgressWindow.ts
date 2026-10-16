import { BrowserWindow } from "electron";
import { createStartupProgressController, type StartupProgressState } from "./startupProgressController";

/**
 * StartupProgressWindow 在真实阶段内显示等待进度，成功反馈完成后交回显示权。
 */
export type StartupProgressWindow = {
  /** 设置阶段起点、动画上限和提示；返回值为空，普通更新最多显示 99%。 */
  update(state: StartupProgressState): void;
  /** 显示 100%，等待绘制并停留 300ms；返回可取消、有界结束的 Promise。 */
  complete(): Promise<void>;
  close(): void;
};

const STARTUP_FEEDBACK_TIMEOUT_MS = 3_000;

const STARTUP_PROGRESS_HTML = `<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'">
    <title>NearMate 正在启动</title>
    <style>
      :root {
        color-scheme: light;
        font-family: "Avenir Next", "PingFang SC", "Microsoft YaHei", sans-serif;
        color: #173042;
        background: #f7f0e7;
      }
      * { box-sizing: border-box; }
      html, body { width: 100%; height: 100%; margin: 0; }
      body {
        display: grid;
        place-items: center;
        overflow: hidden;
        background:
          radial-gradient(circle at 15% 0%, rgba(227, 180, 92, .28), transparent 42%),
          radial-gradient(circle at 100% 100%, rgba(113, 168, 165, .22), transparent 45%),
          #f7f0e7;
      }
      main { width: 100%; padding: 30px 34px 28px; }
      .brand { display: flex; align-items: center; gap: 12px; margin-bottom: 25px; }
      .mark {
        display: grid;
        width: 42px;
        height: 42px;
        place-items: center;
        border-radius: 14px;
        background: linear-gradient(135deg, #e3b45c, #df7d57);
        color: #fffaf4;
        font-size: 22px;
        font-weight: 700;
        box-shadow: 0 8px 20px rgba(223, 125, 87, .22);
      }
      h1 { margin: 0; font-size: 21px; letter-spacing: .01em; }
      .subtitle { margin: 5px 0 0; color: #4f6571; font-size: 12px; }
      .status { min-height: 52px; }
      .status-title { margin: 0 0 7px; font-size: 15px; font-weight: 700; }
      .status-detail { margin: 0; color: #4f6571; font-size: 12px; line-height: 1.5; }
      .progress-track { height: 8px; margin-top: 24px; overflow: hidden; border-radius: 999px; background: rgba(23, 48, 66, .12); }
      .progress-bar { width: 0%; height: 100%; border-radius: inherit; background: linear-gradient(90deg, #71a8a5, #e3b45c, #df7d57); transition: width 180ms ease-out; }
      .progress-label { margin-top: 9px; color: #4f6571; font-size: 11px; text-align: right; }
      @media (prefers-reduced-motion: reduce) { .progress-bar { transition: none; } }
    </style>
  </head>
  <body>
    <main>
      <div class="brand"><div class="mark">N</div><div><h1>NearMate</h1><p class="subtitle">正在准备你的桌面工作搭子</p></div></div>
      <section class="status"><p class="status-title" id="status-title" aria-live="polite" aria-atomic="true">正在启动</p><p class="status-detail" id="status-detail">请稍候，NearMate 很快就会出现。</p></section>
      <div class="progress-track" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0"><div class="progress-bar" id="progress-bar"></div></div>
      <div class="progress-label" id="progress-label">0%</div>
    </main>
    <script>
      var startupCompleted = false;
      window.updateStartupProgress = function (state) {
        if (startupCompleted && state.progress < 100) return;
        var progress = Math.max(0, Math.min(100, Number(state.progress) || 0));
        // 辅助技术只播报真实阶段标题；每 200ms 的动画不能反复触发整页朗读。
        var title = document.getElementById("status-title");
        var detail = document.getElementById("status-detail");
        if (title.textContent !== state.title) title.textContent = state.title;
        if (detail.textContent !== state.detail) detail.textContent = state.detail;
        document.getElementById("progress-bar").style.width = progress + "%";
        document.getElementById("progress-label").textContent = Math.floor(progress) + "%";
        document.querySelector("[role=progressbar]").setAttribute("aria-valuenow", String(Math.floor(progress)));
      };
      window.completeStartupProgress = function (state) {
        startupCompleted = true;
        window.updateStartupProgress(state);
        return new Promise(function (resolve) {
          var bar = document.getElementById("progress-bar");
          var finished = false;
          var finish = function () {
            if (finished) return;
            finished = true;
            bar.removeEventListener("transitionend", finish);
            clearTimeout(fallback);
            requestAnimationFrame(function () { requestAnimationFrame(resolve); });
          };
          // reduced-motion 不派发 transitionend，短兜底仍等待绘制机会。
          var fallback = setTimeout(finish, 220);
          bar.addEventListener("transitionend", finish);
        });
      };
    </script>
  </body>
</html>`;

/**
 * createStartupProgressWindow 创建安装版启动时的即时反馈窗口。
 * 该窗口使用内联静态页面，不依赖 Backend、控制网关或主应用 preload，
 * 因此可以在这些慢操作开始前就显示出来。
 *
 * @returns 可更新阶段、等待成功绘制和主动关闭的启动进度窗口控制器。
 */
export function createStartupProgressWindow(): StartupProgressWindow {
  if (typeof BrowserWindow !== "function") {
    return {
      update() {},
      complete: async () => {},
      close() {}
    };
  }

  let window: BrowserWindow;
  try {
    window = new BrowserWindow({
      width: 480,
      height: 300,
      minWidth: 480,
      minHeight: 300,
      maxWidth: 480,
      maxHeight: 300,
      show: true,
      center: true,
      frame: false,
      resizable: false,
      movable: true,
      minimizable: false,
      maximizable: false,
      closable: false,
      skipTaskbar: true,
      backgroundColor: "#f7f0e7",
      title: "NearMate 正在启动",
      webPreferences: {
        contextIsolation: true,
        nodeIntegration: false,
        sandbox: true
      }
    });
  } catch (error) {
    // 加载反馈是辅助能力，测试替身或极早期 Electron 不支持建窗时不能阻断主启动。
    console.warn("[companion-main] 启动进度窗口创建失败", error);
    return {
      update() {},
      complete: async () => {},
      close() {}
    };
  }

  let isReady = false;
  let isClosed = false;
  let pendingState: StartupProgressState | null = null;
  let resolveReady!: () => void;
  const ready = new Promise<void>(resolve => { resolveReady = resolve; });

  const flushPendingState = (): void => {
    if (!isReady || isClosed || !pendingState || window.isDestroyed()) return;

    const state = pendingState;
    pendingState = null;
    const serializedState = JSON.stringify(state);
    void window.webContents
      .executeJavaScript(`window.updateStartupProgress(${serializedState})`, true)
      .catch(error => {
        // 启动反馈失败不应阻断 NearMate 本身的启动流程。
        console.warn("[companion-main] 启动进度窗口更新失败", error);
      });
  };

  window.webContents.once("did-finish-load", () => {
    isReady = true;
    resolveReady();
    flushPendingState();
  });
  void window
    .loadURL(`data:text/html;charset=UTF-8,${encodeURIComponent(STARTUP_PROGRESS_HTML)}`)
    .then(() => {
      // 测试替身和少数 Electron 版本可能只解析 loadURL 而不派发事件，保留这个兜底。
      isReady = true;
      resolveReady();
      flushPendingState();
    })
    .catch(error => {
      resolveReady();
      console.warn("[companion-main] 启动进度窗口加载失败", error);
    });

  const controller = createStartupProgressController({
    async render(state, waitForPaint, signal) {
      if (isClosed || window.isDestroyed()) return;
      if (!waitForPaint) {
        pendingState = state;
        flushPendingState();
        return;
      }
      // 加载、脚本执行和首帧都可能永不回执；反馈窗口不能无限阻挡已经装配好的主应用。
      await waitForStartupFeedback(ready, signal);
      if (signal.aborted || isClosed || !isReady || window.isDestroyed()) return;
      pendingState = null;
      await waitForStartupFeedback(window.webContents.executeJavaScript(
        `window.completeStartupProgress(${JSON.stringify(state)})`, true
      ), signal);
    }
  });

  return {
    update(state) {
      if (isClosed) return;
      controller.update(state);
    },
    complete: () => controller.complete(),
    close() {
      if (isClosed) return;
      isClosed = true;
      controller.close();
      resolveReady();
      pendingState = null;
      if (!window.isDestroyed()) {
        // Windows 上 closable:false 会让 BrowserWindow.close() 直接失效；这里是程序主动收口，必须强制销毁。
        window.destroy();
      }
    }
  };
}

/**
 * 有界等待辅助窗口的加载或绘制，退出时立即结束。
 *
 * Args:
 *   operation: 页面加载或脚本绘制回执。
 *   signal: 启动窗口关闭时触发的取消信号。
 * Returns:
 *   回执、取消或超时后的 Promise；超时保留诊断错误。
 */
function waitForStartupFeedback(operation: Promise<unknown>, signal: AbortSignal): Promise<void> {
  if (signal.aborted) return Promise.resolve();
  return new Promise((resolve, reject) => {
    const finish = (error?: unknown): void => {
      clearTimeout(timer);
      signal.removeEventListener("abort", cancel);
      if (error) reject(error);
      else resolve();
    };
    const cancel = (): void => finish();
    const timer = setTimeout(() => finish(new Error("启动反馈窗口等待超时")), STARTUP_FEEDBACK_TIMEOUT_MS);
    signal.addEventListener("abort", cancel, { once: true });
    operation.then(() => finish(), error => finish(error));
  });
}
