import { describe, expect, it, vi } from "vitest";
import { LiveKitAssistantTrackPlayback } from "./livekitAssistantTrackPlayback";
import type { LiveKitRoomHandlers } from "./livekitRoomTypes";

describe("助手音轨异步播放归属", () => {
  it.each([false, true])("旧音轨的迟到 play %s 不能接管或释放新音轨", async fails => {
    let resolve!: () => void;
    let reject!: (error: Error) => void;
    const oldPlay = new Promise<void>((done, fail) => { resolve = done; reject = fail; });
    const sink = { playTrack: vi.fn(async (): Promise<void> => undefined).mockReturnValueOnce(oldPlay), release: vi.fn() };
    const manager = new LiveKitAssistantTrackPlayback(sink);
    const handlers: LiveKitRoomHandlers = {
      onStatusChange: vi.fn(), onAssistantTrackSubscribed: vi.fn(), onAssistantPlaybackReady: vi.fn(),
      onAssistantPlaybackUnavailable: vi.fn()
    };
    const pending = manager.playTrack({}, handlers);
    manager.release();
    await manager.playTrack({}, handlers);
    const releases = sink.release.mock.calls.length;
    if (fails) reject(new Error("old play failed")); else resolve();
    await pending;
    expect(sink.release).toHaveBeenCalledTimes(releases);
    expect(handlers.onAssistantPlaybackReady).toHaveBeenCalledOnce();
    expect(handlers.onAssistantPlaybackUnavailable).not.toHaveBeenCalled();
  });
});
