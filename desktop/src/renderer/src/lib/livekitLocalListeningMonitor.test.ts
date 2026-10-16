import { describe, expect, it, vi } from "vitest";
import { LiveKitLocalListeningMonitor } from "./livekitLocalListeningMonitor";

describe("LiveKitLocalListeningMonitor", () => {
  it("超过阈值立即激活，短暂低音量不会闪烁，释放延迟后关闭", () => {
    const frames = createFrameScheduler();
    const calculateVolume = vi.fn()
      .mockReturnValueOnce(0.08)
      .mockReturnValueOnce(0.01)
      .mockReturnValueOnce(0.01);
    const cleanup = vi.fn();
    const activeStates: boolean[] = [];
    const monitor = new LiveKitLocalListeningMonitor({
      attackThreshold: 0.04,
      releaseDelayMs: 240,
      createAnalyser: () => ({ calculateVolume, cleanup }),
      requestFrame: frames.requestFrame,
      cancelFrame: frames.cancelFrame
    });

    monitor.start({}, active => activeStates.push(active));
    frames.runNext(100);
    frames.runNext(220);
    frames.runNext(350);

    expect(activeStates).toEqual([true, false]);
    monitor.stop();
    expect(cleanup).toHaveBeenCalledOnce();
  });

  it("退订旧轨道不会误停后来接管的新 clean-mic", () => {
    const frames = createFrameScheduler();
    const firstCleanup = vi.fn();
    const secondCleanup = vi.fn();
    const firstTrack = {};
    const secondTrack = {};
    const createAnalyser = vi.fn()
      .mockReturnValueOnce({ calculateVolume: () => 0.08, cleanup: firstCleanup })
      .mockReturnValueOnce({ calculateVolume: () => 0.08, cleanup: secondCleanup });
    const monitor = new LiveKitLocalListeningMonitor({
      createAnalyser,
      requestFrame: frames.requestFrame,
      cancelFrame: frames.cancelFrame
    });

    monitor.start(firstTrack, vi.fn());
    monitor.start(secondTrack, vi.fn());
    monitor.stop(firstTrack);

    expect(firstCleanup).toHaveBeenCalledOnce();
    expect(secondCleanup).not.toHaveBeenCalled();

    monitor.stop(secondTrack);
    expect(secondCleanup).toHaveBeenCalledOnce();
  });
});

/**
 * createFrameScheduler 创建可手动推进时间戳的动画帧调度器。
 *
 * Returns:
 *   监测器可注入的 request/cancel 函数和测试推进函数。
 */
function createFrameScheduler() {
  let nextFrameId = 1;
  const callbacks = new Map<number, FrameRequestCallback>();

  return {
    requestFrame: vi.fn((callback: FrameRequestCallback) => {
      const frameId = nextFrameId;
      nextFrameId += 1;
      callbacks.set(frameId, callback);
      return frameId;
    }),
    cancelFrame: vi.fn((frameId: number) => {
      callbacks.delete(frameId);
    }),
    runNext(timestamp: number) {
      const nextFrame = callbacks.entries().next().value as
        | [number, FrameRequestCallback]
        | undefined;
      if (!nextFrame) {
        throw new Error("没有待执行的动画帧");
      }

      callbacks.delete(nextFrame[0]);
      nextFrame[1](timestamp);
    }
  };
}
