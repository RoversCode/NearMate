import { EventEmitter } from "node:events";
import type { ChildProcess, spawn } from "node:child_process";
import { afterEach, describe, expect, it, vi } from "vitest";
import { createManagedLiveKitSupervisor } from "./managedLiveKitSupervisor";

function createHarness(overrides: Record<string, unknown> = {}) {
  const child = new EventEmitter() as ChildProcess;
  Object.assign(child, { pid: 5678, exitCode: null, kill: vi.fn(() => { queueMicrotask(() => child.emit("exit", 0, null)); return true; }) });
  const spawnProcess = vi.fn(() => child);
  const release = vi.fn(async () => {});
  const checkReady = vi.fn(async () => true);
  const reportDiagnostic = vi.fn();
  const onUnexpectedTermination = vi.fn();
  const supervisor = createManagedLiveKitSupervisor({
    executablePath: "C:/NearMate/runtime/tools/livekit/livekit-server.exe", userDataDir: "C:/test-data", environment: {},
    spawnProcess: spawnProcess as unknown as typeof spawn,
    reservePorts: async () => ({ httpPort: 20001, tcpPort: 20002, udpPort: 20003, release }),
    checkReady, reportDiagnostic, onUnexpectedTermination, startupTimeoutMs: 100, pollIntervalMs: 10, stopTimeoutMs: 10,
    ...overrides
  });
  return { supervisor, child, spawnProcess, release, checkReady, reportDiagnostic, onUnexpectedTermination };
}

describe("受管本地 LiveKit", () => {
  afterEach(() => vi.useRealTimers());

  it("完整外部配置不启动或停止任何本地服务", async () => {
    const h = createHarness({ environment: { LIVEKIT_URL: "wss://example.test", LIVEKIT_API_KEY: "test-key", LIVEKIT_API_SECRET: "test-secret" } });
    expect(await h.supervisor.start()).toEqual({ url: "wss://example.test", apiKey: "test-key", apiSecret: "test-secret" });
    await h.supervisor.stop();
    h.supervisor.forceStop();
    expect(h.spawnProcess).not.toHaveBeenCalled();
  });

  it("不完整外部配置和缺少随包文件路径都在 spawn 前失败", async () => {
    const h = createHarness({ environment: { LIVEKIT_API_KEY: "private-test-key" } });
    await expect(h.supervisor.start()).rejects.toThrow("外部语音服务配置不完整");
    expect(h.spawnProcess).not.toHaveBeenCalled();
    const missing = createHarness({ executablePath: null });
    await expect(missing.supervisor.start()).rejects.toThrow("重新安装完整版本");
  });

  it("并发 start 只有一个进程，认证就绪后才返回，配置不落盘或进入命令行/日志", async () => {
    const h = createHarness({ environment: { SYSTEMROOT: "C:/Windows", PRIVATE_KEY: "host-secret" } });
    const [a, b] = await Promise.all([h.supervisor.start(), h.supervisor.start()]);
    expect(a).toEqual(b);
    expect(h.spawnProcess).toHaveBeenCalledOnce();
    const [, args, options] = h.spawnProcess.mock.calls[0] as unknown as [string, string[], { env: NodeJS.ProcessEnv; windowsHide: boolean; stdio: string }];
    expect(args).toEqual([]);
    expect(options.windowsHide).toBe(true);
    expect(options.stdio).toBe("ignore");
    expect(options.env.PRIVATE_KEY).toBeUndefined();
    expect(JSON.parse(options.env.LIVEKIT_CONFIG!).keys[a.apiKey]).toBe(a.apiSecret);
    expect(JSON.stringify(h.reportDiagnostic.mock.calls)).not.toContain(a.apiSecret);
    expect(h.checkReady).toHaveBeenCalled();
    await h.supervisor.stop();
    expect(h.child.kill).toHaveBeenCalled();
    expect(h.onUnexpectedTermination).not.toHaveBeenCalled();
  });

  it("服务一直不就绪时有界失败并清理自己创建的进程", async () => {
    vi.useFakeTimers();
    const h = createHarness({ checkReady: async () => false });
    const result = h.supervisor.start().catch(error => error);
    await vi.advanceTimersByTimeAsync(200);
    expect(await result).toMatchObject({ message: "本地语音服务启动超时，请重新打开 NearMate。" });
    expect(h.child.kill).toHaveBeenCalled();
  });

  it("启动期间 stop 取消尚未完成的端口分配，不产生迟到子进程", async () => {
    let releasePorts!: (value: { httpPort: number; tcpPort: number; udpPort: number; release: () => Promise<void> }) => void;
    const ports = new Promise<{ httpPort: number; tcpPort: number; udpPort: number; release: () => Promise<void> }>(resolve => { releasePorts = resolve; });
    const h = createHarness({ reservePorts: () => ports });
    const result = h.supervisor.start().catch(error => error);
    await h.supervisor.stop();
    releasePorts({ httpPort: 20001, tcpPort: 20002, udpPort: 20003, release: h.release });
    expect(await result).toMatchObject({ message: "本地语音服务启动已取消。" });
    expect(h.spawnProcess).not.toHaveBeenCalled();
    expect(h.release).toHaveBeenCalledOnce();
  });

  it("就绪后意外退出仅通知一次，不自动重启或输出原始异常", async () => {
    const h = createHarness();
    await h.supervisor.start();
    h.child.emit("error", new Error("secret-value"));
    h.child.emit("exit", 1, null);
    expect(h.onUnexpectedTermination).toHaveBeenCalledOnce();
    expect(h.spawnProcess).toHaveBeenCalledOnce();
    expect(JSON.stringify(h.reportDiagnostic.mock.calls)).not.toContain("secret-value");
  });
});
