import { spawn, type ChildProcess } from "node:child_process";
import { randomUUID } from "node:crypto";
import { join } from "node:path";
import { createPackagedBackendEnvironment } from "./backendEnvironment";
import { appendBackendDiagnostic } from "./backendDiagnostics";
import type { NearMateRuntimeLayout } from "./runtimeLayout";
import type { LiveKitConnectionSettings } from "./liveKitConfiguration";

type BackendSupervisorOptions = {
  pythonExe: string;
  cwd: string;
  userDataDir: string;
  runtimeLayout: NearMateRuntimeLayout;
  controlUrl: string;
  liveKitConnection?: LiveKitConnectionSettings;
  spawnProcess?: typeof spawn;
  fetchImpl?: typeof fetch;
  startupTimeoutMs?: number;
  pollIntervalMs?: number;
  healthRequestTimeoutMs?: number;
  stopTimeoutMs?: number;
  /** 后台已就绪后意外结束时通知壳层，不自动重启或重放任务。 */
  onUnexpectedTermination?: (termination: BackendUnexpectedTermination) => void | Promise<void>;
  /** 仅用于测试注入平台，生产代码默认读取当前 Node 平台。 */
  platform?: NodeJS.Platform;
};

/** 已启动后台的安全退出摘要，不携带标准输出、环境变量或凭据。 */
export type BackendUnexpectedTermination = {
  kind: "exit" | "error";
  pid: number | null;
  code: number | null;
  signal: NodeJS.Signals | null;
  logPath: string;
};

export type BackendSupervisor = {
  start(): Promise<void>;
  stop(): Promise<void>;
  /** 立即发出第二次进程树终止请求，不等待 child 自己收口。 */
  forceStop(): void;
};

const DEFAULT_HEALTH_REQUEST_TIMEOUT_MS = 2_000;
const DEFAULT_STOP_TIMEOUT_MS = 5_000;
// 已配置用户启动时要顺序加载多套本地模型，30 秒会在正常冷启动过程中误杀后台。
const DEFAULT_STARTUP_TIMEOUT_MS = 120_000;

type BackendStartupFailureCode =
  | "startup_timeout"
  | "startup_cancelled"
  | "spawn_failed"
  | "process_exited"
  | "control_port_occupied"
  | "startup_failed";

/** 带有固定失败类型的启动错误，日志和界面无需从原始异常文案猜测原因。 */
class BackendStartupError extends Error {
  /**
   * Args:
   *   failureCode: 不含进程输出、路径或凭据的固定失败类型。
   *   message: 给调用方显示的错误说明。
   *   options: 保留原始 cause 供内部调试，不写入结构化日志。
   */
  constructor(readonly failureCode: BackendStartupFailureCode, message: string, options?: ErrorOptions) {
    super(message, options);
    this.name = "BackendStartupError";
  }
}

type BackendChildTermination =
  | {
      kind: "exit";
      code: number | null;
      signal: NodeJS.Signals | null;
    }
  | {
      kind: "error";
      error: Error;
    };

type BackendChildState = {
  child: ChildProcess;
  termination: Promise<BackendChildTermination>;
  terminated: boolean;
  ready: boolean;
  stopRequested: boolean;
  instanceId: string;
};

/**
 * 管理安装态 Backend 的唯一子进程。
 *
 * Args:
 *   options: Python、资源目录、用户数据目录和健康检查地址。
 *
 * Returns:
 *   可重复调用的启动/停止控制器。
 */
