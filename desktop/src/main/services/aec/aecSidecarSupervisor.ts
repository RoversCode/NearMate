import { spawn as spawnChildProcess } from "node:child_process";
import { buildAecSidecarSpawnArgs, buildAecSidecarSpawnEnv } from "./aecSidecarCommand";
import {
  attachSidecarStderrDiagnostics,
  emitDiagnostic,
  emitSidecarOutputDiagnostic,
  readSanitizedErrorMessage
} from "./aecSidecarDiagnostics";
import { stopChildForReplacement, type SidecarChild } from "./aecSidecarProcess";
import { isReadyEventForSession } from "./aecSidecarReady";
import {
  DATA_DISCONTINUITY_RESTART_COOLDOWN_MS,
  UNEXPECTED_EXIT_STABLE_RUNTIME_MS,
  getUnexpectedExitRestartDelay,
  restartCurrentSidecarAfterDataDiscontinuity
} from "./aecSidecarRestart";
import type {
  AecSidecarDiagnostic,
  AecSidecarSession,
  AecSidecarSupervisor,
  CreateAecSidecarSupervisorConfig
} from "./aecSidecarTypes";

/**
 * createAecSidecarSupervisor 创建桌面主进程使用的 AEC sidecar supervisor。
 *
 * @param config Python 路径、仓库根目录、父进程 PID 与可选测试替身。
 * @returns 可 start/stop 的 sidecar 生命周期管理器。
 */
