import { EventEmitter } from "node:events";
import { delimiter, join } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  createAecSidecarSupervisor
} from "./aecSidecarSupervisor";
import type { AecSidecarSession, AecSidecarSupervisor } from "./aecSidecarTypes";

/**
 * FakeSidecarChild 用极小事件能力模拟 Node 子进程。
 * 核心入参由测试直接构造；预期输出是一个可触发 exit 事件、可观测 kill 调用的假进程。
 * 边界：stdout 使用 EventEmitter 模拟机器可解析 ready 事件，不模拟真实流背压或编码细节。
 */
class FakeSidecarChild extends EventEmitter {
  /**
   * stdout 模拟 Python sidecar 的标准输出流。
   * supervisor 必须从这里读取 ready JSON，避免把 Node spawn 成功误判为 clean 麦克风已经可用。
   */
  readonly stdout = new EventEmitter();

  /**
   * stderr 模拟 Python sidecar 的错误输出流。
   * supervisor 需要把运行期 traceback 或设备错误转成脱敏诊断，否则子进程退出时只能看到 code/signal。
   */
  readonly stderr = new EventEmitter();

  /**
   * kill 记录 supervisor 是否主动终止旧进程。
   * 它不自动触发 exit，避免测试把“调用 kill”和“子进程真实退出”两个阶段混在一起。
   */
  readonly kill = vi.fn(() => true);
}

/**
 * startAndConfirmSpawn 模拟 Node 子进程已经通过 spawn 安全检查。
 * 核心入参是 supervisor、假 child 与可选 session；预期输出是在 start Promise resolve 后继续测试。
 * 边界：必须先拿到 start Promise 再 emit spawn，才能验证 supervisor 确实等待了异步 spawn/error checkpoint。
 */
async function startAndConfirmSpawn(
  supervisor: AecSidecarSupervisor,
  child: FakeSidecarChild,
  session = createSessionFixture()
): Promise<void> {
  const startPromise = supervisor.start(session);
  child.emit("spawn");
  child.stdout.emit("data", Buffer.from(
    JSON.stringify({
      event: "aec-sidecar-ready",
      identity: session.identity,
      track: "clean-mic"
    }) + "\n"
  ));
  await startPromise;
}

/**
 * waitForNextTask 等待一次事件循环任务。
 * 核心入参为空；预期输出是让 Promise.race、exit listener 和后续 spawn 注册有机会全部收敛。
 * 边界异常：不使用假定调用次数的微任务等待，避免 Node 调度细节让替换测试偶发丢 ready 事件。
 */
async function waitForNextTask(): Promise<void> {
  await new Promise<void>(resolve => {
    setTimeout(resolve, 0);
  });
}

/**
 * createSessionFixture 生成启动 sidecar 所需的 LiveKit 会话视图。
 * 核心入参为空；预期输出包含 UI 不应接触的 AEC token 与 identity。
 * 边界：测试会用固定 token 断言诊断日志不会把敏感令牌打印出来。
 */
function createSessionFixture(): AecSidecarSession {
  return {
    livekitUrl: "wss://livekit.example.test",
    roomName: "desktop-room",
    identity: "desktop-aec-identity",
    token: "desktop-aec-token",
    debugAudio: {
      enabled: false,
      outputDir: "G:\\repo\\backend\\data\\debug-audio\\aec-sidecar",
      maxRuns: 10
    },
    audioDeviceSelection: {
      mode: "system_default" as const,
      microphoneId: null,
      microphoneName: null,
      speakerLoopbackId: null,
      speakerLoopbackName: null
    }
  };
}