export function createBackendSupervisor(options: BackendSupervisorOptions): BackendSupervisor {
  const spawnProcess = options.spawnProcess ?? spawn;
  const fetchImpl = options.fetchImpl ?? fetch;
  let child: ChildProcess | null = null;
  let childState: BackendChildState | null = null;
  let startPromise: Promise<void> | null = null;
  let stopPromise: Promise<void> | null = null;
  let lifecycleGeneration = 0;
  const platform = options.platform ?? process.platform;
  const startupLogPath = join(options.userDataDir, "logs", "backend-startup.log");

  /** 只写入明确挑选的生命周期字段，避免把环境变量和异常对象中的凭据带入日志。 */
  function recordLifecycle(
    event: string,
    state: BackendChildState | null,
    fields: Record<string, string | number | boolean | null> = {}
  ): void {
    appendBackendDiagnostic(startupLogPath, `\n[nearmate-backend] ${JSON.stringify({
      timestamp: new Date().toISOString(), event,
      pid: state?.child.pid ?? null,
      instance_id: state?.instanceId ?? null,
      ...fields
    })}\n`);
  }

  /**
   * forceStop 只负责把终止信号再次发给当前 Backend，不等待它返回。
   *
   * 退出清理已经耗尽等待预算时，不能继续依赖一个可能永久挂起的 stop Promise。
   * Windows 使用 taskkill 递归结束 Python 进程树；其它平台使用 SIGKILL，避免子进程
   * 在 Desktop 退出后脱离生命周期。真正的 exit 事件仍由 observeBackendChild 收口。
   *
   * Returns:
   *   无；请求失败只记录日志，后续仍由 supervisor 的状态观察与下次 stop 负责诊断。
   */
  function forceStop(): void {
    lifecycleGeneration += 1;
    const currentState = childState;
    const currentChild = currentState?.child ?? child;
    if (!currentChild || currentState?.terminated || currentChild.exitCode !== null) {
      return;
    }

    if (currentState) currentState.stopRequested = true;
    recordLifecycle("backend.force_stop_requested", currentState);

    if (platform === "win32" && currentChild.pid) {
      try {
        const killer = spawnProcess("taskkill", ["/pid", String(currentChild.pid), "/T", "/F"], {
          stdio: "ignore",
          windowsHide: true
        });
        killer.once("error", error => {
          console.error("[companion-main] 强制停止 Backend 失败", error);
        });
        killer.once("exit", (code, signal) => {
          if (code !== 0 && !currentState?.terminated && currentChild.exitCode === null) {
            console.error(
              `[companion-main] 强制停止 Backend 未确认成功：退出码 ${String(code)}，信号 ${String(signal)}`
            );
          }
        });
      } catch (error) {
        console.error("[companion-main] 发起 Backend 强制停止失败", error);
      }
      return;
    }

    try {
      currentChild.kill("SIGKILL");
    } catch (error) {
      console.error("[companion-main] 发起 Backend 强制停止失败", error);
    }
  }

  async function start(): Promise<void> {
    // stop() 已经发出终止请求时，必须等旧进程真实退出后才能启动下一代。
    // 这一步放在“已有 child 直接返回”之前，避免 stop 期间误把旧进程当成可复用实例。
    if (stopPromise) {
      await stopPromise;
      return start();
    }
    // child 已经 spawn 不代表健康检查已经完成。并发启动必须共享同一个 Promise，
    // 否则第二个调用方会在 Backend 尚未接管控制端口时提前创建窗口。
    if (startPromise) {
      return startPromise;
    }
    if (childState && !childState.terminated && childState.child.exitCode === null) {
      return;
    }
    // 进程已经退出但 exitCode 尚未由测试替身同步更新时，也要丢弃旧代际引用。
    if (childState?.terminated) {
      child = null;
      childState = null;
    }
    const startedGeneration = lifecycleGeneration;
    const startupTimeoutMs = options.startupTimeoutMs ?? DEFAULT_STARTUP_TIMEOUT_MS;
    const startedAt = Date.now();
    // 每次启动生成一次性的运行身份，健康检查用它区分本尊和抢占端口的既有实例（僵尸 Backend）。
    const instanceId = randomUUID();
    let startedState: BackendChildState | null = null;
    const pendingStart = (async () => {
      // 停止旧 Backend 时必须先等进程树真正退出，避免新旧实例同时抢控制端口。
      if (stopPromise) {
        await stopPromise;
      }
      // stop() 可能在上面的等待期间发生；被取代的启动不得再 spawn 新进程。
      if (lifecycleGeneration !== startedGeneration) {
        throw new BackendStartupError("startup_cancelled", "Backend start was superseded by stop.");
      }

      const url = new URL(options.controlUrl);
      const environment = createPackagedBackendEnvironment(
        options.runtimeLayout,
        options.userDataDir,
        options.controlUrl,
        instanceId,
        process.env,
        options.liveKitConnection,
      );

      recordLifecycle("backend.start_requested", null, {
        instance_id: instanceId, profile: options.runtimeLayout.profile,
        startup_timeout_ms: startupTimeoutMs
      });

      let spawnedChild: ChildProcess;
      try {
        spawnedChild = spawnProcess(options.pythonExe, ["-I", "-B", "-X", "utf8", "-m", "companion.core.main"], {
          cwd: options.cwd,
          env: environment,
          stdio: ["ignore", "pipe", "pipe"],
          windowsHide: true
        });
      } catch (error) {
        throw new BackendStartupError("spawn_failed", "无法创建本地后台进程，请关闭 NearMate 后重新打开", { cause: error });
      }
      const spawnedState = observeBackendChild(spawnedChild, instanceId);
      startedState = spawnedState;
      child = spawnedChild;
      childState = spawnedState;
      recordLifecycle("backend.spawned", spawnedState);

      spawnedChild.stdout?.on("data", chunk => appendBackendDiagnostic(startupLogPath, chunk));
      spawnedChild.stderr?.on("data", chunk => appendBackendDiagnostic(startupLogPath, chunk));
      // 健康检查成功后仍持续观察这一个进程，原生崩溃不能只留下一个失联的窗口。
      void spawnedState.termination.then(async termination => {
        const code = termination.kind === "exit" ? termination.code : null;
        const signal = termination.kind === "exit" ? termination.signal : null;
        recordLifecycle("backend.terminated", spawnedState, {
          kind: termination.kind, code,
          code_hex: code === null ? null : `0x${(code >>> 0).toString(16).toUpperCase().padStart(8, "0")}`,
          signal, expected: spawnedState.stopRequested,
          phase: spawnedState.ready ? "running" : "starting"
        });
        if (!spawnedState.ready || spawnedState.stopRequested || childState !== spawnedState) return;
        try {
          await options.onUnexpectedTermination?.({
            kind: termination.kind, pid: spawnedChild.pid ?? null, code, signal, logPath: startupLogPath
          });
        } catch {
          recordLifecycle("backend.notification_failed", spawnedState);
        }
      });

      await waitForBackend(
        fetchImpl,
        url,
        spawnedChild,
        spawnedState,
        instanceId,
        startupTimeoutMs,
        options.pollIntervalMs ?? 250,
        options.healthRequestTimeoutMs ?? DEFAULT_HEALTH_REQUEST_TIMEOUT_MS
      );
      // 健康检查刚成功时也要再验一次代际，避免 stop() 在最后一个 await 期间
      // 已经收走 child，却把旧启动误报成成功。
      if (
        lifecycleGeneration !== startedGeneration ||
        childState !== spawnedState
      ) {
        throw new BackendStartupError("startup_cancelled", "Backend start was superseded by stop.");
      }
      if (spawnedState.terminated) {
        const termination = await spawnedState.termination;
        if (termination.kind === "error") {
          throw new BackendStartupError("spawn_failed", `Backend 进程启动失败：${termination.error.message}`);
        }
        throw new BackendStartupError("process_exited", `Backend 在健康检查前退出，退出码 ${termination.code}`);
      }
      spawnedState.ready = true;
      recordLifecycle("backend.ready", spawnedState, { elapsed_ms: Date.now() - startedAt });
    })();

    const wrappedStart = pendingStart.catch(async error => {
      const failureCode = error instanceof BackendStartupError ? error.failureCode : "startup_failed";
      // 停止流程会清空当前 child；先用本次启动持有的身份记录失败，保留它与退出日志的关联。
      recordLifecycle("backend.start_failed", startedState, {
        instance_id: instanceId, failure_code: failureCode,
        startup_timeout_ms: startupTimeoutMs, elapsed_ms: Date.now() - startedAt
      });
      // 启动错误本身比清理错误更有诊断价值；清理失败只记录，不覆盖原始原因。
      try {
        await stop();
      } catch (stopError) {
        console.error("[companion-main] Backend 启动失败后的进程清理失败", stopError);
      }
      const reason = error instanceof Error ? error.message : String(error);
      throw new BackendStartupError(failureCode, `NearMate Backend 启动失败：${reason}。诊断日志：${startupLogPath}`, { cause: error });
    }).finally(() => {
      if (startPromise === wrappedStart) {
        startPromise = null;
      }
    });
    startPromise = wrappedStart;
    await wrappedStart;
  }

  async function stop(): Promise<void> {
    // 先让所有尚未 spawn 的 start 失效；即使当前没有 child，也不能让旧 start
    // 在 stop 返回后偷偷创建新进程。
    lifecycleGeneration += 1;
    if (stopPromise) {
      return stopPromise;
    }

    const currentState = childState;
    const currentChild = currentState?.child ?? child;
    if (!currentChild || currentState?.terminated || currentChild.exitCode !== null) {
      if (childState === currentState) {
        child = null;
        childState = null;
      }
      return;
    }

    if (currentState) currentState.stopRequested = true;
    recordLifecycle("backend.stop_requested", currentState);
    stopPromise = (async () => {
      let taskkillError: Error | null = null;
      // Windows 下需要结束整个 Python 进程树，避免 Assistant 或媒体线程脱离 Desktop 残留。
      if (platform === "win32" && currentChild.pid) {
        await new Promise<void>((resolve, reject) => {
          const killer = spawnProcess("taskkill", ["/pid", String(currentChild.pid), "/T", "/F"], {
            stdio: "ignore",
            windowsHide: true
          });
          killer.once("exit", (code, signal) => {
            if (code === 0) {
              resolve();
              return;
            }
            reject(new Error(`taskkill 终止 Backend 失败：退出码 ${String(code)}，信号 ${String(signal)}`));
          });
          killer.once("error", error => reject(error));
        }).catch(error => {
          taskkillError = error instanceof Error ? error : new Error(String(error));
        });
        if (taskkillError) {
          // taskkill 自身失败时立即报告可操作错误，同时保留 childState 让后续 stop 重试或继续观察。
          throw taskkillError;
        }
      } else {
        try {
          currentChild.kill("SIGTERM");
        } catch (error) {
          // kill 失败时仍继续观察真实退出；Node 可能已经在退出队列中。
          console.warn("[companion-main] 请求停止 Backend 失败", error);
        }
      }

      const termination = currentState?.termination ?? observeBackendChild(currentChild).termination;
      const timeoutMs = options.stopTimeoutMs ?? DEFAULT_STOP_TIMEOUT_MS;
      const result = await awaitBackendTermination(termination, timeoutMs);
      if (result.kind === "error") {
        // error 事件代表 Node 已经无法继续管理这个 child，不能再把它当成健康运行中的进程。
        return;
      }
    })().then(
      result => {
        // 只有确认 child 已经结束，才能清除代际引用；超时或 taskkill 失败时保留引用，
        // 让后续 start/stop 继续观察同一个残留进程，而不是制造第二个 Backend。
        if (childState === currentState && (!currentState || currentState.terminated || currentChild.exitCode !== null)) {
          child = null;
          childState = null;
        }
        return result;
      },
      error => {
        if (childState === currentState && currentState?.terminated) {
          child = null;
          childState = null;
        }
        throw error;
      }
    ).finally(() => {
      stopPromise = null;
    });

    return stopPromise;
  }

  return { start, stop, forceStop };
}

