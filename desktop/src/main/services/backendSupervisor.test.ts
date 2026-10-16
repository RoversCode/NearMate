import { EventEmitter } from "node:events";
import { mkdtemp, readFile, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it, vi } from "vitest";
import type { ChildProcess, spawn } from "node:child_process";
import { createBackendSupervisor, type BackendUnexpectedTermination } from "./backendSupervisor";
import type { NearMateRuntimeLayout } from "./runtimeLayout";
import { createPackagedBackendEnvironment } from "./backendEnvironment";

function createRuntimeLayout(): NearMateRuntimeLayout {
  return {
    resourcesRoot: "C:/NearMate",
    runtimeRoot: "C:/NearMate/runtime",
    manifestPath: "C:/NearMate/runtime/runtime-manifest.json",
    manifest: null,
    profile: "cuda",
    pythonExe: "C:/NearMate/runtime/python/Scripts/python.exe",
    backendCwd: "C:/NearMate/runtime/backend",
    nativeRoot: "C:/NearMate/runtime/native",
    sharedRoot: "C:/NearMate/runtime/shared",
    sidecarPackageRoot: "C:/NearMate/runtime/python/Lib/site-packages/audio_aec_sidecar",
    consoleDistDir: "C:/NearMate/runtime/console/dist",
    modelCatalogPath: "C:/NearMate/runtime/model-catalog.json",
    seedCharacterCardRoot: "C:/NearMate/runtime/backend/seed-character-cards",
    assetsRoot: "C:/NearMate/runtime/assets",
    liveKitServerExe: null
  };
}

function createExitedChild(): ChildProcess {
  const child = new EventEmitter() as ChildProcess & { exitCode: number; pid: number };
  child.exitCode = 1;
  child.pid = 1234;
  child.stdout = new EventEmitter() as ChildProcess["stdout"];
  child.stderr = new EventEmitter() as ChildProcess["stderr"];
  child.kill = vi.fn(() => true);
  return child;
}

/**
 * createRunningChild 模拟仍在运行的 Backend 子进程，测试可手动改写 exitCode 模拟退出。
 */
function createRunningChild(): ChildProcess & { exitCode: number | null } {
  const child = new EventEmitter() as ChildProcess & { exitCode: number | null; pid: number };
  child.exitCode = null;
  child.pid = 4321;
  child.stdout = new EventEmitter() as ChildProcess["stdout"];
  child.stderr = new EventEmitter() as ChildProcess["stderr"];
  child.kill = vi.fn(() => true);
  return child;
}

function healthResponse(instanceId: string): Response {
  return new Response(JSON.stringify({ status: "ok", instance_id: instanceId }), {
    status: 200,
    headers: { "content-type": "application/json" }
  });
}

type SupervisorHarness = {
  supervisor: ReturnType<typeof createBackendSupervisor>;
  child: ChildProcess & { exitCode: number | null };
  spawnSpy: ReturnType<typeof vi.fn>;
  fetchSpy: ReturnType<typeof vi.fn>;
  userDataDir: string;
  /** 本次 spawn 注入的一次性运行身份，从 spawn env 观察口读出。 */
  spawnedInstanceId(): string;
};

type SpawnCallWithEnvironment = [
  command: string,
  args: unknown,
  options: { env: Record<string, string> }
];

function getSpawnOptions(spawnSpy: ReturnType<typeof vi.fn>, index: number): SpawnCallWithEnvironment[2] {
  const call = (spawnSpy.mock.calls as unknown as SpawnCallWithEnvironment[])[index];
  if (!call?.[2]) {
    throw new Error("测试未观察到带环境变量的 Backend spawn 调用");
  }
  return call[2];
}

/**
 * createSupervisorHarness 以固定的启动参数搭建 supervisor，并暴露 spawn 与 fetch 观察口。
 * 健康应答应回显什么身份由每个用例自行决定：本尊用 spawnedInstanceId()，僵尸用固定串。
 */