describe("AEC sidecar supervisor", () => {
  beforeEach(() => {
    vi.spyOn(console, "info").mockImplementation(() => undefined);
  });

  afterEach(() => {
    vi.restoreAllMocks();
    vi.useRealTimers();
  });

  it("旧 session 在被新 session 取代后，即使迟到 ready 也不能把旧 start 判定为成功", async () => {
    const firstChild = new FakeSidecarChild();
    const secondChild = new FakeSidecarChild();
    const spawn = vi.fn()
      .mockReturnValueOnce(firstChild)
      .mockReturnValueOnce(secondChild);
    const supervisor = createAecSidecarSupervisor({
      pythonExe: "python",
      repoRoot: "G:\\repo",
      parentPid: 4242,
      spawn
    });
    const firstSession = createSessionFixture();
    const secondSession = {
      ...firstSession,
      roomName: "next-room",
      identity: "desktop-aec-next"
    };

    const firstStart = supervisor.start(firstSession).then(
      () => ({ status: "resolved" as const }),
      error => ({ status: "rejected" as const, error })
    );
    firstChild.emit("spawn");
    const secondStart = supervisor.start(secondSession).then(
      () => ({ status: "resolved" as const }),
      error => ({ status: "rejected" as const, error })
    );

    // A 的 ready 到达时，B 已经取得代际；旧 Promise 必须进入 superseded，而不是 resolve。
    firstChild.stdout.emit("data", Buffer.from(
      JSON.stringify({
        event: "aec-sidecar-ready",
        identity: firstSession.identity,
        track: "clean-mic"
      }) + "\n"
    ));
    await Promise.resolve();

    firstChild.emit("exit", 0, null);
    await waitForNextTask();
    secondChild.emit("spawn");
    secondChild.stdout.emit("data", Buffer.from(
      JSON.stringify({
        event: "aec-sidecar-ready",
        identity: secondSession.identity,
        track: "clean-mic"
      }) + "\n"
    ));

    const firstResult = await firstStart;
    const secondResult = await secondStart;
    expect(firstResult.status).toBe("rejected");
    expect(firstResult.status === "rejected" ? String(firstResult.error) : "").toContain("superseded");
    expect(secondResult).toEqual({ status: "resolved" });
    supervisor.stop();
  });

  it("start 会等到 sidecar 输出 ready 事件后才向调用方报告可用", async () => {
    const child = new FakeSidecarChild();
    const spawn = vi.fn(() => child);
    const supervisor = createAecSidecarSupervisor({
      pythonExe: "python",
      repoRoot: "G:\\repo",
      parentPid: 4242,
      spawn
    });
    let startResolved = false;

    const startPromise = supervisor.start(createSessionFixture()).then(() => {
      startResolved = true;
    });

    /**
     * Node spawn 只代表进程被创建，不代表 LiveKit 房间已连接、设备已打开、clean-mic 已发布。
     * 这里先触发 spawn，确认 start 仍保持 pending，避免 renderer 太早拿到可连接 session。
     */
    child.emit("spawn");
    await Promise.resolve();

    expect(startResolved).toBe(false);

    child.stdout.emit("data", Buffer.from(
      JSON.stringify({
        event: "aec-sidecar-ready",
        identity: "desktop-aec-identity",
        track: "clean-mic"
      }) + "\n"
    ));
    await startPromise;

    expect(startResolved).toBe(true);
  });

  it("start 会忽略 identity 不匹配的 ready 事件，避免旧 sidecar 抢先放行新会话", async () => {
    const child = new FakeSidecarChild();
    const spawn = vi.fn(() => child);
    const supervisor = createAecSidecarSupervisor({
      pythonExe: "python",
      repoRoot: "G:\\repo",
      parentPid: 4242,
      spawn
    });
    let startResolved = false;

    const startPromise = supervisor.start(createSessionFixture()).then(() => {
      startResolved = true;
    });
    child.emit("spawn");

    /**
     * ready 事件必须带本次 session 的 identity。
     * 业务意图是让 supervisor 校验后端显式签发的 AEC 身份，而不是相信任意 sidecar 输出。
     */
    child.stdout.emit("data", Buffer.from(
      JSON.stringify({
        event: "aec-sidecar-ready",
        identity: "other-aec-identity",
        track: "clean-mic"
      }) + "\n"
    ));
    await Promise.resolve();

    expect(startResolved).toBe(false);

    child.stdout.emit("data", Buffer.from(
      JSON.stringify({
        event: "aec-sidecar-ready",
        identity: "desktop-aec-identity",
        track: "clean-mic"
      }) + "\n"
    ));
    await startPromise;

    expect(startResolved).toBe(true);
  });

  it("旧 child 替换时会先等待退出，再启动新 sidecar，降低音频设备抢占风险", async () => {
    const firstChild = new FakeSidecarChild();
    const secondChild = new FakeSidecarChild();
    const spawn = vi.fn()
      .mockReturnValueOnce(firstChild)
      .mockReturnValueOnce(secondChild);
    const supervisor = createAecSidecarSupervisor({
      pythonExe: "python",
      repoRoot: "G:\\repo",
      parentPid: 4242,
      spawn
    });

    await startAndConfirmSpawn(supervisor, firstChild);
    const secondStartPromise = supervisor.start({
      ...createSessionFixture(),
      roomName: "next-room",
      identity: "desktop-aec-next"
    });

    /**
     * supervisor 已经要求旧 child 退出，但在 exit 到达前不能立即 spawn 第二个进程。
     * 这样可以给旧 Python runtime 释放麦克风、loopback 和 LiveKit track 的机会。
     */
    expect(firstChild.kill).toHaveBeenCalledOnce();
    expect(spawn).toHaveBeenCalledTimes(1);

    firstChild.emit("exit", 0, null);
    await waitForNextTask();
    secondChild.emit("spawn");
    secondChild.stdout.emit("data", Buffer.from(
      JSON.stringify({
        event: "aec-sidecar-ready",
        identity: "desktop-aec-next",
        track: "clean-mic"
      }) + "\n"
    ));
    await secondStartPromise;

    expect(spawn).toHaveBeenCalledTimes(2);
  });

  it("旧 child 只发出 error 没有 exit 时，必须阻断替换并等待真实退出", async () => {
    const firstChild = new FakeSidecarChild();
    const secondChild = new FakeSidecarChild();
    const spawn = vi.fn()
      .mockReturnValueOnce(firstChild)
      .mockReturnValueOnce(secondChild);
    const supervisor = createAecSidecarSupervisor({
      pythonExe: "python",
      repoRoot: "G:\\repo",
      parentPid: 4242,
      spawn
    });

    await startAndConfirmSpawn(supervisor, firstChild);
    const nextStart = supervisor.start({
      ...createSessionFixture(),
      roomName: "next-room"
    }).then(
      () => ({ status: "resolved" as const }),
      error => ({ status: "rejected" as const, error })
    );
    firstChild.emit("error", new Error("child handle failed"));
    await waitForNextTask();

    const result = await nextStart;
    expect(result.status).toBe("rejected");
    expect(result.status === "rejected" ? String(result.error) : "").toContain("未确认旧进程退出");
    expect(spawn).toHaveBeenCalledTimes(1);

    firstChild.emit("exit", 0, null);
    await waitForNextTask();
    const retry = supervisor.start({
      ...createSessionFixture(),
      roomName: "next-room"
    });
    secondChild.emit("spawn");
    secondChild.stdout.emit("data", Buffer.from(
      JSON.stringify({
        event: "aec-sidecar-ready",
        identity: "desktop-aec-identity",
        track: "clean-mic"
      }) + "\n"
    ));
    await retry;
    expect(spawn).toHaveBeenCalledTimes(2);
    supervisor.stop();
  });

  it("旧 child 超过替换宽限仍未退出时，拒绝启动下一代 sidecar", async () => {
    vi.useFakeTimers();
    const firstChild = new FakeSidecarChild();
    const secondChild = new FakeSidecarChild();
    const spawn = vi.fn()
      .mockReturnValueOnce(firstChild)
      .mockReturnValueOnce(secondChild);
    const supervisor = createAecSidecarSupervisor({
      pythonExe: "python",
      repoRoot: "G:\\repo",
      parentPid: 4242,
      spawn
    });

    await startAndConfirmSpawn(supervisor, firstChild);
    const nextStart = supervisor.start({
      ...createSessionFixture(),
      roomName: "next-room"
    }).then(
      () => ({ status: "resolved" as const }),
      error => ({ status: "rejected" as const, error })
    );
    await Promise.resolve();

    expect(firstChild.kill).toHaveBeenCalledOnce();
    expect(spawn).toHaveBeenCalledTimes(1);

    await vi.advanceTimersByTimeAsync(1000);
    const result = await nextStart;
    expect(result.status).toBe("rejected");
    expect(result.status === "rejected" ? String(result.error) : "").toContain("拒绝启动下一代");
    expect(spawn).toHaveBeenCalledTimes(1);

    // 旧进程随后退出也不能偷偷补启动失败的下一代；必须由上层显式重试。
    firstChild.emit("exit", 0, null);
    await Promise.resolve();
    expect(spawn).toHaveBeenCalledTimes(1);
    supervisor.stop();
  });

  it("stopAndWait 尚未确认旧 child 退出时，新的 start 不得提前 spawn", async () => {
    const firstChild = new FakeSidecarChild();
    const secondChild = new FakeSidecarChild();
    const spawn = vi.fn()
      .mockReturnValueOnce(firstChild)
      .mockReturnValueOnce(secondChild);
    const supervisor = createAecSidecarSupervisor({
      pythonExe: "python",
      repoRoot: "G:\\repo",
      parentPid: 4242,
      spawn
    });

    await startAndConfirmSpawn(supervisor, firstChild);
    const stopPromise = supervisor.stopAndWait!();
    const nextSession = {
      ...createSessionFixture(),
      roomName: "after-stop-room",
      identity: "desktop-aec-after-stop"
    };
    const nextStartPromise = supervisor.start(nextSession);

    // kill 只是发出停止请求，旧进程真正退出前麦克风和 LiveKit 身份仍可能被占用。
    // 因此 start 必须等待同一个 stopAndWait 完成，不能用新代际掩盖残留进程。
    expect(firstChild.kill).toHaveBeenCalledOnce();
    expect(spawn).toHaveBeenCalledTimes(1);

    firstChild.emit("exit", 0, null);
    await stopPromise;
    await waitForNextTask();

    expect(spawn).toHaveBeenCalledTimes(2);
    secondChild.emit("spawn");
    secondChild.stdout.emit("data", Buffer.from(
      JSON.stringify({
        event: "aec-sidecar-ready",
        identity: nextSession.identity,
        track: "clean-mic"
      }) + "\n"
    ));
    await nextStartPromise;
    supervisor.stop();
  });

  it("stopAndWait 超时后，在残留 child 退出前持续阻断下一代启动", async () => {
    vi.useFakeTimers();
    const firstChild = new FakeSidecarChild();
    const secondChild = new FakeSidecarChild();
    const spawn = vi.fn()
      .mockReturnValueOnce(firstChild)
      .mockReturnValueOnce(secondChild);
    const supervisor = createAecSidecarSupervisor({
      pythonExe: "python",
      repoRoot: "G:\\repo",
      parentPid: 4242,
      spawn,
      stopTimeoutMs: 1000
    });

    await startAndConfirmSpawn(supervisor, firstChild);
    const stopResult = supervisor.stopAndWait!().then(
      () => ({ status: "resolved" as const }),
      error => ({ status: "rejected" as const, error })
    );
    firstChild.emit("error", new Error("kill failed"));
    await vi.advanceTimersByTimeAsync(1000);
    const stopped = await stopResult;
    expect(stopped.status).toBe("rejected");

    const nextSession = {
      ...createSessionFixture(),
      roomName: "after-timeout-room",
      identity: "desktop-aec-after-timeout"
    };
    await expect(supervisor.start(nextSession)).rejects.toThrow("旧进程退出前拒绝启动下一代");
    expect(spawn).toHaveBeenCalledTimes(1);

    // 旧进程真正退出后解除阻断；下一次显式启动才允许创建新 child。
    firstChild.emit("exit", 0, null);
    await Promise.resolve();
    const retry = supervisor.start(nextSession);
    await Promise.resolve();
    secondChild.emit("spawn");
    secondChild.stdout.emit("data", Buffer.from(
      JSON.stringify({
        event: "aec-sidecar-ready",
        identity: nextSession.identity,
        track: "clean-mic"
      }) + "\n"
    ));
    await retry;
    expect(spawn).toHaveBeenCalledTimes(2);
    supervisor.stop();
  });

  it("stopAndWait 挂起时 forceStop 会再次终止仍登记的 child", async () => {
    const child = new FakeSidecarChild();
    const spawn = vi.fn(() => child);
    const supervisor = createAecSidecarSupervisor({
      pythonExe: "python",
      repoRoot: "G:\\repo",
      parentPid: 4242,
      spawn
    });

    await startAndConfirmSpawn(supervisor, child);
    const stopPromise = supervisor.stopAndWait!();
    supervisor.forceStop();

    expect(child.kill).toHaveBeenCalledTimes(2);
    child.emit("exit", 0, null);
    await stopPromise;
  });

  it("start 会在 sidecar 调试录音开启时附带稳定参数启动 Python sidecar，并注入开发态 PYTHONPATH", async () => {
    const repoRoot = "G:\\Workspace\\projects\\virtual_companion";
    const previousPythonPath = "C:\\existing\\pythonpath";
    const originalPythonPath = process.env["PYTHONPATH"];
    const child = new FakeSidecarChild();
    const spawn = vi.fn(() => child);
    const supervisor = createAecSidecarSupervisor({
      pythonExe: "G:\\Workspace\\projects\\virtual_companion\\.venv\\Scripts\\python.exe",
      repoRoot,
      parentPid: 4242,
      spawn
    });

    /**
     * 这里临时写入进程环境变量，模拟开发机已有 PYTHONPATH。
     * 测试结束后会恢复原值，避免污染同文件后续用例或其他测试文件。
     */
    process.env["PYTHONPATH"] = previousPythonPath;

    try {
      await startAndConfirmSpawn(
        supervisor,
        child,
        {
          ...createSessionFixture(),
          debugAudio: {
            enabled: true,
            outputDir: join(repoRoot, "backend", "data", "debug-audio", "aec-sidecar"),
            maxRuns: 7
          }
        }
      );

      expect(spawn).toHaveBeenCalledWith(
        "G:\\Workspace\\projects\\virtual_companion\\.venv\\Scripts\\python.exe",
        [
          "-B",
          "-X",
          "utf8",
          "-m",
          "audio_aec_sidecar.main",
          "--livekit-url",
          "wss://livekit.example.test",
          "--token",
          "desktop-aec-token",
          "--room-name",
          "desktop-room",
          "--identity",
          "desktop-aec-identity",
          "--parent-pid",
          "4242",
          "--sample-rate",
          "48000",
          "--clean-output-sample-rate",
          "16000",
          "--warmup-seconds",
          "2",
          "--audio-device-mode",
          "system_default",
          "--debug-output-dir",
          join(repoRoot, "backend", "data", "debug-audio", "aec-sidecar"),
          "--max-debug-runs",
          "7"
        ],
        expect.objectContaining({
          cwd: repoRoot,
          windowsHide: true,
          env: expect.objectContaining({
            PYTHONPATH: [
              join(repoRoot, "tools", "audio-aec-poc", "src"),
              previousPythonPath
            ].join(delimiter)
          })
        })
      );
    } finally {
      /**
       * 还原 PYTHONPATH，确保这个用例只验证 supervisor 行为，不把宿主环境变化泄漏给其他测试。
       */
      if (originalPythonPath === undefined) {
        delete process.env["PYTHONPATH"];
      } else {
        process.env["PYTHONPATH"] = originalPythonPath;
      }
    }
  });

  it("start 会在 sidecar 调试录音关闭时省略调试参数，避免持续保存连续音频", async () => {
    const child = new FakeSidecarChild();
    const spawn = vi.fn(() => child);
    const supervisor = createAecSidecarSupervisor({
      pythonExe: "python",
      repoRoot: "G:\\repo",
      parentPid: 4242,
      spawn
    });

    await startAndConfirmSpawn(
      supervisor,
      child,
      {
        ...createSessionFixture(),
        debugAudio: {
          enabled: false,
          outputDir: "G:\\repo\\backend\\data\\debug-audio\\aec-sidecar",
          maxRuns: 7
        }
      }
    );

    expect(spawn).toHaveBeenCalledWith(
      "python",
      expect.not.arrayContaining([
        "--debug-output-dir",
        "G:\\repo\\backend\\data\\debug-audio\\aec-sidecar",
        "--max-debug-runs",
        "7"
      ]),
      expect.any(Object)
    );
  });

  it("start 会把手动选择的音频设备传给 Python sidecar", async () => {
    const child = new FakeSidecarChild();
    const spawn = vi.fn(() => child);
    const supervisor = createAecSidecarSupervisor({
      pythonExe: "python",
      repoRoot: "G:\\repo",
      parentPid: 4242,
      spawn
    });

    await startAndConfirmSpawn(
      supervisor,
      child,
      {
        ...createSessionFixture(),
        audioDeviceSelection: {
          mode: "manual",
          microphoneId: "mic-usb",
          microphoneName: "麦克风 (USB Camera)",
          speakerLoopbackId: "speaker-loopback",
          speakerLoopbackName: "扬声器 (Realtek USB Audio)"
        }
      }
    );

    expect(spawn).toHaveBeenCalledWith(
      "python",
      expect.arrayContaining([
        "--audio-device-mode",
        "manual",
        "--microphone-id",
        "mic-usb",
        "--speaker-loopback-id",
        "speaker-loopback"
      ]),
      expect.any(Object)
    );
  });

  it("start 会清理上一轮 sidecar，避免同一桌面进程残留多个 AEC 参与者", async () => {
    const firstChild = new FakeSidecarChild();
    const secondChild = new FakeSidecarChild();
    const spawn = vi.fn()
      .mockReturnValueOnce(firstChild)
      .mockReturnValueOnce(secondChild);
    const supervisor = createAecSidecarSupervisor({
      pythonExe: "python",
      repoRoot: "G:\\repo",
      parentPid: 4242,
      spawn
    });

    await startAndConfirmSpawn(supervisor, firstChild);
    const secondStartPromise = supervisor.start({
      ...createSessionFixture(),
      roomName: "next-room"
    });
    firstChild.emit("exit", 0, null);
    await waitForNextTask();
    secondChild.emit("spawn");
    secondChild.stdout.emit("data", Buffer.from(
      JSON.stringify({
        event: "aec-sidecar-ready",
        identity: "desktop-aec-identity",
        track: "clean-mic"
      }) + "\n"
    ));
    await secondStartPromise;

    expect(firstChild.kill).toHaveBeenCalledOnce();
    expect(secondChild.kill).not.toHaveBeenCalled();
    expect(spawn).toHaveBeenCalledTimes(2);
  });

  it("stop 会终止当前 sidecar 并清空引用，后续重复 stop 保持幂等", async () => {
    const child = new FakeSidecarChild();
    const spawn = vi.fn(() => child);
    const supervisor = createAecSidecarSupervisor({
      pythonExe: "python",
      repoRoot: "G:\\repo",
      parentPid: 4242,
      spawn
    });

    await startAndConfirmSpawn(supervisor, child);
    supervisor.stop();
    supervisor.stop();

    expect(child.kill).toHaveBeenCalledOnce();
  });

  it("stop 只发出 kill 但旧 child 尚未退出时，后续 start 不得抢先创建新进程", async () => {
    const firstChild = new FakeSidecarChild();
    const secondChild = new FakeSidecarChild();
    const spawn = vi.fn()
      .mockReturnValueOnce(firstChild)
      .mockReturnValueOnce(secondChild);
    const supervisor = createAecSidecarSupervisor({
      pythonExe: "python",
      repoRoot: "G:\\repo",
      parentPid: 4242,
      spawn
    });

    await startAndConfirmSpawn(supervisor, firstChild);
    supervisor.stop();
    await expect(supervisor.start(createSessionFixture())).rejects.toThrow("旧进程未退出");
    expect(spawn).toHaveBeenCalledTimes(1);

    firstChild.emit("exit", 0, null);
    const retry = supervisor.start(createSessionFixture());
    await Promise.resolve();
    secondChild.emit("spawn");
    secondChild.stdout.emit("data", Buffer.from(
      JSON.stringify({
        event: "aec-sidecar-ready",
        identity: "desktop-aec-identity",
        track: "clean-mic"
      }) + "\n"
    ));
    await retry;
    expect(spawn).toHaveBeenCalledTimes(2);
    supervisor.stop();
  });

  it("child exit 会输出不含 token 的诊断，并清空当前 child 引用", async () => {
    const firstChild = new FakeSidecarChild();
    const secondChild = new FakeSidecarChild();
    const spawn = vi.fn()
      .mockReturnValueOnce(firstChild)
      .mockReturnValueOnce(secondChild);
    const onDiagnostic = vi.fn();
    const supervisor = createAecSidecarSupervisor({
      pythonExe: "python",
      repoRoot: "G:\\repo",
      parentPid: 4242,
      spawn,
      onDiagnostic
    });

    await startAndConfirmSpawn(supervisor, firstChild);
    firstChild.emit("exit", 17, null);
    supervisor.stop();
    await startAndConfirmSpawn(supervisor, secondChild);

    expect(firstChild.kill).not.toHaveBeenCalled();
    expect(secondChild.kill).not.toHaveBeenCalled();
    expect(onDiagnostic).toHaveBeenCalledWith({
      event: "aec-sidecar-exit",
      code: 17,
      signal: null
    });
    expect(JSON.stringify(onDiagnostic.mock.calls)).not.toContain("desktop-aec-token");
  });

  it("ready 后 child 异常退出会用同一 session 自动重启", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(0);
    const firstChild = new FakeSidecarChild();
    const secondChild = new FakeSidecarChild();
    const spawn = vi.fn()
      .mockReturnValueOnce(firstChild)
      .mockReturnValueOnce(secondChild);
    const onDiagnostic = vi.fn();
    const supervisor = createAecSidecarSupervisor({
      pythonExe: "python",
      repoRoot: "G:\\repo",
      parentPid: 4242,
      spawn,
      onDiagnostic
    });
    const session = createSessionFixture();

    await startAndConfirmSpawn(supervisor, firstChild, session);
    firstChild.emit("exit", 17, null);

    await vi.advanceTimersByTimeAsync(999);
    expect(spawn).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(1);

    expect(spawn).toHaveBeenCalledTimes(2);
    expect(spawn.mock.calls[1]?.[1]).toEqual(spawn.mock.calls[0]?.[1]);

    secondChild.emit("spawn");
    secondChild.stdout.emit("data", Buffer.from(
      JSON.stringify({
        event: "aec-sidecar-ready",
        identity: session.identity,
        track: "clean-mic"
      }) + "\n"
    ));

    expect(onDiagnostic).toHaveBeenCalledWith({
      event: "aec-sidecar-exit",
      code: 17,
      signal: null
    });
    supervisor.stop();
  });

  it("自动重启在 ready 前失败时会保留脱敏诊断并按退避时间再次尝试", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(0);
    const firstChild = new FakeSidecarChild();
    const failedChild = new FakeSidecarChild();
    const recoveredChild = new FakeSidecarChild();
    const spawn = vi.fn()
      .mockReturnValueOnce(firstChild)
      .mockReturnValueOnce(failedChild)
      .mockReturnValueOnce(recoveredChild);
    const onDiagnostic = vi.fn();
    const supervisor = createAecSidecarSupervisor({
      pythonExe: "python",
      repoRoot: "G:\\repo",
      parentPid: 4242,
      spawn,
      onDiagnostic
    });
    const session = createSessionFixture();

    await startAndConfirmSpawn(supervisor, firstChild, session);
    firstChild.emit("exit", 17, null);
    await vi.advanceTimersByTimeAsync(1_000);
    expect(spawn).toHaveBeenCalledTimes(2);

    const unsafeRuntimeError = Object.assign(new Error(`runtime ${session.token} leaked`), {
      spawnargs: ["--token", session.token]
    });
    failedChild.emit("error", unsafeRuntimeError);
    await Promise.resolve();

    const diagnosticJson = JSON.stringify(onDiagnostic.mock.calls);
    expect(diagnosticJson).not.toContain(session.token);
    expect(diagnosticJson).not.toContain("spawnargs");
    expect(onDiagnostic).toHaveBeenCalledWith({
      event: "aec-sidecar-error",
      phase: "start",
      message: "runtime [redacted-token] leaked"
    });

    await vi.advanceTimersByTimeAsync(1_999);
    expect(spawn).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(1);
    expect(spawn).toHaveBeenCalledTimes(3);

    recoveredChild.emit("spawn");
    recoveredChild.stdout.emit("data", Buffer.from(
      JSON.stringify({
        event: "aec-sidecar-ready",
        identity: session.identity,
        track: "clean-mic"
      }) + "\n"
    ));
    await Promise.resolve();
    supervisor.stop();
  });

  it("主动 stop 会取消 ready 后异常退出的待重启任务", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(0);
    const firstChild = new FakeSidecarChild();
    const secondChild = new FakeSidecarChild();
    const spawn = vi.fn()
      .mockReturnValueOnce(firstChild)
      .mockReturnValueOnce(secondChild);
    const supervisor = createAecSidecarSupervisor({
      pythonExe: "python",
      repoRoot: "G:\\repo",
      parentPid: 4242,
      spawn
    });

    await startAndConfirmSpawn(supervisor, firstChild);
    firstChild.emit("exit", 17, null);
    supervisor.stop();
    await vi.advanceTimersByTimeAsync(60_000);

    expect(spawn).toHaveBeenCalledTimes(1);
  });

  it("start 遇到 child error 时会拒绝、清空引用，并输出不含 token 或 spawnargs 的诊断", async () => {
    const failedChild = new FakeSidecarChild();
    const nextChild = new FakeSidecarChild();
    const spawn = vi.fn()
      .mockReturnValueOnce(failedChild)
      .mockReturnValueOnce(nextChild);
    const onDiagnostic = vi.fn();
    const supervisor = createAecSidecarSupervisor({
      pythonExe: "python",
      repoRoot: "G:\\repo",
      parentPid: 4242,
      spawn,
      onDiagnostic
    });
    const unsafeSpawnError = Object.assign(new Error("spawn python ENOENT"), {
      spawnargs: [
        "-m",
        "audio_aec_sidecar.main",
        "--token",
        "desktop-aec-token"
      ]
    });

    const startPromise = supervisor.start(createSessionFixture());
    failedChild.emit("error", unsafeSpawnError);

    await expect(startPromise).rejects.toThrow("AEC sidecar failed to start.");
    supervisor.stop();
    await startAndConfirmSpawn(supervisor, nextChild);

    const diagnosticJson = JSON.stringify(onDiagnostic.mock.calls);
    expect(failedChild.kill).not.toHaveBeenCalled();
    expect(nextChild.kill).not.toHaveBeenCalled();
    expect(onDiagnostic).toHaveBeenCalledWith({
      event: "aec-sidecar-error",
      phase: "start",
      message: "spawn python ENOENT"
    });
    expect(diagnosticJson).not.toContain("desktop-aec-token");
    expect(diagnosticJson).not.toContain("spawnargs");
  });

  it("start 成功后的 child error 仍会被永久监听器消化并输出脱敏诊断", async () => {
    const child = new FakeSidecarChild();
    const spawn = vi.fn(() => child);
    const onDiagnostic = vi.fn();
    const supervisor = createAecSidecarSupervisor({
      pythonExe: "python",
      repoRoot: "G:\\repo",
      parentPid: 4242,
      spawn,
      onDiagnostic
    });
    const unsafeRuntimeError = Object.assign(new Error("runtime write failed"), {
      spawnargs: [
        "--token",
        "desktop-aec-token"
      ]
    });

    await startAndConfirmSpawn(supervisor, child);
    child.emit("error", unsafeRuntimeError);

    const diagnosticJson = JSON.stringify(onDiagnostic.mock.calls);
    expect(onDiagnostic).toHaveBeenCalledWith({
      event: "aec-sidecar-error",
      phase: "runtime",
      message: "runtime write failed"
    });
    expect(diagnosticJson).not.toContain("desktop-aec-token");
    expect(diagnosticJson).not.toContain("spawnargs");
  });

  it("start 会把 sidecar stderr 行转成脱敏诊断，帮助定位运行期断流原因", async () => {
    const child = new FakeSidecarChild();
    const spawn = vi.fn(() => child);
    const onDiagnostic = vi.fn();
    const supervisor = createAecSidecarSupervisor({
      pythonExe: "python",
      repoRoot: "G:\\repo",
      parentPid: 4242,
      spawn,
      onDiagnostic
    });

    await startAndConfirmSpawn(supervisor, child);
    child.stderr.emit("data", Buffer.from("Traceback: desktop-aec-token microphone device lost\n"));

    expect(onDiagnostic).toHaveBeenCalledWith({
      event: "aec-sidecar-error",
      phase: "runtime",
      message: "Traceback: [redacted-token] microphone device lost"
    });
    expect(JSON.stringify(onDiagnostic.mock.calls)).not.toContain("desktop-aec-token");
  });

  it("start 会把 soundcard 录音不连续 stderr 降级成运行期 warning 诊断", async () => {
    const child = new FakeSidecarChild();
    const spawn = vi.fn(() => child);
    const onDiagnostic = vi.fn();
    const supervisor = createAecSidecarSupervisor({
      pythonExe: "python",
      repoRoot: "G:\\repo",
      parentPid: 4242,
      spawn,
      onDiagnostic
    });

    await startAndConfirmSpawn(supervisor, child);
    child.stderr.emit("data", Buffer.from(
      "G:\\repo\\.venv\\Lib\\site-packages\\soundcard\\mediafoundation.py:772: SoundcardRuntimeWarning: data discontinuity in recording\n"
    ));

    expect(onDiagnostic).toHaveBeenCalledWith({
      event: "aec-sidecar-warning",
      phase: "runtime",
      kind: "data_discontinuity",
      message: "Soundcard recording data discontinuity detected."
    });
    expect(onDiagnostic).not.toHaveBeenCalledWith(expect.objectContaining({
      event: "aec-sidecar-error",
      message: expect.stringContaining("data discontinuity in recording")
    }));
  });

  it("start 会聚合同类 soundcard 录音不连续 warning，避免 stderr 风暴逐行刷屏", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(0);
    const child = new FakeSidecarChild();
    const spawn = vi.fn(() => child);
    const onDiagnostic = vi.fn();
    const supervisor = createAecSidecarSupervisor({
      pythonExe: "python",
      repoRoot: "G:\\repo",
      parentPid: 4242,
      spawn,
      onDiagnostic
    });

    await startAndConfirmSpawn(supervisor, child);
    child.stderr.emit("data", Buffer.from(
      "SoundcardRuntimeWarning: data discontinuity in recording\n"
      + "warnings.warn(\"data discontinuity in recording\", SoundcardRuntimeWarning)\n"
      + "SoundcardRuntimeWarning: data discontinuity in recording\n"
    ));

    const warningCallsBeforeSummary = onDiagnostic.mock.calls.filter(([diagnostic]) => (
      diagnostic.event === "aec-sidecar-warning"
    ));
    expect(warningCallsBeforeSummary).toHaveLength(1);

    vi.setSystemTime(10_000);
    child.stderr.emit("data", Buffer.from("SoundcardRuntimeWarning: data discontinuity in recording\n"));

    expect(onDiagnostic).toHaveBeenCalledWith({
      event: "aec-sidecar-warning",
      phase: "runtime",
      kind: "data_discontinuity",
      message: "Soundcard recording data discontinuity repeated 3 times in the last 10 seconds."
    });
  });

  it("ready 后录音不连续 warning 高频出现时会重启当前 sidecar", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(0);
    const firstChild = new FakeSidecarChild();
    const secondChild = new FakeSidecarChild();
    const spawn = vi.fn()
      .mockReturnValueOnce(firstChild)
      .mockReturnValueOnce(secondChild);
    const onDiagnostic = vi.fn();
    const supervisor = createAecSidecarSupervisor({
      pythonExe: "python",
      repoRoot: "G:\\repo",
      parentPid: 4242,
      spawn,
      onDiagnostic
    });

    await startAndConfirmSpawn(supervisor, firstChild);
    for (let index = 0; index < 100; index += 1) {
      firstChild.stderr.emit("data", Buffer.from("SoundcardRuntimeWarning: data discontinuity in recording\n"));
      vi.setSystemTime(index + 1);
    }

    expect(firstChild.kill).toHaveBeenCalledOnce();
    firstChild.emit("exit", 0, null);
    await Promise.resolve();
    await Promise.resolve();
    secondChild.emit("spawn");
    secondChild.stdout.emit("data", Buffer.from(
      JSON.stringify({
        event: "aec-sidecar-ready",
        identity: "desktop-aec-identity",
        track: "clean-mic"
      }) + "\n"
    ));
    await Promise.resolve();
    await Promise.resolve();

    expect(spawn).toHaveBeenCalledTimes(2);
    expect(spawn.mock.calls[1]?.[1]).toEqual(spawn.mock.calls[0]?.[1]);
    expect(onDiagnostic).toHaveBeenCalledWith({
      event: "aec-sidecar-warning",
      phase: "runtime",
      kind: "data_discontinuity",
      message: "Soundcard recording data discontinuity exceeded 100 times in 30 seconds; restarting AEC sidecar."
    });
  });

  it("录音不连续 warning 触发重启后会遵守冷却时间，避免无限重启", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(0);
    const firstChild = new FakeSidecarChild();
    const secondChild = new FakeSidecarChild();
    const spawn = vi.fn()
      .mockReturnValueOnce(firstChild)
      .mockReturnValueOnce(secondChild);
    const supervisor = createAecSidecarSupervisor({
      pythonExe: "python",
      repoRoot: "G:\\repo",
      parentPid: 4242,
      spawn
    });

    await startAndConfirmSpawn(supervisor, firstChild);
    for (let index = 0; index < 100; index += 1) {
      firstChild.stderr.emit("data", Buffer.from("SoundcardRuntimeWarning: data discontinuity in recording\n"));
      vi.setSystemTime(index + 1);
    }
    firstChild.emit("exit", 0, null);
    await Promise.resolve();
    await Promise.resolve();
    secondChild.emit("spawn");
    secondChild.stdout.emit("data", Buffer.from(
      JSON.stringify({
        event: "aec-sidecar-ready",
        identity: "desktop-aec-identity",
        track: "clean-mic"
      }) + "\n"
    ));
    await Promise.resolve();
    await Promise.resolve();

    vi.setSystemTime(1_000);
    for (let index = 0; index < 100; index += 1) {
      secondChild.stderr.emit("data", Buffer.from("SoundcardRuntimeWarning: data discontinuity in recording\n"));
      vi.setSystemTime(1_000 + index + 1);
    }

    expect(firstChild.kill).toHaveBeenCalledOnce();
    expect(secondChild.kill).not.toHaveBeenCalled();
    expect(spawn).toHaveBeenCalledTimes(2);
  });

  it("自动重启等待旧进程退出期间 stop 会取消后续重启", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(0);
    const firstChild = new FakeSidecarChild();
    const secondChild = new FakeSidecarChild();
    const spawn = vi.fn()
      .mockReturnValueOnce(firstChild)
      .mockReturnValueOnce(secondChild);
    const supervisor = createAecSidecarSupervisor({
      pythonExe: "python",
      repoRoot: "G:\\repo",
      parentPid: 4242,
      spawn
    });

    await startAndConfirmSpawn(supervisor, firstChild);
    for (let index = 0; index < 100; index += 1) {
      firstChild.stderr.emit("data", Buffer.from("SoundcardRuntimeWarning: data discontinuity in recording\n"));
      vi.setSystemTime(index + 1);
    }

    supervisor.stop();
    firstChild.emit("exit", 0, null);
    await Promise.resolve();
    await Promise.resolve();

    expect(firstChild.kill).toHaveBeenCalledTimes(2);
    expect(spawn).toHaveBeenCalledTimes(1);
  });

  it("start 会把 ready 后的 sidecar stdout 摘要转成运行期诊断", async () => {
    const child = new FakeSidecarChild();
    const spawn = vi.fn(() => child);
    const onDiagnostic = vi.fn();
    const supervisor = createAecSidecarSupervisor({
      pythonExe: "python",
      repoRoot: "G:\\repo",
      parentPid: 4242,
      spawn,
      onDiagnostic
    });

    await startAndConfirmSpawn(supervisor, child);
    child.stdout.emit("data", Buffer.from("Frames published: 23\nStop reason: parent_exited\n"));

    expect(onDiagnostic).toHaveBeenCalledWith({
      event: "aec-sidecar-output",
      phase: "runtime",
      message: "Frames published: 23"
    });
    expect(onDiagnostic).toHaveBeenCalledWith({
      event: "aec-sidecar-output",
      phase: "runtime",
      message: "Stop reason: parent_exited"
    });
  });
});
