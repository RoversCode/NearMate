import { spawn, type ChildProcess } from "node:child_process";
import { createServer, type Server } from "node:net";
import { createSocket, type Socket } from "node:dgram";
import { join } from "node:path";
import { appendBackendDiagnostic } from "./backendDiagnostics";
import { createLiveKitHealthToken, createLiveKitServerEnvironment, createLocalLiveKitConfiguration, readExternalLiveKitConnection, type LiveKitConnectionSettings, type LocalLiveKitPorts } from "./liveKitConfiguration";

type PortReservation = LocalLiveKitPorts & { release(): Promise<void> };
export type LiveKitUnexpectedTermination = { kind: "exit" | "error"; pid: number | null; code: number | null; signal: NodeJS.Signals | null; logPath: string };
type Options = {
  executablePath: string | null;
  userDataDir: string;
  environment?: NodeJS.ProcessEnv;
  spawnProcess?: typeof spawn;
  reservePorts?: () => Promise<PortReservation>;
  checkReady?: (connection: LiveKitConnectionSettings, signal: AbortSignal) => Promise<boolean>;
  reportDiagnostic?: (event: string, fields: Record<string, string | number | null>) => void;
  onUnexpectedTermination?: (termination: LiveKitUnexpectedTermination) => void | Promise<void>;
  startupTimeoutMs?: number;
  pollIntervalMs?: number;
  stopTimeoutMs?: number;
};
export type ManagedLiveKitSupervisor = { start(): Promise<LiveKitConnectionSettings>; stop(): Promise<void>; forceStop(): void };
type OwnedChild = {
  process: ChildProcess;
  exited: boolean;
  failed: boolean;
  ready: boolean;
  stopping: boolean;
  notified: boolean;
  exit: Promise<void>;
};

/**
 * 监管安装版自己创建的本机媒体服务；外部服务完全不归它启停。
 *
 * @param options 随包路径、用户日志目录和可替换的进程/网络端口。
 * @returns 启动返回主进程连接配置，停止只触及自身持有的 child。
 */
export function createManagedLiveKitSupervisor(options: Options): ManagedLiveKitSupervisor {
  const environment = options.environment ?? process.env;
  const logPath = join(options.userDataDir, "logs", "livekit-runtime.log");
  let child: OwnedChild | null = null;
  let connection: LiveKitConnectionSettings | null = null;
  let startTask: Promise<LiveKitConnectionSettings> | null = null;
  let stopTask: Promise<void> | null = null;
  let startupController: AbortController | null = null;

  function record(event: string, fields: Record<string, string | number | null> = {}): void {
    const safeFields = { pid: child?.process.pid ?? null, ...fields };
    if (options.reportDiagnostic) options.reportDiagnostic(event, safeFields);
    else appendBackendDiagnostic(logPath, `${JSON.stringify({ timestamp: new Date().toISOString(), event, ...safeFields })}\n`);
  }

  function notify(state: OwnedChild, kind: "exit" | "error", code: number | null, signal: NodeJS.Signals | null): void {
    if (!state.ready || state.stopping || state.notified) return;
    state.notified = true;
    try {
      Promise.resolve(options.onUnexpectedTermination?.({ kind, code, signal, pid: state.process.pid ?? null, logPath }))
        .catch(() => record("livekit.notification_failed"));
    } catch {
      record("livekit.notification_failed");
    }
  }

  function forceStop(): void {
    startupController?.abort(new Error("本地语音服务启动已取消。"));
    const state = child;
    if (!state || state.exited) return;
    state.stopping = true;
    record("livekit.force_stop_requested");
    try { state.process.kill("SIGKILL"); } catch { record("livekit.force_stop_failed"); }
  }

  async function stop(): Promise<void> {
    startupController?.abort(new Error("本地语音服务启动已取消。"));
    if (stopTask) return stopTask;
    const state = child;
    if (!state || state.exited) return;
    state.stopping = true;
    const stopping = (async () => {
      record("livekit.stop_requested");
      try { state.process.kill("SIGTERM"); } catch { record("livekit.stop_signal_failed"); }
      if (!await waitForExit(state, options.stopTimeoutMs ?? 1500)) {
        forceStop();
        if (!await waitForExit(state, 1000)) throw new Error("本地语音服务尚未退出，请关闭 NearMate 后重试。");
      }
      if (child === state) child = null;
      connection = null;
    })();
    stopTask = stopping;
    try { await stopping; } finally { if (stopTask === stopping) stopTask = null; }
  }

  async function start(): Promise<LiveKitConnectionSettings> {
    if (stopTask) await stopTask;
    if (startTask) return startTask;
    if (child && !child.exited) {
      if (child.ready && !child.stopping && !child.failed && connection) return connection;
      throw new Error("本地语音服务尚未退出，请关闭 NearMate 后重试。");
    }
    const external = readExternalLiveKitConnection(environment);
    if (external) return external;
    if (!options.executablePath) throw new Error("安装包缺少本地语音服务，请重新安装完整版本。");
    const controller = new AbortController();
    startupController = controller;
    const deadline = setTimeout(() => controller.abort(new Error("本地语音服务启动超时，请重新打开 NearMate。")), options.startupTimeoutMs ?? 15_000);
    const starting = (async () => {
      record("livekit.start_requested");
      try {
        const allocation = (options.reservePorts ?? reserveLocalPorts)().then(async reserved => {
          if (controller.signal.aborted) await reserved.release();
          return reserved;
        });
        const reserved = await waitForOperation(allocation, controller.signal);
        const local = createLocalLiveKitConfiguration(reserved);
        await reserved.release();
        if (controller.signal.aborted) throw controller.signal.reason;
        let process: ChildProcess;
        try {
          process = (options.spawnProcess ?? spawn)(options.executablePath!, [], {
            env: createLiveKitServerEnvironment(environment, local.configBody), stdio: "ignore", windowsHide: true
          });
        } catch { throw new Error("无法启动本地语音服务，请重新安装完整版本后重试。"); }
        let resolveExit!: () => void;
        const state: OwnedChild = { process, exited: false, failed: false, ready: false, stopping: false, notified: false, exit: new Promise(resolve => { resolveExit = resolve; }) };
        child = state;
        connection = local.connection;
        process.once("exit", (code, signal) => {
          state.exited = true;
          resolveExit();
          record("livekit.exited", { pid: state.process.pid ?? null, code, signal });
          if (!state.ready && !state.stopping) controller.abort(new Error("本地语音服务启动失败，请查看诊断日志后重试。"));
          notify(state, "exit", code, signal);
        });
        process.once("error", () => {
          state.failed = true;
          record("livekit.process_error", { pid: state.process.pid ?? null });
          if (!state.ready && !state.stopping) controller.abort(new Error("本地语音服务启动失败，请查看诊断日志后重试。"));
          // spawn 失败没有进程可等待；运行中 error 不代表退出，仍保留 child 供停止收口。
          if (!process.pid) { state.exited = true; resolveExit(); }
          notify(state, "error", null, null);
        });
        while (!controller.signal.aborted) {
          if (state.exited || state.failed) throw new Error("本地语音服务启动失败，请查看诊断日志后重试。");
          const ready = await waitForOperation((options.checkReady ?? checkLocalServiceReady)(local.connection, controller.signal), controller.signal);
          if (state.exited || state.failed) throw new Error("本地语音服务启动失败，请查看诊断日志后重试。");
          if (ready) {
            state.ready = true;
            record("livekit.ready");
            return local.connection;
          }
          await waitForOperation(new Promise(resolve => setTimeout(resolve, options.pollIntervalMs ?? 200)), controller.signal);
        }
        throw controller.signal.reason;
      } catch (error) {
        record("livekit.start_failed");
        try { await stop(); } catch { record("livekit.cleanup_incomplete"); }
        throw error instanceof Error ? error : new Error("本地语音服务启动失败。");
      } finally {
        clearTimeout(deadline);
        if (startupController === controller) startupController = null;
      }
    })();
    startTask = starting;
    try { return await starting; } finally { if (startTask === starting) startTask = null; }
  }

  return { start, stop, forceStop };
}