async function createSupervisorHarness(input: {
  respond: (harness: SupervisorHarness) => Promise<Response>;
  startupTimeoutMs?: number;
  pollIntervalMs?: number;
  healthRequestTimeoutMs?: number;
  onUnexpectedTermination?: (termination: BackendUnexpectedTermination) => void | Promise<void>;
}): Promise<SupervisorHarness> {
  const userDataDir = await mkdtemp(join(tmpdir(), "nearmate-backend-supervisor-"));
  const child = createRunningChild();
  const spawnSpy = vi.fn((command: string, _args?: unknown, _options?: unknown) => {
    // stop() 在 Windows 上用 taskkill 结束进程树；mock 立即回 exit，避免测试挂起。
    if (String(command).includes("taskkill")) {
      const killer = new EventEmitter();
      queueMicrotask(() => killer.emit("exit", 0));
      return killer as unknown as ChildProcess;
    }
    return child;
  });
  const harness = {} as SupervisorHarness;
  const fetchSpy = vi.fn(() => input.respond(harness));
  Object.assign(harness, {
    child,
    spawnSpy,
    fetchSpy,
    userDataDir,
    spawnedInstanceId() {
      const spawnOptions = spawnSpy.mock.calls[0]?.[2] as unknown as { env: Record<string, string> };
      return spawnOptions.env["NEARMATE_RUNTIME_INSTANCE"];
    }
  });
  harness.supervisor = createBackendSupervisor({
    pythonExe: "C:/NearMate/runtime/python.exe",
    cwd: "C:/NearMate/runtime/backend",
    runtimeLayout: createRuntimeLayout(),
    userDataDir,
    controlUrl: "http://127.0.0.1:8765",
    spawnProcess: spawnSpy as unknown as typeof spawn,
    fetchImpl: fetchSpy as unknown as typeof fetch,
    startupTimeoutMs: input.startupTimeoutMs,
    pollIntervalMs: input.pollIntervalMs ?? 1,
    healthRequestTimeoutMs: input.healthRequestTimeoutMs ?? 100,
    stopTimeoutMs: 25,
    onUnexpectedTermination: input.onUnexpectedTermination
  });
  return harness;
}