/**
 * 监听 Backend child 的所有终止入口，并把 error 当成可消费的生命周期结果。
 *
 * Args:
 *   child: 刚由 spawn 返回的 Backend 子进程。
 *   instanceId: 本次启动的唯一身份；仅补充停止观察时可以省略。
 *
 * Returns:
 *   包含终止 Promise 的状态对象；Promise 永远 resolve，不会制造未处理 rejection。
 */
function observeBackendChild(child: ChildProcess, instanceId = ""): BackendChildState {
  let resolveTermination!: (termination: BackendChildTermination) => void;
  let settled = false;
  const state: BackendChildState = {
    child,
    termination: new Promise<BackendChildTermination>(resolve => {
      resolveTermination = resolve;
    }),
    terminated: false,
    ready: false,
    stopRequested: false,
    instanceId
  };

  const finish = (termination: BackendChildTermination): void => {
    if (settled) return;
    settled = true;
    state.terminated = true;
    resolveTermination(termination);
  };

  // 必须尽早注册 error 监听器。可执行文件不存在时 Node 可能只发 error，不发 exit。
  child.on("error", error => finish({ kind: "error", error }));
  child.on("exit", (code, signal) => finish({ kind: "exit", code, signal }));

  // 测试替身和极快退出的进程可能在监听器绑定前就已经有 exitCode。
  if (child.exitCode !== null) {
    queueMicrotask(() => finish({ kind: "exit", code: child.exitCode, signal: null }));
  }

  return state;
}

