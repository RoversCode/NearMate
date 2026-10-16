import { describe, expect, it, vi } from "vitest";
import { createEnvironmentCaptureService, fitWithinLongEdge } from "./contextCapture";

describe("createEnvironmentCaptureService", () => {
  it("采集完整显示器并把原始位图交给编码器", async () => {
    const encode = vi.fn(async () => Buffer.from("png"));
    const getSources = vi.fn(async () => [{
      display_id: "1",
      thumbnail: {
        isEmpty: () => false,
        getSize: () => ({ width: 1600, height: 900 }),
        toBitmap: () => Buffer.alloc(1600 * 900 * 4)
      }
    }]);
    const capture = createEnvironmentCaptureService({
      activeWindow: { readTitle: () => "virtual_companion" },
      imageEncoder: { encode, stop: vi.fn() },
      screen: {
        getCursorScreenPoint: () => ({ x: 10, y: 10 }),
        getDisplayNearestPoint: () => ({ id: 1, bounds: { width: 2560, height: 1440 } })
      },
      desktopCapturer: { getSources },
      now: () => new Date("2026-06-26T02:00:00.000Z")
    });

    const snapshot = await capture.captureSnapshot();

    expect(getSources).toHaveBeenCalledWith({
      types: ["screen"],
      thumbnailSize: { width: 1600, height: 900 }
    });
    expect(encode).toHaveBeenCalledOnce();
    expect(snapshot.activeWindowTitle).toBe("virtual_companion");
    expect(snapshot.screenshotBytes).toEqual(Buffer.from("png"));
    expect(snapshot.captureError).toBeNull();
  });

  it("截图失败时保留窗口标题并返回稳定错误", async () => {
    const capture = createEnvironmentCaptureService({
      activeWindow: { readTitle: () => "Code" },
      imageEncoder: { encode: vi.fn(), stop: vi.fn() },
      screen: {
        getCursorScreenPoint: () => ({ x: 0, y: 0 }),
        getDisplayNearestPoint: () => ({ id: 1, bounds: { width: 1920, height: 1080 } })
      },
      desktopCapturer: { getSources: vi.fn(async () => { throw new Error("capture denied"); }) }
    });

    const snapshot = await capture.captureSnapshot();

    expect(snapshot.activeWindowTitle).toBe("Code");
    expect(snapshot.screenshotBytes).toBeNull();
    expect(snapshot.captureError).toContain("capture denied");
  });
});

describe("fitWithinLongEdge", () => {
  it("完整等比缩放而不裁剪", () => {
    expect(fitWithinLongEdge({ width: 2560, height: 1440 }, 1600)).toEqual({ width: 1600, height: 900 });
    expect(fitWithinLongEdge({ width: 1200, height: 800 }, 1600)).toEqual({ width: 1200, height: 800 });
  });
});
