import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { runInNewContext } from "node:vm";

type StartupWindowStub = {
  close: ReturnType<typeof vi.fn>;
  destroy: ReturnType<typeof vi.fn>;
  isDestroyed: ReturnType<typeof vi.fn>;
  loadURL: ReturnType<typeof vi.fn>;
  webContents: {
    executeJavaScript: ReturnType<typeof vi.fn>;
    once: ReturnType<typeof vi.fn>;
  };
};

let latestWindow: StartupWindowStub;

vi.mock("electron", () => ({
  BrowserWindow: vi.fn(() => latestWindow)
}));

import { createStartupProgressWindow } from "./startupProgressWindow";

describe("startupProgressWindow", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    latestWindow = {
      close: vi.fn(),
      destroy: vi.fn(),
      isDestroyed: vi.fn(() => false),
      loadURL: vi.fn(async () => undefined),
      webContents: {
        executeJavaScript: vi.fn(async () => undefined),
        once: vi.fn()
      }
    };
  });

  afterEach(() => vi.useRealTimers());

  it("会把进度更新排队到页面加载完成后，普通更新不能提前显示 100%", async () => {
    let didFinishLoad: (() => void) | undefined;
    latestWindow.webContents.once.mockImplementation((event: string, listener: () => void) => {
      if (event === "did-finish-load") didFinishLoad = listener;
    });
    let resolveLoad!: () => void;
    latestWindow.loadURL.mockReturnValue(new Promise<void>(resolve => {
      resolveLoad = resolve;
    }));

    const progressWindow = createStartupProgressWindow();
    progressWindow.update({ progress: 140, title: "正在加载", detail: "请稍候" });

    expect(latestWindow.webContents.executeJavaScript).not.toHaveBeenCalled();

    didFinishLoad?.();
    resolveLoad();
    await Promise.resolve();

    expect(latestWindow.webContents.executeJavaScript).toHaveBeenCalledWith(
      expect.stringContaining('"progress":99'),
      true
    );
  });

  it("即使窗口禁止用户关闭，程序收口也会强制销毁窗口且保持幂等", () => {
    const progressWindow = createStartupProgressWindow();

    progressWindow.close();
    progressWindow.close();

    expect(latestWindow.destroy).toHaveBeenCalledOnce();
    expect(latestWindow.close).not.toHaveBeenCalled();
  });

  it("后台长时间等待时每 200ms 增长，但不能越过下一真实阶段", async () => {
    const progressWindow = createStartupProgressWindow();
    await Promise.resolve();
    progressWindow.update({ progress: 40, progressCeiling: 85, title: "正在启动后台", detail: "请稍候" });
    await vi.advanceTimersByTimeAsync(200);
    const firstTick = latestWindow.webContents.executeJavaScript.mock.calls.at(-1)?.[0] as string;
    await vi.advanceTimersByTimeAsync(60_000);
    const lastTick = latestWindow.webContents.executeJavaScript.mock.calls.at(-1)?.[0] as string;
    const readProgress = (script: string) => Number(script.match(/"progress":([\d.]+)/)?.[1]);
    expect(readProgress(firstTick)).toBeGreaterThan(40);
    expect(readProgress(lastTick)).toBeGreaterThan(readProgress(firstTick));
    expect(readProgress(lastTick)).toBeLessThan(85);
    expect(readProgress(lastTick)).toBeLessThan(100);
    progressWindow.close();
  });

  it("十秒后显示当前阶段等待时长，换阶段重置且系统时间变化不影响秒数", async () => {
    vi.useRealTimers();
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "setInterval", "clearInterval", "performance"] });
    const progressWindow = createStartupProgressWindow();
    await Promise.resolve();
    progressWindow.update({ progress: 40, progressCeiling: 85, title: "正在启动后台", detail: "正在等待本地模型。" });
    const lastScript = () => String(latestWindow.webContents.executeJavaScript.mock.calls.at(-1)?.[0]);
    await vi.advanceTimersByTimeAsync(9_800);
    expect(lastScript()).not.toContain("已等待");
    await vi.advanceTimersByTimeAsync(200);
    expect(lastScript()).toContain("正在等待本地模型。 已等待 10 秒。");
    vi.setSystemTime(new Date("2030-01-01"));
    await vi.advanceTimersByTimeAsync(1_000);
    expect(lastScript()).toContain("已等待 11 秒。");
    expect(lastScript().match(/已等待/g)).toHaveLength(1);
    progressWindow.update({ progress: 40, progressCeiling: 85, title: "正在启动后台", detail: "正在等待本地模型。" });
    expect(lastScript()).toContain("已等待 11 秒。");
    progressWindow.update({ progress: 85, progressCeiling: 90, title: "正在准备材料", detail: "正在整理本次材料。" });
    expect(lastScript()).not.toContain("已等待");
    await vi.advanceTimersByTimeAsync(10_000);
    expect(lastScript()).toContain("正在整理本次材料。 已等待 10 秒。");
    expect(lastScript()).not.toContain("本地模型");
    progressWindow.close();
  });

  it("等待 100% 绘制完成后再停留 300ms，迟到阶段不能覆盖成功", async () => {
    const progressWindow = createStartupProgressWindow();
    await Promise.resolve();
    let finishPaint!: () => void;
    latestWindow.webContents.executeJavaScript.mockReturnValue(new Promise<void>(resolve => { finishPaint = resolve; }));
    const completed = vi.fn();
    const completion = progressWindow.complete().then(completed);
    await Promise.resolve();
    progressWindow.update({ progress: 40, title: "迟到阶段", detail: "不应覆盖" });
    await vi.advanceTimersByTimeAsync(500);
    expect(completed).not.toHaveBeenCalled();
    expect(latestWindow.webContents.executeJavaScript).toHaveBeenLastCalledWith(expect.stringContaining('"progress":100'), true);
    finishPaint();
    await vi.advanceTimersByTimeAsync(299);
    expect(completed).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(1);
    await completion;
    expect(completed).toHaveBeenCalledOnce();
    progressWindow.close();
  });

  it("绘制或加载不返回时有界结束，主动关闭会立即取消完成等待", async () => {
    latestWindow.loadURL.mockReturnValue(new Promise(() => {}));
    const progressWindow = createStartupProgressWindow();
    const completion = progressWindow.complete();
    progressWindow.close();
    await completion;
    await vi.advanceTimersByTimeAsync(10_000);
    expect(latestWindow.webContents.executeJavaScript).not.toHaveBeenCalled();
    expect(latestWindow.destroy).toHaveBeenCalledOnce();
  });

  it.each(["load", "paint"])("%s 永不回执时三秒结束反馈等待，不遗留动画或计时器", async stage => {
    vi.spyOn(console, "warn").mockImplementation(() => {});
    if (stage === "load") latestWindow.loadURL.mockReturnValue(new Promise(() => {}));
    else latestWindow.webContents.executeJavaScript.mockReturnValue(new Promise(() => {}));
    const progressWindow = createStartupProgressWindow();
    await Promise.resolve();
    const completion = progressWindow.complete();
    await vi.advanceTimersByTimeAsync(3_000);
    await completion;
    progressWindow.close();
    expect(vi.getTimerCount()).toBe(0);
  });

  it("关闭成功停留会立即取消剩余 300ms，重复完成共享同一次交接", async () => {
    const progressWindow = createStartupProgressWindow();
    await Promise.resolve();
    const completion = progressWindow.complete();
    expect(progressWindow.complete()).toBe(completion);
    await vi.advanceTimersByTimeAsync(100);
    progressWindow.close();
    await completion;
    expect(vi.getTimerCount()).toBe(0);
  });

  it("真实内联页面显示 100% 后等待绘制机会，旧状态不能再覆盖成功文字", async () => {
    const progressWindow = createStartupProgressWindow();
    const html = decodeURIComponent(String(latestWindow.loadURL.mock.calls[0][0]).split(",").slice(1).join(","));
    expect(html).not.toContain('<main aria-live="polite">');
    expect(html).toContain('id="status-title" aria-live="polite"');
    const script = html.match(/<script>([\s\S]*?)<\/script>/)?.[1];
    const elements = new Map<string, { textContent: string; style: { width: string }; addEventListener: ReturnType<typeof vi.fn>; removeEventListener: ReturnType<typeof vi.fn>; setAttribute: ReturnType<typeof vi.fn> }>();
    const element = (id: string) => {
      if (!elements.has(id)) elements.set(id, { textContent: "", style: { width: "" }, addEventListener: vi.fn(), removeEventListener: vi.fn(), setAttribute: vi.fn() });
      return elements.get(id)!;
    };
    const renderer = {} as { updateStartupProgress(state: object): void; completeStartupProgress(state: object): Promise<void> };
    const paintCallbacks: Array<() => void> = [];
    runInNewContext(script!, {
      window: renderer,
      document: { getElementById: element, querySelector: () => element("track") },
      setTimeout, clearTimeout,
      requestAnimationFrame: (callback: () => void) => paintCallbacks.push(callback)
    });
    const finished = vi.fn();
    const completion = renderer.completeStartupProgress({ progress: 100, title: "已启动", detail: "完成" }).then(finished);
    renderer.updateStartupProgress({ progress: 40, title: "迟到的后台阶段", detail: "等待" });
    expect(element("progress-label").textContent).toBe("100%");
    expect(element("status-title").textContent).toBe("已启动");
    await vi.advanceTimersByTimeAsync(220);
    expect(finished).not.toHaveBeenCalled();
    paintCallbacks.shift()?.();
    expect(finished).not.toHaveBeenCalled();
    paintCallbacks.shift()?.();
    await completion;
    expect(finished).toHaveBeenCalledOnce();
    progressWindow.close();
  });
});