/**
 * 等待 Backend 真正结束，避免只等 taskkill.exe 返回就误以为 Python 已经退出。
 *
 * Args:
 *   termination: child 的终止状态 Promise。
 *   timeoutMs: 最长等待时长。
 *
 * Returns:
 *   child 的终止状态；超时则抛出明确错误。
 */
async function awaitBackendTermination(
  termination: Promise<BackendChildTermination>,
  timeoutMs: number,
): Promise<BackendChildTermination> {
  return await new Promise<BackendChildTermination>((resolve, reject) => {
    let settled = false;
    const timer = setTimeout(() => {
      if (settled) return;
      settled = true;
      reject(new Error(`Backend 停止超时（${timeoutMs}ms），进程可能仍残留`));
    }, Math.max(1, timeoutMs));

    termination.then(result => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve(result);
    });
  });
}

/**
 * 等待本次 spawn 的 Backend 真正接管控制端口，而不是把任何 200 应答误判成就绪。
 *
 * Backend 启动需要走完重量级 import 才绑定端口；如果端口已被上一次残留的实例占用，
 * 僵尸会立刻抢答健康检查，而本次 child 随后会因绑定失败退出。
 * 因此健康应答必须回显本次注入的启动身份；身份不匹配时继续观察本 child，
 * 等它退出后给出"端口被占用"的可操作错误，而不是静默挂接到僵尸实例。
 *
 * Args:
 *   fetchImpl: 可注入的 HTTP 客户端。
 *   url: Backend 控制地址。
 *   child: 需要监听异常退出的子进程。
 *   childState: child 的 error/exit 终止状态。
 *   instanceId: 本次 spawn 注入 NEARMATE_RUNTIME_INSTANCE 的一次性身份。
 *   timeoutMs: 健康检查总超时。
 *   pollIntervalMs: 两次健康检查之间的间隔。
 *   healthRequestTimeoutMs: 单次健康请求的最大等待时间。
 *
 * Returns:
 *   本尊 Backend 健康后结束；超时、进程退出或端口被既有实例占用时抛错。
 */
