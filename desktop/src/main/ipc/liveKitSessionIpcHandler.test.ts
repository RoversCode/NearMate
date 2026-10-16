import { EventEmitter } from "node:events";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { registerLiveKitSessionIpcHandler } from "./liveKitSessionIpcHandler";
import type { RegisterShellIpcHandlersInput } from "./shellIpcTypes";

const client = vi.hoisted(() => ({ createSession: vi.fn() }));
vi.mock("../services/liveKitSessionClient", () => ({ createLiveKitSessionClient: () => client }));
const session = { url: "ws://localhost:1", roomName: "test", desktopUi: { identity: "ui", token: "ui-token" }, desktopAec: { identity: "aec", token: "aec-token" } };
function deferred<T>() { let resolve!: (value: T) => void; const promise = new Promise<T>(done => { resolve = done; }); return { promise, resolve }; }
function harness() {
  const handlers = new Map<string, (event: { sender: EventEmitter }) => unknown>();
  const sender = Object.assign(new EventEmitter(), { isDestroyed: () => false });
  const aec = { start: vi.fn(async () => {}), stopAndWait: vi.fn(async () => {}) };
  registerLiveKitSessionIpcHandler({ handle: vi.fn((channel, _allowed, handler) => { handlers.set(channel, handler as (event: { sender: EventEmitter }) => unknown); }) }, { aecSidecarSupervisor: aec } as unknown as RegisterShellIpcHandlersInput);
  return { aec, sender, invoke: (channel: string, owner: EventEmitter = sender) => handlers.get(channel)?.({ sender: owner }) };
}

describe("LiveKit IPC 取消边界", () => {
  beforeEach(() => client.createSession.mockReset().mockResolvedValue(session));
  it("取消 pending HTTP 会传递信号，迟到成功不能启动 AEC", async () => {
    const pending = deferred<typeof session>();
    client.createSession.mockReturnValueOnce(pending.promise);
    const h = harness();
    const result = (h.invoke("livekit:create-session") as Promise<unknown>).catch(error => error);
    h.invoke("livekit:cancel-session");
    pending.resolve(session);
    expect(await result).toMatchObject({ message: "媒体会话请求已取消。" });
    expect(client.createSession.mock.calls[0][0].aborted).toBe(true);
    expect(h.aec.start).not.toHaveBeenCalled();
  });

  it("AEC 正在启动时取消，要等其退出才释放本轮 single-flight", async () => {
    const pendingAec = deferred<void>();
    const h = harness();
    h.aec.start.mockReturnValueOnce(pendingAec.promise);
    const task = h.invoke("livekit:create-session") as Promise<unknown>;
    const result = task.catch(error => error);
    await Promise.resolve();
    h.invoke("livekit:cancel-session");
    expect(h.invoke("livekit:create-session")).toBe(task);
    pendingAec.resolve();
    expect(await result).toMatchObject({ message: "媒体会话请求已取消。" });
    expect(h.aec.stopAndWait).toHaveBeenCalledOnce();
    await h.invoke("livekit:create-session");
    expect(client.createSession).toHaveBeenCalledTimes(2);
  });

  it.each(["destroyed", "render-process-gone"])("窗口 %s 时主进程主动取消，迟到响应不能打开麦克风", async (event) => {
    const pending = deferred<typeof session>();
    client.createSession.mockReturnValueOnce(pending.promise);
    const h = harness();
    const result = (h.invoke("livekit:create-session") as Promise<unknown>).catch(error => error);
    h.sender.emit(event);
    pending.resolve(session);
    expect(await result).toMatchObject({ message: "媒体会话请求已取消。" });
    expect(h.aec.start).not.toHaveBeenCalled();
    expect(h.sender.listenerCount("destroyed")).toBe(0);
    expect(h.sender.listenerCount("render-process-gone")).toBe(0);
  });

  it("旧窗口的取消消息不能取消新窗口的会话请求", async () => {
    const pending = deferred<typeof session>();
    client.createSession.mockReturnValueOnce(pending.promise);
    const h = harness();
    const result = h.invoke("livekit:create-session") as Promise<unknown>;
    h.invoke("livekit:cancel-session", new EventEmitter());
    pending.resolve(session);
    await expect(result).resolves.toMatchObject({ audio_input_status: "available" });
    expect(h.aec.start).toHaveBeenCalledOnce();
  });
});
