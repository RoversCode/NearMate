import type { WebContents } from "electron";
import { resolveControlHttpRootUrl } from "../../shared/controlHttpUrl";
import { createLiveKitSessionClient } from "../services/liveKitSessionClient";
import type { AuthorizedIpcMain } from "./authorizedIpcMain";
import type { RegisterShellIpcHandlersInput } from "./shellIpcTypes";

const AVATAR_WINDOW = ["avatar"] as const;

type RendererLiveKitSessionPayload = {
  url: string;
  room_name: string;
  desktop_ui: {
    identity: string;
    token: string;
  };
  audio_input_status: "available" | "audio_unavailable";
};

/**
 * registerLiveKitSessionIpcHandler 注册 renderer 创建 LiveKit session 的 invoke handler。
 *
 * @param ipcMain Electron IPC 主进程对象。
 * @param input IPC 处理器依赖。
 */
export function registerLiveKitSessionIpcHandler(
  ipcMain: Pick<AuthorizedIpcMain, "handle">,
  input: RegisterShellIpcHandlersInput
): void {
  let liveKitSessionStartTask: Promise<RendererLiveKitSessionPayload> | null = null;
  let activeController: AbortController | null = null;
  let activeSender: WebContents | null = null;

  ipcMain.handle("livekit:cancel-session", AVATAR_WINDOW, event => {
    if (event.sender !== activeSender) return;
    activeController?.abort(new Error("媒体会话请求已取消。"));
  });

  ipcMain.handle("livekit:create-session", AVATAR_WINDOW, event => {
    if (liveKitSessionStartTask !== null) {
      return liveKitSessionStartTask;
    }

    const controller = new AbortController();
    activeController = controller;
    const sender = event.sender;
    activeSender = sender;
    const cancel = () => controller.abort(new Error("媒体会话请求已取消。"));
    // 窗口销毁或 renderer 崩溃时 React cleanup 未必执行，main 必须自己阻止迟到请求打开麦克风。
    sender.once("destroyed", cancel);
    sender.once("render-process-gone", cancel);
    if (sender.isDestroyed()) cancel();
    liveKitSessionStartTask = createRendererLiveKitSession(input, controller.signal).finally(() => {
      sender.removeListener("destroyed", cancel);
      sender.removeListener("render-process-gone", cancel);
      liveKitSessionStartTask = null;
      if (activeController === controller) {
        activeController = null;
        activeSender = null;
      }
    });

    return liveKitSessionStartTask;
  });
}

/**
 * createRendererLiveKitSession 创建桌面 LiveKit 会话并启动 AEC sidecar。
 *
 * @param input IPC 处理器依赖。
 * @param signal 窗口卸载或设备替换时取消旧会话，防止迟到响应重新打开麦克风。
 * @returns renderer 只能读取的 UI 参与者会话载荷。
 */
async function createRendererLiveKitSession(
  input: RegisterShellIpcHandlersInput,
  signal: AbortSignal
): Promise<RendererLiveKitSessionPayload> {
  signal.throwIfAborted();
  const httpBaseUrl = resolveControlHttpRootUrl(
    process.env["COMPANION_CONTROL_HTTP_URL"]
  );
  const sessionClient = createLiveKitSessionClient({
    httpBaseUrl
  });
  const session = await sessionClient.createSession(signal);
  signal.throwIfAborted();

  let audioInputStatus: "available" | "audio_unavailable" = "available";
  try {
    await input.aecSidecarSupervisor.start({
      livekitUrl: session.url,
      roomName: session.roomName,
      identity: session.desktopAec.identity,
      token: session.desktopAec.token,
      debugAudio: session.aecSidecarDebugAudio,
      audioDeviceSelection: session.audioDeviceSelection
    });
  } catch {
    audioInputStatus = "audio_unavailable";
    if (!signal.aborted) console.error("[companion-main] 麦克风组件不可用，继续建立语音播放连接。");
  }
  if (signal.aborted) {
    // AEC 启动后才收到取消时，必须先收回本轮麦克风，再释放 single-flight 给下一代。
    if (input.aecSidecarSupervisor.stopAndWait) await input.aecSidecarSupervisor.stopAndWait();
    else input.aecSidecarSupervisor.stop?.();
    signal.throwIfAborted();
  }

  return {
    url: session.url,
    room_name: session.roomName,
    desktop_ui: session.desktopUi,
    audio_input_status: audioInputStatus
  };
}
