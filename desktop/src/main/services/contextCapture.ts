import { desktopCapturer, screen } from "electron";
import { performance } from "node:perf_hooks";
import type { EnvironmentImageEncoder } from "./environmentImageEncoder";
import type { ActiveWindowTitleProvider } from "./windowsActiveWindow";

const MAX_SCREENSHOT_EDGE_PX = 1600;

export type CapturedEnvironmentSnapshot = {
  capturedAt: string;
  activeWindowTitle: string | null;
  screenshotBytes: Buffer | null;
  captureError: string | null;
  timingsMs: Record<string, number>;
};

type ScreenPort = {
  getCursorScreenPoint(): { x: number; y: number };
  getDisplayNearestPoint(point: { x: number; y: number }): {
    id: number;
    bounds: { width: number; height: number };
  };
};

type DesktopCapturerPort = {
  getSources(options: {
    types: Array<"screen">;
    thumbnailSize: { width: number; height: number };
  }): Promise<Array<{
    display_id: string;
    thumbnail: {
      isEmpty(): boolean;
      getSize(): { width: number; height: number };
      toBitmap(): Buffer;
    };
  }>>;
};

type ContextCaptureOptions = {
  activeWindow: ActiveWindowTitleProvider;
  imageEncoder: EnvironmentImageEncoder;
  screen?: ScreenPort;
  desktopCapturer?: DesktopCapturerPort;
  now?: () => Date;
  clock?: () => number;
};

export type EnvironmentCaptureService = {
  captureSnapshot(): Promise<CapturedEnvironmentSnapshot>;
};

/**
 * 创建不落盘的完整屏幕现场采集器。
 *
 * Args:
 *   options: 活动窗口、图片编码、Electron 屏幕能力与时间源。
 *
 * Returns:
 *   只在内存中返回标题和 PNG 字节的单次采集服务。
 */
export function createEnvironmentCaptureService(options: ContextCaptureOptions): EnvironmentCaptureService {
  const screenPort = options.screen ?? screen;
  const desktopCapturerPort = options.desktopCapturer ?? desktopCapturer;
  const now = options.now ?? (() => new Date());
  const clock = options.clock ?? (() => performance.now());

  return {
    async captureSnapshot(): Promise<CapturedEnvironmentSnapshot> {
      const totalStartedAt = clock();
      const timingsMs: Record<string, number> = {};
      const titleStartedAt = clock();
      let activeWindowTitle: string | null = null;
      const errors: string[] = [];
      try {
        activeWindowTitle = options.activeWindow.readTitle();
      } catch (error) {
        errors.push(`active_window_title:${errorText(error)}`);
      }
      timingsMs.activeWindowTitle = clock() - titleStartedAt;

      const capturedAt = now().toISOString();
      let screenshotBytes: Buffer | null = null;
      try {
        const cursorPoint = screenPort.getCursorScreenPoint();
        const display = screenPort.getDisplayNearestPoint(cursorPoint);
        const thumbnailSize = fitWithinLongEdge(display.bounds, MAX_SCREENSHOT_EDGE_PX);
        const captureStartedAt = clock();
        const sources = await desktopCapturerPort.getSources({ types: ["screen"], thumbnailSize });
        timingsMs.screenCapture = clock() - captureStartedAt;
        const source = sources.find(candidate => candidate.display_id === String(display.id)) ?? sources[0];
        if (!source || source.thumbnail.isEmpty()) {
          throw new Error("screen_source_empty");
        }
        const bitmapStartedAt = clock();
        const bitmap = source.thumbnail.toBitmap();
        const size = source.thumbnail.getSize();
        timingsMs.bitmapRead = clock() - bitmapStartedAt;
        const encodeStartedAt = clock();
        screenshotBytes = await options.imageEncoder.encode({ bitmap, width: size.width, height: size.height });
        timingsMs.imageEncode = clock() - encodeStartedAt;
      } catch (error) {
        errors.push(`screen_capture:${errorText(error)}`);
      }
      timingsMs.total = clock() - totalStartedAt;

      return {
        capturedAt,
        activeWindowTitle,
        screenshotBytes,
        captureError: errors.length > 0 ? errors.join(";") : null,
        timingsMs
      };
    }
  };
}

/**
 * 计算覆盖完整画面且最长边不超过限制的等比尺寸。
 *
 * Args:
 *   bounds: 当前显示器宽高。
 *   maxEdge: 输出最长边上限。
 *
 * Returns:
 *   不裁剪的目标缩略图尺寸。
 */
export function fitWithinLongEdge(
  bounds: { width: number; height: number },
  maxEdge: number
): { width: number; height: number } {
  const longestEdge = Math.max(bounds.width, bounds.height);
  if (longestEdge <= maxEdge) {
    return { width: bounds.width, height: bounds.height };
  }
  const scale = maxEdge / longestEdge;
  return {
    width: Math.max(1, Math.round(bounds.width * scale)),
    height: Math.max(1, Math.round(bounds.height * scale))
  };
}

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