export function createAecSidecarSupervisor(
  config: CreateAecSidecarSupervisorConfig
): AecSidecarSupervisor {
  const replacementExitGraceMs = 1000;
  const readyTimeoutMs = config.readyTimeoutMs ?? 30_000;
  const stopTimeoutMs = config.stopTimeoutMs ?? 8_000;
  const spawnImplementation = config.spawn ?? spawnChildProcess;

  let currentChild: SidecarChild | null = null;
  let lifecycleGeneration = 0;
  let latestSession: AecSidecarSession | null = null;
  let lastDataDiscontinuityRestartAt = Number.NEGATIVE_INFINITY;
  let unexpectedExitRestartTimer: ReturnType<typeof setTimeout> | null = null;
  let unexpectedExitRestartAttempt = 0;
  const controlledRestartChildren = new WeakSet<SidecarChild>();
  const ownedChildren = new Set<SidecarChild>();
  let stopAndWaitPromise: Promise<void> | null = null;
  let stopAndWaitActive = false;
  let startBlockedByStopFailure: Error | null = null;

  const clearStartBlockWhenChildrenDrain = (): void => {
    if (startBlockedByStopFailure && ownedChildren.size === 0) {
      startBlockedByStopFailure = null;
    }
  };

  const cancelUnexpectedExitRestart = (): void => {
    if (unexpectedExitRestartTimer === null) {
      return;
    }

    clearTimeout(unexpectedExitRestartTimer);
    unexpectedExitRestartTimer = null;
  };

  const scheduleUnexpectedExitRestart = (
    session: AecSidecarSession,
    failedGeneration: number
  ): void => {
    if (
      unexpectedExitRestartTimer !== null ||
      lifecycleGeneration !== failedGeneration ||
      latestSession !== session ||
      currentChild !== null
    ) {
      return;
    }

    const restartDelayMs = getUnexpectedExitRestartDelay(unexpectedExitRestartAttempt);
    unexpectedExitRestartAttempt += 1;

    emitDiagnostic(config.onDiagnostic, {
      event: "aec-sidecar-warning",
      phase: "runtime",
      kind: "sidecar_restart_scheduled",
      message: `AEC sidecar exited unexpectedly; restarting in ${restartDelayMs} ms.`
    });

    unexpectedExitRestartTimer = setTimeout(() => {
      unexpectedExitRestartTimer = null;

      if (
        lifecycleGeneration !== failedGeneration ||
        latestSession !== session ||
        currentChild !== null
      ) {
        return;
      }

      void startSidecarInternal(session, true).catch(error => {
        if (
          lifecycleGeneration !== failedGeneration + 1 ||
          latestSession !== session
        ) {
          return;
        }

        emitDiagnostic(config.onDiagnostic, {
          event: "aec-sidecar-error",
          phase: "runtime",
          message: readSanitizedErrorMessage(error, session.token)
        });

        /**
         * 自动启动可能在 ready 前失败。只要期间没有新的 session 或主动 stop，
         * 就继续沿用当前代际重试；等待时间会逐次增加，避免错误配置造成进程风暴。
         */
        if (
          lifecycleGeneration === failedGeneration + 1 &&
          latestSession === session &&
          currentChild === null
        ) {
          scheduleUnexpectedExitRestart(session, lifecycleGeneration);
        }
      });
    }, restartDelayMs);
  };

  const startSidecar = async (session: AecSidecarSession): Promise<void> => {
    // before-quit 的 stopAndWait 仍在等待旧 child 时，禁止新 session 抢占音频设备。
    // 等待失败也要原样阻断启动，避免把“可能残留”的旧进程隐藏在新代际后面。
    if (stopAndWaitPromise) {
      await stopAndWaitPromise;
    }
    // stopAndWait 超时后仍可能有旧进程残留；在它真正退出前禁止任何自动或手动新启动。
    if (startBlockedByStopFailure) {
      throw new Error("AEC sidecar 上一轮停止未完成，旧进程退出前拒绝启动下一代", {
        cause: startBlockedByStopFailure
      });
    }
    if (!currentChild && ownedChildren.size > 0) {
      throw new Error("AEC sidecar 仍有旧进程未退出，拒绝启动下一代");
    }
    await startSidecarInternal(session, false);
  };

  async function startSidecarInternal(
    session: AecSidecarSession,
    preserveUnexpectedExitBackoff: boolean
  ): Promise<void> {
    cancelUnexpectedExitRestart();
    if (!preserveUnexpectedExitBackoff) {
      unexpectedExitRestartAttempt = 0;
    }

    latestSession = session;
    lifecycleGeneration += 1;
    const startedGeneration = lifecycleGeneration;

    /**
     * 新 session 到来时先停止旧 sidecar，避免多个 Python 进程同时占用音频设备与 LiveKit 身份。
     */
    if (currentChild) {
      const childToReplace = currentChild;
      await stopChildForReplacement(childToReplace, replacementExitGraceMs);
      if (currentChild === childToReplace) {
        currentChild = null;
      }
    }

    if (lifecycleGeneration !== startedGeneration || latestSession !== session) {
      throw new Error("AEC sidecar start was superseded.");
    }

    try {
      currentChild = spawnImplementation(
        config.pythonExe,
        buildAecSidecarSpawnArgs(session, config.parentPid, config.packaged),
        {
          cwd: config.repoRoot,
          env: config.environment ?? buildAecSidecarSpawnEnv(config.repoRoot, process.env),
          stdio: ["ignore", "pipe", "pipe"],
          windowsHide: true
        }
      );
    } catch (error) {
      emitDiagnostic(config.onDiagnostic, {
        event: "aec-sidecar-error",
        phase: preserveUnexpectedExitBackoff ? "runtime" : "start",
        message: readSanitizedErrorMessage(error, session.token)
      });
      currentChild = null;
      throw new Error("AEC sidecar failed to start.");
    }

    const startedChild = currentChild;
    ownedChildren.add(startedChild);
    const releaseOwnedChildAfterExit = (): void => {
      ownedChildren.delete(startedChild);
      clearStartBlockWhenChildrenDrain();
    };
    const releaseOwnedChildAfterError = (): void => {
      if (stopAndWaitActive) {
        // stopAndWait 仍在等真实 exit；error 不能让 ownedChildren 提前丢失，
        // 否则超时后会错误放行下一代启动。
        return;
      }
      // error 可能只是 kill 失败；等 ready/exit 监听器先更新 currentChild，
      // 只有确认当前代际已经失效时才从 ownedChildren 移除，避免 stopAndWait 漏掉残留进程。
      queueMicrotask(() => {
        if (currentChild === startedChild) return;
        ownedChildren.delete(startedChild);
        clearStartBlockWhenChildrenDrain();
      });
    };
    startedChild.on("exit", releaseOwnedChildAfterExit);
    startedChild.on("error", releaseOwnedChildAfterError);
    const startedAt = Date.now();
    let hasPublishedReady = false;

    const waitForReadyCheckpoint = createReadyCheckpoint({
      child: startedChild,
      session,
      isCurrentChild: () => currentChild === startedChild,
      markCurrentChildUnavailable: () => {
        currentChild = null;
      },
      hasPublishedReady: () => hasPublishedReady,
      markPublishedReady: () => {
        hasPublishedReady = true;
      },
      readyTimeoutMs,
      onDiagnostic: config.onDiagnostic
    });

    /**
     * exit 是 sidecar 生命周期的退出收口点。
     * 只有当前 child 退出时才清空引用；ready 后的非主动退出需要恢复同一 session。
     */
    startedChild.on("exit", (code, signal) => {
      emitDiagnostic(config.onDiagnostic, {
        event: "aec-sidecar-exit",
        code,
        signal
      });

      if (currentChild !== startedChild) {
        return;
      }

      currentChild = null;

      if (
        !hasPublishedReady ||
        lifecycleGeneration !== startedGeneration ||
        controlledRestartChildren.has(startedChild)
      ) {
        return;
      }

      if (Date.now() - startedAt >= UNEXPECTED_EXIT_STABLE_RUNTIME_MS) {
        unexpectedExitRestartAttempt = 0;
      }

      scheduleUnexpectedExitRestart(session, startedGeneration);
    });

    attachSidecarStderrDiagnostics(
      startedChild,
      session.token,
      () => hasPublishedReady,
      config.onDiagnostic,
      () => {
        if (currentChild !== startedChild) {
          return;
        }

        const restartSession = latestSession;
        const now = Date.now();
        if (
          restartSession === null ||
          now - lastDataDiscontinuityRestartAt < DATA_DISCONTINUITY_RESTART_COOLDOWN_MS
        ) {
          return;
        }

        lastDataDiscontinuityRestartAt = now;
        emitDiagnostic(config.onDiagnostic, {
          event: "aec-sidecar-warning",
          phase: "runtime",
          kind: "data_discontinuity",
          message: "Soundcard recording data discontinuity exceeded 100 times in 30 seconds; restarting AEC sidecar."
        });

        controlledRestartChildren.add(startedChild);
        void restartCurrentSidecarAfterDataDiscontinuity(
          startedChild,
          restartSession,
          replacementExitGraceMs,
          () => lifecycleGeneration === startedGeneration && latestSession === restartSession,
          startSidecar
        ).catch(error => {
          emitDiagnostic(config.onDiagnostic, {
            event: "aec-sidecar-error",
            phase: "runtime",
            message: readSanitizedErrorMessage(error, restartSession.token)
          });
        }).finally(() => {
          controlledRestartChildren.delete(startedChild);
        });
      }
    );

    await waitForReadyCheckpoint;

    // ready 事件只证明 child 在某一瞬间可用；等待期间可能已经被新 session
    // 或显式 stop 取代。必须在返回调用方前再次核对完整代际，避免旧 start
    // 把已失效的音频会话当成成功交付。
    if (
      lifecycleGeneration !== startedGeneration ||
      latestSession !== session ||
      currentChild !== startedChild
    ) {
      throw new Error("AEC sidecar start was superseded.");
    }
  }

  const stopSidecar = (): void => {
    cancelUnexpectedExitRestart();
    latestSession = null;
    lifecycleGeneration += 1;
    unexpectedExitRestartAttempt = 0;

    const childToStop = currentChild;
    currentChild = null;
    if (!childToStop) {
      return;
    }

    /**
     * 先清空引用再 kill，避免同步 exit 把旧 child 重新误认为当前进程。
     */
    try {
      childToStop.kill();
    } catch (error) {
      emitDiagnostic(config.onDiagnostic, {
        event: "aec-sidecar-error",
        phase: "runtime",
        message: readSanitizedErrorMessage(error, "")
      });
    }
  };

  /**
   * forceStop 立即终止 supervisor 已经登记的全部 sidecar。
   *
   * stopAndWait 负责有界等待真实 exit，但退出总预算耗尽时不能继续依赖它的
   * Promise。这里再次清空可重启状态并向当前及替换中的旧 child 发出 kill，之后
   * 仍由 exit 监听器从 ownedChildren 中移除，避免把尚未退出的进程误当成已收口。
   *
   * Returns:
   *   无；单个 child 的 kill 失败只记录诊断，不能阻塞 Desktop 继续退出。
   */
  const forceStop = (): void => {
    cancelUnexpectedExitRestart();
    latestSession = null;
    lifecycleGeneration += 1;
    unexpectedExitRestartAttempt = 0;

    const childrenToStop = [...ownedChildren];
    currentChild = null;
    for (const childToStop of childrenToStop) {
      try {
        childToStop.kill();
      } catch (error) {
        emitDiagnostic(config.onDiagnostic, {
          event: "aec-sidecar-error",
          phase: "runtime",
          message: readSanitizedErrorMessage(error, "")
        });
      }
    }
  };

  /**
   * 停止并等待所有由 supervisor 持有的 sidecar 真正退出。
   *
   * Returns:
   *   所有 child 都发出 exit/error 时 resolve；超时或 kill 失败时 reject。
   */
  const stopAndWait = (): Promise<void> => {
    if (stopAndWaitPromise) {
      return stopAndWaitPromise;
    }

    const childrenToStop = [...ownedChildren];
    const currentChildBeforeStop = currentChild;
    stopAndWaitActive = true;
    stopAndWaitPromise = (async () => {
      // 监听必须先于 kill 注册，否则真实进程可能同步发出 exit，等待方会永远错过事件。
      const waits = childrenToStop.map(child => waitForSidecarChildTermination(child, stopTimeoutMs));

      // 先让 supervisor 进入 stopped 代际，防止迟到 exit 事件再次安排自动重启。
      stopSidecar();

      for (const child of childrenToStop) {
        // stopSidecar 已经 kill 当前 child，避免重复 kill 触发某些 Windows fake/runtime 的异常。
        if (child === currentChildBeforeStop) {
          continue;
        }
        try {
          child.kill();
        } catch (error) {
          emitDiagnostic(config.onDiagnostic, {
            event: "aec-sidecar-error",
            phase: "runtime",
            message: readSanitizedErrorMessage(error, "")
          });
        }
      }

      const results = await Promise.allSettled(waits);
      const failure = results.find(
        (result): result is PromiseRejectedResult => result.status === "rejected"
      );
      if (failure) {
        startBlockedByStopFailure = failure.reason instanceof Error
          ? failure.reason
          : new Error(String(failure.reason));
        clearStartBlockWhenChildrenDrain();
        throw failure.reason;
      }
    })().finally(() => {
      stopAndWaitActive = false;
      stopAndWaitPromise = null;
    });

    return stopAndWaitPromise;
  };

  return {
    start: startSidecar,
    stop: stopSidecar,
    forceStop,
    stopAndWait
  };
}