async function waitForBackend(
  fetchImpl: typeof fetch,
  url: URL,
  child: ChildProcess,
  childState: BackendChildState,
  instanceId: string,
  timeoutMs: number,
  pollIntervalMs: number,
  healthRequestTimeoutMs: number
): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  let foreignInstanceReplied = false;
  while (Date.now() < deadline) {
    if (childState.terminated || child.exitCode !== null) {
      const termination = await Promise.race([
        childState.termination,
        Promise.resolve<BackendChildTermination>({
          kind: "exit",
          code: child.exitCode,
          signal: null
        })
      ]);
      if (termination.kind === "error") {
        throw new BackendStartupError("spawn_failed", `Backend 进程启动失败：${termination.error.message}`);
      }
      if (foreignInstanceReplied) {
        throw new BackendStartupError("control_port_occupied", portOccupiedMessage(url, `本次启动的 Backend 已退出，退出码 ${termination.code}`));
      }
      throw new BackendStartupError("process_exited", `Backend 在健康检查前退出，退出码 ${termination.code}`);
    }
    try {
      const controller = new AbortController();
      const requestTimeoutMs = Math.max(
        1,
        Math.min(healthRequestTimeoutMs, deadline - Date.now())
      );
      const healthOperation = awaitWithTimeout(
        (async () => {
          const response = await fetchImpl(new URL("/healthz", url), {
            method: "GET",
            signal: controller.signal
          });
          const body = response.ok
            ? await response.json() as { instance_id?: unknown }
            : null;
          return { response, body };
        })(),
        requestTimeoutMs,
        () => controller.abort()
      );
      const healthResult = await Promise.race([
        healthOperation.then(
          result => ({ kind: "health" as const, result }),
          error => ({ kind: "health-error" as const, error })
        ),
        childState.termination.then(termination => ({
          kind: "terminated" as const,
          termination
        }))
      ]);
      if (healthResult.kind === "terminated") {
        controller.abort();
        if (healthResult.termination.kind === "error") {
          throw new BackendStartupError("spawn_failed", `Backend 进程启动失败：${healthResult.termination.error.message}`);
        }
        if (foreignInstanceReplied) {
          throw new BackendStartupError("control_port_occupied", portOccupiedMessage(url, `本次启动的 Backend 已退出，退出码 ${healthResult.termination.code}`));
        }
        throw new BackendStartupError("process_exited", `Backend 在健康检查前退出，退出码 ${healthResult.termination.code}`);
      }
      if (healthResult.kind === "health-error") {
        throw healthResult.error;
      }
      const { response, body } = healthResult.result;
      if (response.ok) {
        if (body?.instance_id === instanceId) {
          return;
        }
        // 有实例在应答但身份不是本次启动的：端口被既有实例占用，本次 child 稍后会自行退出。
        foreignInstanceReplied = true;
      }
    } catch {
      // child 的 error/exit 优先于网络轮询结果，避免可执行文件不存在时空转到总超时。
      if (childState.terminated) {
        const termination = await childState.termination;
        if (termination.kind === "error") {
          throw new BackendStartupError("spawn_failed", `Backend 进程启动失败：${termination.error.message}`);
        }
        if (foreignInstanceReplied) {
          throw new BackendStartupError("control_port_occupied", portOccupiedMessage(url, `本次启动的 Backend 已退出，退出码 ${termination.code}`));
        }
        throw new BackendStartupError("process_exited", `Backend 在健康检查前退出，退出码 ${termination.code}`);
      }
      // 请求失败通常表示模型尚未加载完，继续受总预算约束；不把底层请求细节带到用户提示。
    }
    const remainingAfterAttemptMs = deadline - Date.now();
    if (remainingAfterAttemptMs <= 0) break;
    await new Promise(resolve => setTimeout(
      resolve,
      Math.min(pollIntervalMs, remainingAfterAttemptMs)
    ));
  }
  if (foreignInstanceReplied) {
    throw new BackendStartupError("control_port_occupied", portOccupiedMessage(url, "健康检查超时"));
  }
  throw new BackendStartupError("startup_timeout", `后台启动超时：等待 ${Math.ceil(timeoutMs / 1_000)} 秒后仍未就绪。本地模型可能加载较慢，或启动遇到了错误，请重新打开 NearMate；反复出现时查看诊断日志`);
}