describe("backend supervisor", () => {
  it("壳层媒体配置覆盖宿主别名，仅进入 Backend 子进程环境", () => {
    const base = { SystemRoot: "C:/Windows", livekit_url: "wss://old.test", LiveKit_Api_Key: "old-key", LIVEKIT_API_SECRET: "old-secret" };
    const result = createPackagedBackendEnvironment(createRuntimeLayout(), "C:/data", "http://127.0.0.1:8765", "instance", base, {
      url: "ws://127.0.0.1:20001", apiKey: "new-key", apiSecret: "new-secret"
    });
    expect(result.LIVEKIT_URL).toBe("ws://127.0.0.1:20001");
    expect(result.LIVEKIT_API_KEY).toBe("new-key");
    expect(result.LIVEKIT_API_SECRET).toBe("new-secret");
    expect(result.livekit_url).toBeUndefined();
    expect(result.LiveKit_Api_Key).toBeUndefined();
    expect(base.livekit_url).toBe("wss://old.test");
  });
  it("默认预算允许大型本地模型超过三十秒后就绪", async () => {
    vi.useFakeTimers();
    try {
      const startedAt = Date.now();
      const harness = await createSupervisorHarness({
        respond: async current => Date.now() - startedAt < 45_000
          ? new Response("", { status: 503 })
          : healthResponse(current.spawnedInstanceId()),
        pollIntervalMs: 1_000
      });
      let result: unknown = "pending";
      const startup = harness.supervisor.start().then(
        () => { result = null; }, error => { result = error; }
      );
      await vi.advanceTimersByTimeAsync(45_000);
      expect(result).toBeNull();
      await startup;
      expect(harness.spawnSpy).toHaveBeenCalledOnce();
    } finally {
      vi.useRealTimers();
    }
  });

  it("默认冷启动预算仍在两分钟到期，不无限等待挂起的健康请求", async () => {
    vi.useFakeTimers();
    try {
      const harness = await createSupervisorHarness({
        respond: async () => await new Promise<Response>(() => undefined),
        healthRequestTimeoutMs: 2_000,
        pollIntervalMs: 1_000
      });
      let result: unknown = "pending";
      const startup = harness.supervisor.start().catch(error => { result = error; });
      await vi.advanceTimersByTimeAsync(119_999);
      expect(result).toBe("pending");
      await vi.advanceTimersByTimeAsync(26);
      await startup;
      expect(result).toMatchObject({ failureCode: "startup_timeout" });
      expect((result as Error).message).toContain("等待 120 秒");
    } finally {
      vi.useRealTimers();
    }
  });

  it("冷启动超时清理后仍记录原进程身份和安全失败类型", async () => {
    const harness = await createSupervisorHarness({
      respond: async () => { throw new Error("http://user:private-token@local.invalid"); },
      startupTimeoutMs: 20
    });
    harness.spawnSpy.mockImplementation((command: string) => {
      if (command !== "taskkill") return harness.child;
      const killer = new EventEmitter();
      queueMicrotask(() => {
        harness.child.exitCode = 1;
        harness.child.emit("exit", 1, null);
        killer.emit("exit", 0, null);
      });
      return killer as unknown as ChildProcess;
    });
    harness.child.kill = vi.fn(() => {
      harness.child.exitCode = 1;
      harness.child.emit("exit", 1, null);
      return true;
    });
    await expect(harness.supervisor.start()).rejects.toMatchObject({ failureCode: "startup_timeout" });
    const log = await readFile(join(harness.userDataDir, "logs", "backend-startup.log"), "utf8");
    const failureLine = log.split("\n").find(line => line.includes('"event":"backend.start_failed"'));
    expect(failureLine).toContain('"pid":4321');
    expect(failureLine).toContain(`"instance_id":"${harness.spawnedInstanceId()}"`);
    expect(failureLine).toContain('"failure_code":"startup_timeout"');
    expect(failureLine).toContain('"startup_timeout_ms":20');
    expect(log).not.toContain("private-token");
  });

  it("无法创建进程时保留本次实例与固定失败类型，不写入异常原文", async () => {
    const harness = await createSupervisorHarness({ respond: async () => new Response("") });
    harness.spawnSpy.mockImplementation(() => { throw new Error("private-process-details"); });
    await expect(harness.supervisor.start()).rejects.toMatchObject({ failureCode: "spawn_failed" });
    const log = await readFile(join(harness.userDataDir, "logs", "backend-startup.log"), "utf8");
    const failureLine = log.split("\n").find(line => line.includes('"event":"backend.start_failed"'));
    expect(failureLine).toContain('"pid":null');
    expect(failureLine).toMatch(/"instance_id":"[0-9a-f-]{36}"/);
    expect(failureLine).toContain('"failure_code":"spawn_failed"');
    expect(log).not.toContain("private-process-details");
  });

  it("健康检查后原生崩溃也记录真实退出码并通知一次，不自动重启", async () => {
    const onUnexpectedTermination = vi.fn();
    const harness = await createSupervisorHarness({
      respond: async current => healthResponse(current.spawnedInstanceId()),
      onUnexpectedTermination
    });
    await harness.supervisor.start();
    harness.child.stderr?.emit("data", "ORT runtime API mismatch\n");
    harness.child.exitCode = -1073741819;
    harness.child.emit("exit", -1073741819, null);
    harness.child.emit("exit", -1073741819, null);
    await vi.waitFor(() => expect(onUnexpectedTermination).toHaveBeenCalledOnce());
    expect(onUnexpectedTermination).toHaveBeenCalledWith({
      kind: "exit", pid: 4321, code: -1073741819, signal: null,
      logPath: join(harness.userDataDir, "logs", "backend-startup.log")
    });
    const log = await readFile(join(harness.userDataDir, "logs", "backend-startup.log"), "utf8");
    expect(log).toContain('"event":"backend.ready"');
    expect(log).toContain('"event":"backend.terminated"');
    expect(log).toContain('"code":-1073741819');
    expect(log).toContain('"code_hex":"0xC0000005"');
    expect(log).toContain('"expected":false');
    expect(log).toContain("ORT runtime API mismatch");
    expect(harness.spawnSpy).toHaveBeenCalledOnce();
  });

  it("日志达到容量上限后仍保留本次崩溃摘要和最近输出", async () => {
    const harness = await createSupervisorHarness({ respond: async current => healthResponse(current.spawnedInstanceId()) });
    await harness.supervisor.start();
    const logPath = join(harness.userDataDir, "logs", "backend-startup.log");
    await writeFile(logPath, "x".repeat(2 * 1024 * 1024));
    harness.child.stderr?.emit("data", "most recent failure evidence\n");
    harness.child.exitCode = -1073741819;
    harness.child.emit("exit", -1073741819, null);
    await Promise.resolve();
    const log = await readFile(logPath, "utf8");
    expect(log.includes("most recent failure evidence")).toBe(true);
    expect(log.includes('"event":"backend.terminated"')).toBe(true);
    expect(Buffer.byteLength(log)).toBeLessThanOrEqual(2 * 1024 * 1024);
    expect((await readFile(`${logPath}.1`)).length).toBe(2 * 1024 * 1024);
  });

  it("主动停止只记录期望退出，不把正常关闭误报成崩溃", async () => {
    const onUnexpectedTermination = vi.fn();
    const harness = await createSupervisorHarness({
      respond: async current => healthResponse(current.spawnedInstanceId()),
      onUnexpectedTermination
    });
    await harness.supervisor.start();
    const stopping = harness.supervisor.stop();
    harness.child.exitCode = 0;
    harness.child.emit("exit", 0, null);
    await stopping;
    expect(onUnexpectedTermination).not.toHaveBeenCalled();
    const log = await readFile(join(harness.userDataDir, "logs", "backend-startup.log"), "utf8");
    expect(log).toContain('"event":"backend.stop_requested"');
    expect(log).toContain('"expected":true');
  });

  it("强制停止同样不会误报成运行期崩溃", async () => {
    const onUnexpectedTermination = vi.fn();
    const harness = await createSupervisorHarness({
      respond: async current => healthResponse(current.spawnedInstanceId()),
      onUnexpectedTermination
    });
    await harness.supervisor.start();
    harness.supervisor.forceStop();
    harness.child.exitCode = 1;
    harness.child.emit("exit", 1, null);
    await Promise.resolve();
    expect(onUnexpectedTermination).not.toHaveBeenCalled();
    const log = await readFile(join(harness.userDataDir, "logs", "backend-startup.log"), "utf8");
    expect(log).toContain('"event":"backend.force_stop_requested"');
    expect(log).toContain('"expected":true');
  });

  it("启动阶段的 error 由启动失败路径处理，不重复触发运行期通知", async () => {
    const onUnexpectedTermination = vi.fn();
    const harness = await createSupervisorHarness({
      respond: async current => {
        current.child.emit("error", new Error("spawn ENOENT"));
        return healthResponse(current.spawnedInstanceId());
      },
      onUnexpectedTermination
    });
    await expect(harness.supervisor.start()).rejects.toThrow("启动失败");
    expect(onUnexpectedTermination).not.toHaveBeenCalled();
    const log = await readFile(join(harness.userDataDir, "logs", "backend-startup.log"), "utf8");
    expect(log).toContain('"event":"backend.terminated"');
    expect(log).toContain('"phase":"starting"');
    expect(log).toContain('"failure_code":"spawn_failed"');
  });

  it("运行期 error 会通知，但通知本身失败不能制造未处理 rejection", async () => {
    const onUnexpectedTermination = vi.fn(async () => { throw new Error("native dialog unavailable"); });
    const harness = await createSupervisorHarness({
      respond: async current => healthResponse(current.spawnedInstanceId()),
      onUnexpectedTermination
    });
    await harness.supervisor.start();
    harness.child.emit("error", new Error("process handle unavailable"));
    harness.child.emit("exit", 1, null);
    await vi.waitFor(() => expect(onUnexpectedTermination).toHaveBeenCalledOnce());
    const log = await readFile(join(harness.userDataDir, "logs", "backend-startup.log"), "utf8");
    expect(log).toContain('"event":"backend.notification_failed"');
    expect(log).toContain('"kind":"error"');
  });

  it("并发 start 必须共享同一个健康检查结果，不能在 child 尚未就绪时提前返回", async () => {
    let releaseHealth!: () => void;
    const healthGate = new Promise<void>(resolve => {
      releaseHealth = resolve;
    });
    const harness = await createSupervisorHarness({
      respond: async currentHarness => {
        await healthGate;
        return healthResponse(currentHarness.spawnedInstanceId());
      }
    });

    let secondStartResolved = false;
    const firstStart = harness.supervisor.start();
    await vi.waitFor(() => expect(harness.fetchSpy).toHaveBeenCalledOnce());
    const secondStart = harness.supervisor.start().then(() => {
      secondStartResolved = true;
    });

    // spawn 存在只说明进程已创建；所有并发调用方都必须等同一轮 /healthz 身份校验。
    await Promise.resolve();
    expect(secondStartResolved).toBe(false);
    expect(harness.spawnSpy.mock.calls.filter(([command]) => command !== "taskkill")).toHaveLength(1);

    releaseHealth();
    await Promise.all([firstStart, secondStart]);
    expect(secondStartResolved).toBe(true);
  });

  it("停止旧 Backend 尚未完成时，新的 start 必须等待旧 child 退出后再 spawn", async () => {
    const firstChild = createRunningChild();
    const secondChild = createRunningChild();
    const userDataDir = await mkdtemp(join(tmpdir(), "nearmate-backend-supervisor-race-"));
    let backendSpawnCount = 0;
    const spawnSpy = vi.fn((command: string) => {
      if (command === "taskkill") {
        const killer = new EventEmitter();
        queueMicrotask(() => killer.emit("exit", 0));
        return killer as unknown as ChildProcess;
      }
      backendSpawnCount += 1;
      return backendSpawnCount === 1 ? firstChild : secondChild;
    });
    const fetchSpy = vi.fn(async () => {
      const calls = spawnSpy.mock.calls as unknown as SpawnCallWithEnvironment[];
      const spawnOptions = [...calls].reverse().find(([command]) => command !== "taskkill")?.[2];
      if (!spawnOptions) throw new Error("测试未观察到 Backend spawn 调用");
      return healthResponse(spawnOptions.env["NEARMATE_RUNTIME_INSTANCE"]);
    });
    const supervisor = createBackendSupervisor({
      pythonExe: "C:/NearMate/runtime/python.exe",
      cwd: "C:/NearMate/runtime/backend",
      runtimeLayout: createRuntimeLayout(),
      userDataDir,
      controlUrl: "http://127.0.0.1:8765",
      spawnProcess: spawnSpy as unknown as typeof spawn,
      fetchImpl: fetchSpy as unknown as typeof fetch,
      platform: "win32",
      pollIntervalMs: 1,
      stopTimeoutMs: 100
    });

    await supervisor.start();
    const stopping = supervisor.stop();
    const restarting = supervisor.start();

    // 旧 child 尚未发出 exit 事实时，新启动只能等待，不能提前占用端口。
    await Promise.resolve();
    expect(spawnSpy.mock.calls.filter(([command]) => command !== "taskkill")).toHaveLength(1);

    firstChild.exitCode = 0;
    firstChild.emit("exit", 0, null);
    await stopping;
    await restarting;

    expect(spawnSpy.mock.calls.filter(([command]) => command !== "taskkill")).toHaveLength(2);
    const stoppingSecond = supervisor.stop();
    secondChild.exitCode = 0;
    secondChild.emit("exit", 0, null);
    await stoppingSecond;
  });

  it("stop 取代正在等待健康检查的 start 时，迟到的健康响应不能让旧 start 成功", async () => {
    const child = createRunningChild();
    const userDataDir = await mkdtemp(join(tmpdir(), "nearmate-backend-supervisor-race-"));
    let resolveHealth!: (response: Response) => void;
    let healthBodyConsumed = false;
    const healthPromise = new Promise<Response>(resolve => {
      resolveHealth = resolve;
    });
    const spawnSpy = vi.fn((command: string) => {
      if (command === "taskkill") {
        const killer = new EventEmitter();
        queueMicrotask(() => killer.emit("exit", 0));
        return killer as unknown as ChildProcess;
      }
      return child;
    });
    const supervisor = createBackendSupervisor({
      pythonExe: "C:/NearMate/runtime/python.exe",
      cwd: "C:/NearMate/runtime/backend",
      runtimeLayout: createRuntimeLayout(),
      userDataDir,
      controlUrl: "http://127.0.0.1:8765",
      spawnProcess: spawnSpy as unknown as typeof spawn,
      fetchImpl: vi.fn(() => healthPromise) as unknown as typeof fetch,
      platform: "win32",
      startupTimeoutMs: 500,
      healthRequestTimeoutMs: 200,
      stopTimeoutMs: 100
    });

    const starting = supervisor.start();
    await vi.waitFor(() => {
      expect(spawnSpy.mock.calls.filter(([command]) => command !== "taskkill")).toHaveLength(1);
    });

    const stopping = supervisor.stop();
    const calls = spawnSpy.mock.calls as unknown as SpawnCallWithEnvironment[];
    const spawnOptions = calls.find(([command]) => command !== "taskkill")?.[2];
    if (!spawnOptions) throw new Error("测试未观察到 Backend spawn 调用");
    // 健康应答在 stop 之后才到达，必须被代际校验拒绝。
    resolveHealth({
      ok: true,
      status: 200,
      json: async () => {
        healthBodyConsumed = true;
        return { instance_id: spawnOptions.env["NEARMATE_RUNTIME_INSTANCE"] };
      }
    } as unknown as Response);
    // 先让 waitForBackend 完成响应体解析，再发出 exit；这样断言的是迟到成功而不是提前退出。
    await vi.waitFor(() => expect(healthBodyConsumed).toBe(true));
    await Promise.resolve();
    child.exitCode = 0;
    child.emit("exit", 0, null);
    await expect(starting).rejects.toThrow(/Backend start was superseded by stop/);
    await stopping;
  });

  it("Windows taskkill 返回非零时，stop 必须报告终止失败而不是静默成功", async () => {
    const child = createRunningChild();
    const userDataDir = await mkdtemp(join(tmpdir(), "nearmate-backend-supervisor-stop-"));
    const spawnSpy = vi.fn((command: string) => {
      if (command === "taskkill") {
        const killer = new EventEmitter();
        queueMicrotask(() => killer.emit("exit", 5, null));
        return killer as unknown as ChildProcess;
      }
      return child;
    });
    const supervisor = createBackendSupervisor({
      pythonExe: "C:/NearMate/runtime/python.exe",
      cwd: "C:/NearMate/runtime/backend",
      runtimeLayout: createRuntimeLayout(),
      userDataDir,
      controlUrl: "http://127.0.0.1:8765",
      spawnProcess: spawnSpy as unknown as typeof spawn,
      fetchImpl: vi.fn(async () => {
        const spawnOptions = getSpawnOptions(spawnSpy, 0);
        return healthResponse(spawnOptions.env["NEARMATE_RUNTIME_INSTANCE"]);
      }) as unknown as typeof fetch,
      platform: "win32",
      stopTimeoutMs: 100
    });

    await supervisor.start();
    await expect(supervisor.stop()).rejects.toThrow(/taskkill 终止 Backend 失败：退出码 5/);
    child.exitCode = 5;
    child.emit("exit", 5, null);
  });

  it("stop 超时后 forceStop 会再次发出进程树终止请求", async () => {
    const harness = await createSupervisorHarness({
      respond: async current => healthResponse(current.spawnedInstanceId())
    });

    await harness.supervisor.start();
    await expect(harness.supervisor.stop()).rejects.toThrow(/停止超时/);

    // 第一次 stop 已经超时但 child 仍在运行；退出收口必须还能对同一 PID 发起第二次强制终止。
    harness.supervisor.forceStop();
    const taskkillCalls = harness.spawnSpy.mock.calls.filter(([command]) => command === "taskkill");
    expect(taskkillCalls).toHaveLength(2);
    expect(taskkillCalls[1]?.[1]).toEqual(["/pid", "4321", "/T", "/F"]);

    harness.child.exitCode = 137;
    harness.child.emit("exit", 137, null);
  });

  it("reports the exit code and diagnostic log when Backend dies before health check", async () => {
    const userDataDir = await mkdtemp(join(tmpdir(), "nearmate-backend-supervisor-"));
    const child = createExitedChild();
    const supervisor = createBackendSupervisor({
      pythonExe: "C:/NearMate/runtime/python.exe",
      cwd: "C:/NearMate/runtime/backend",
      runtimeLayout: createRuntimeLayout(),
      userDataDir,
      controlUrl: "http://127.0.0.1:8765",
      spawnProcess: vi.fn(() => child) as unknown as typeof import("node:child_process").spawn,
      fetchImpl: vi.fn(async () => {
        throw new Error("connect refused");
      }) as unknown as typeof fetch,
    });

    await expect(supervisor.start()).rejects.toThrow(
      `NearMate Backend 启动失败：Backend 在健康检查前退出，退出码 1。诊断日志：${join(userDataDir, "logs", "backend-startup.log")}`,
    );
    const log = await readFile(join(userDataDir, "logs", "backend-startup.log"), "utf8");
    const failureLine = log.split("\n").find(line => line.includes('"event":"backend.start_failed"'));
    expect(failureLine).toContain('"failure_code":"process_exited"');
    expect(failureLine).toContain('"pid":1234');
  });

  it("注入一次性运行身份，/healthz 回显本尊身份后判定启动成功", async () => {
    const harness = await createSupervisorHarness({
      respond: async current => healthResponse(current.spawnedInstanceId())
    });

    await harness.supervisor.start();

    const [command, args, spawnOptions] = harness.spawnSpy.mock.calls[0] as unknown as [
      string,
      string[],
      { env: Record<string, string> }
    ];
    expect(command).toBe("C:/NearMate/runtime/python.exe");
    expect(args).toEqual(["-I", "-B", "-X", "utf8", "-m", "companion.core.main"]);
    expect(harness.spawnedInstanceId()).toMatch(
      /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/
    );
    expect(spawnOptions.env["NEARMATE_RUNTIME_INSTANCE"]).toBe(harness.spawnedInstanceId());
  });

  it("发行 Backend 只继承基础目录，不把开发机 Python/CUDA 环境带入子进程", async () => {
    vi.stubEnv("PYTHONPATH", "C:/developer/source");
    vi.stubEnv("PYTHONHOME", "C:/developer/python");
    vi.stubEnv("CUDA_PATH", "C:/developer/cuda");
    vi.stubEnv("LIVEKIT_URL", "wss://livekit.example.test");
    vi.stubEnv("LIVEKIT_API_KEY", "managed-key");
    vi.stubEnv("LIVEKIT_API_SECRET", "managed-secret");
    try {
      const harness = await createSupervisorHarness({
        respond: async current => healthResponse(current.spawnedInstanceId())
      });

      await harness.supervisor.start();
      const spawnOptions = harness.spawnSpy.mock.calls[0]?.[2] as { env: Record<string, string | undefined> };

      expect(spawnOptions.env["NEARMATE_PACKAGED"]).toBe("1");
      expect(spawnOptions.env["NEARMATE_RESOURCE_ROOT"]).toBe("C:/NearMate");
      expect(spawnOptions.env["NEARMATE_DATA_ROOT"]).toBeDefined();
      expect(spawnOptions.env["PYTHONPATH"] ?? "").not.toContain("C:/developer/source");
      expect(spawnOptions.env["PYTHONHOME"]).toBeUndefined();
      expect(spawnOptions.env["CUDA_PATH"]).toBeUndefined();
      expect(spawnOptions.env["PYTHONNOUSERSITE"]).toBe("1");
      expect(spawnOptions.env["LIVEKIT_URL"]).toBe("wss://livekit.example.test");
      expect(spawnOptions.env["LIVEKIT_API_KEY"]).toBe("managed-key");
      expect(spawnOptions.env["LIVEKIT_API_SECRET"]).toBe("managed-secret");
    } finally {
      vi.unstubAllEnvs();
    }
  });

  it("健康检查必须命中 /healthz，而不是任何 200 应答都算就绪", async () => {
    const harness = await createSupervisorHarness({
      respond: async current => healthResponse(current.spawnedInstanceId())
    });

    await harness.supervisor.start();

    const requestedUrls = harness.fetchSpy.mock.calls.map(call => String(call[0]));
    expect(requestedUrls.length).toBeGreaterThan(0);
    for (const requestedUrl of requestedUrls) {
      expect(requestedUrl).toBe("http://127.0.0.1:8765/healthz");
    }
  });

  it("本尊迟到时继续轮询，回显正确身份后启动成功", async () => {
    let attempt = 0;
    const harness = await createSupervisorHarness({
      respond: async current => {
        attempt += 1;
        if (attempt < 3) {
          throw new Error("connect refused");
        }
        return healthResponse(current.spawnedInstanceId());
      }
    });

    await expect(harness.supervisor.start()).resolves.toBeUndefined();
    expect(attempt).toBe(3);
  });

  it("僵尸实例抢答且本次 child 退出时，报端口占用而不是挂接到僵尸", async () => {
    const harness = await createSupervisorHarness({
      respond: async current => {
        // 僵尸抢答之后，本次 spawn 的 Backend 因端口绑定失败退出。
        current.child.exitCode = 1;
        return healthResponse("zombie-instance");
      }
    });

    await expect(harness.supervisor.start()).rejects.toThrow(
      /控制端口 8765 已被另一个正在运行的 NearMate Backend 实例占用.*请关闭所有 NearMate 进程后重试/s
    );
  });

  it("僵尸实例一直抢答且本次 child 不退场时，超时后仍报端口占用", async () => {
    const harness = await createSupervisorHarness({
      respond: async () => healthResponse("zombie-instance"),
      startupTimeoutMs: 30
    });

    await expect(harness.supervisor.start()).rejects.toThrow(
      "控制端口 8765 已被另一个正在运行的 NearMate Backend 实例占用（健康检查超时）"
    );
  });

  it("健康响应缺少身份字段时按占用处理，不被未知应答挂接", async () => {
    const harness = await createSupervisorHarness({
      respond: async current => {
        current.child.exitCode = 1;
        return new Response(JSON.stringify({ status: "ok" }), { status: 200 });
      }
    });

    await expect(harness.supervisor.start()).rejects.toThrow(
      "控制端口 8765 已被另一个正在运行的 NearMate Backend 实例占用"
    );
  });

  it("单次 fetch 永不返回时仍受总启动超时约束", async () => {
    const harness = await createSupervisorHarness({
      respond: async () => await new Promise<Response>(() => undefined),
      startupTimeoutMs: 30,
      healthRequestTimeoutMs: 5
    });

    await expect(harness.supervisor.start()).rejects.toThrow(
      /后台启动超时/
    );
  });

  it("健康响应体永不结束时仍受单次请求超时约束", async () => {
    const harness = await createSupervisorHarness({
      respond: async () => ({
        ok: true,
        status: 200,
        json: async () => await new Promise<never>(() => undefined)
      }) as unknown as Response,
      startupTimeoutMs: 30,
      healthRequestTimeoutMs: 5
    });

    await expect(harness.supervisor.start()).rejects.toThrow(
      /后台启动超时/
    );
  });
});