/**
 * 等待单个 sidecar 的真实 exit；error 只说明停止请求或句柄操作失败，不能证明进程已经消失。
 *
 * Args:
 *   child: 要等待的 sidecar 子进程。
 *   timeoutMs: 最长等待时间。
 *
 * Returns:
 *   child 真实退出时 resolve；超时则 reject。
 */
function waitForSidecarChildTermination(
  child: SidecarChild,
  timeoutMs: number
): Promise<void> {
  return new Promise<void>((resolve, reject) => {
    let settled = false;
    const timer = setTimeout(() => {
      if (settled) return;
      settled = true;
      reject(new Error(`AEC sidecar 未能退出（${timeoutMs}ms），可能仍残留`));
    }, Math.max(1, timeoutMs));

    const finish = (): void => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve();
    };

    child.on("exit", finish);
    // 必须消费 error，避免 Node EventEmitter 把它升级成未处理异常；但不能据此放行下一代。
    child.on("error", () => undefined);
  });
}

/**
 * createReadyCheckpoint 等待 sidecar 完成真实房间连接、设备管线启动和 clean-mic 发布。
 *
 * @param input 当前 child、session 与 supervisor 状态读写器。
 * @returns sidecar ready 后 resolve，启动失败时 reject。
 */
function createReadyCheckpoint(input: {
  child: SidecarChild;
  session: AecSidecarSession;
  isCurrentChild: () => boolean;
  markCurrentChildUnavailable: () => void;
  hasPublishedReady: () => boolean;
  markPublishedReady: () => void;
  readyTimeoutMs: number;
  onDiagnostic: ((diagnostic: AecSidecarDiagnostic) => void) | undefined;
}): Promise<void> {
  const {
    child,
    session,
    isCurrentChild,
    markCurrentChildUnavailable,
    hasPublishedReady,
    markPublishedReady,
    readyTimeoutMs,
    onDiagnostic
  } = input;

  return new Promise<void>((resolve, reject) => {
    let hasSettledReadyCheckpoint = false;
    let stdoutBuffer = "";
    let timeoutHandle: ReturnType<typeof setTimeout> | null = null;

    const settle = (callback: () => void): void => {
      if (hasSettledReadyCheckpoint) return;
      hasSettledReadyCheckpoint = true;
      if (timeoutHandle !== null) {
        clearTimeout(timeoutHandle);
        timeoutHandle = null;
      }
      callback();
    };

    child.on("spawn", () => {
      /**
       * spawn 只代表系统创建了 Python 进程。
       * 这里不 resolve start，必须继续等待 sidecar 自己输出 ready 事件。
       */
    });

    child.on("error", error => {
      const phase = hasPublishedReady() ? "runtime" : "start";
      emitDiagnostic(onDiagnostic, {
        event: "aec-sidecar-error",
        phase,
        message: readSanitizedErrorMessage(error, session.token)
      });

      if (!hasPublishedReady() && isCurrentChild()) {
        markCurrentChildUnavailable();
      }

      settle(() => reject(new Error("AEC sidecar failed to start.")));
    });

    child.on("exit", () => {
      if (hasSettledReadyCheckpoint || hasPublishedReady()) {
        return;
      }

      if (isCurrentChild()) {
        markCurrentChildUnavailable();
      }

      settle(() => reject(new Error("AEC sidecar exited before ready.")));
    });

    if (!child.stdout) {
      if (isCurrentChild()) {
        markCurrentChildUnavailable();
      }

      settle(() => reject(new Error("AEC sidecar stdout is unavailable.")));
      return;
    }

    timeoutHandle = setTimeout(() => {
      if (hasSettledReadyCheckpoint) return;

      // 先把状态标记为 settled，再 kill，避免 fake child 同步发 exit 时重复 reject。
      settle(() => {
        if (isCurrentChild()) {
          markCurrentChildUnavailable();
        }
        try {
          child.kill();
        } catch {
          // kill 失败不改变 ready 超时的主错误，调用方仍需把 sidecar 视为不可用。
        }
        emitDiagnostic(onDiagnostic, {
          event: "aec-sidecar-error",
          phase: "start",
          message: `AEC sidecar ready 超时（${readyTimeoutMs}ms）。`
        });
        reject(new Error("AEC sidecar ready timeout."));
      });
    }, Math.max(1, readyTimeoutMs));

    child.stdout.on("data", chunk => {
      stdoutBuffer += chunk.toString();
      const lines = stdoutBuffer.split(/\r?\n/);
      stdoutBuffer = lines.pop() ?? "";

      for (const line of lines) {
        if (isReadyEventForSession(line, session.identity)) {
          if (hasSettledReadyCheckpoint) continue;

          settle(() => {
            markPublishedReady();
            resolve();
          });
          return;
        }

        if (hasPublishedReady()) {
          emitSidecarOutputDiagnostic(
            line,
            session.token,
            "runtime",
            onDiagnostic
          );
        }
      }
    });
  });
}