/**
 * 等待一个健康请求，但不允许底层 fetch 或响应体解析无限挂起。
 *
 * Args:
 *   operation: fetch 与响应体解析组成的完整异步操作。
 *   timeoutMs: 本次操作允许的最长时间。
 *   onTimeout: 超时前触发的取消动作。
 *
 * Returns:
 *   操作在时限内完成时返回其结果；超时则抛出明确错误。
 */
async function awaitWithTimeout<T>(
  operation: Promise<T>,
  timeoutMs: number,
  onTimeout: () => void,
): Promise<T> {
  return await new Promise<T>((resolve, reject) => {
    let settled = false;
    const timer = setTimeout(() => {
      if (settled) return;
      settled = true;
      onTimeout();
      reject(new Error(`Backend 健康请求超时（${timeoutMs}ms）`));
    }, timeoutMs);

    operation.then(
      result => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        resolve(result);
      },
      error => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        reject(error);
      }
    );
  });
}

/**
 * 拼装端口占用错误，明确告诉用户该怎么恢复。
 *
 * Args:
 *   url: Backend 控制地址，用于带出被占用的端口。
 *   detail: 触发判定的现场细节。
 *
 * Returns:
 *   含可操作指引的错误文案。
 */
function portOccupiedMessage(url: URL, detail: string): string {
  return `控制端口 ${url.port} 已被另一个正在运行的 NearMate Backend 实例占用（${detail}）。请关闭所有 NearMate 进程后重试`;
}