async function waitForOperation<T>(operation: Promise<T>, signal: AbortSignal): Promise<T> {
  let abort!: () => void;
  const cancellation = new Promise<never>((_resolve, reject) => {
    abort = () => reject(signal.reason);
    signal.addEventListener("abort", abort, { once: true });
    if (signal.aborted) abort();
  });
  try { return await Promise.race([operation, cancellation]); }
  finally { signal.removeEventListener("abort", abort); }
}

async function waitForExit(state: OwnedChild, timeoutMs: number): Promise<boolean> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  try { return await Promise.race([state.exit.then(() => true), new Promise<false>(resolve => { timer = setTimeout(() => resolve(false), timeoutMs); })]); }
  finally { clearTimeout(timer); }
}

async function checkLocalServiceReady(connection: LiveKitConnectionSettings, signal: AbortSignal): Promise<boolean> {
  try {
    const response = await fetch(connection.url.replace(/^ws/, "http") + "/twirp/livekit.RoomService/ListRooms", {
      method: "POST", headers: { Authorization: `Bearer ${createLiveKitHealthToken(connection)}`, "Content-Type": "application/json" },
      body: "{}", signal: AbortSignal.any([signal, AbortSignal.timeout(1000)])
    });
    return response.ok;
  } catch { return false; }
}

async function reserveLocalPorts(): Promise<PortReservation> {
  const tcpServers: Server[] = [];
  let udp: Socket | null = null;
  let released = false;
  const release = async () => {
    if (released) return;
    released = true;
    await Promise.all(tcpServers.map(server => new Promise<void>(resolve => server.close(() => resolve()))));
    if (udp) await new Promise<void>(resolve => { try { udp!.close(() => resolve()); } catch { resolve(); } });
  };
  try {
    const ports: number[] = [];
    for (let index = 0; index < 2; index++) {
      const server = createServer();
      tcpServers.push(server);
      await new Promise<void>((resolve, reject) => { server.once("error", reject); server.listen(0, "127.0.0.1", resolve); });
      ports.push((server.address() as { port: number }).port);
    }
    udp = createSocket("udp4");
    await new Promise<void>((resolve, reject) => { udp!.once("error", reject); udp!.bind(0, "127.0.0.1", resolve); });
    return { httpPort: ports[0], tcpPort: ports[1], udpPort: udp.address().port, release };
  } catch {
    await release();
    throw new Error("无法为本地语音服务分配端口，请重启 NearMate 后重试。");
  }
}
