import { EventEmitter } from "node:events";
import { afterEach, describe, expect, it, vi } from "vitest";
import { watchWindowFirstPaint } from "./windowFirstPaint";

afterEach(() => vi.useRealTimers());

describe("隐藏窗口首帧等待", () => {
  it("加载前登记能接住同步首帧，并清理超时及关闭监听", async () => {
    vi.useFakeTimers();
    const window = new EventEmitter();
    const firstPaint = watchWindowFirstPaint(window as never);
    window.emit("ready-to-show");
    await firstPaint.ready;
    expect(window.listenerCount("ready-to-show")).toBe(0);
    expect(window.listenerCount("closed")).toBe(0);
    expect(vi.getTimerCount()).toBe(0);
  });

  it.each(["closed", "dispose"])("%s 会取消等待且迟到首帧不能再报告成功", async reason => {
    vi.useFakeTimers();
    const window = new EventEmitter();
    const firstPaint = watchWindowFirstPaint(window as never);
    const outcome = expect(firstPaint.ready).rejects.toThrow(reason === "closed" ? "关闭" : "取消");
    if (reason === "closed") window.emit("closed");
    else firstPaint.dispose();
    window.emit("ready-to-show");
    await outcome;
    expect(vi.getTimerCount()).toBe(0);
  });

  it("首帧永不回执时十秒超时并释放监听", async () => {
    vi.useFakeTimers();
    const window = new EventEmitter();
    const firstPaint = watchWindowFirstPaint(window as never);
    const outcome = expect(firstPaint.ready).rejects.toThrow("超时");
    await vi.advanceTimersByTimeAsync(10_000);
    await outcome;
    expect(window.listenerCount("ready-to-show")).toBe(0);
    expect(vi.getTimerCount()).toBe(0);
  });

  it("首帧先完成但页面加载挂起时仍会超时，迟到加载不能改成成功", async () => {
    vi.useFakeTimers();
    const window = new EventEmitter();
    let finishLoading!: () => void;
    const contentLoaded = new Promise<void>(resolve => { finishLoading = resolve; });
    const firstPaint = watchWindowFirstPaint(window as never, contentLoaded);
    const outcome = expect(firstPaint.ready).rejects.toThrow("超时");
    window.emit("ready-to-show");
    await vi.advanceTimersByTimeAsync(10_000);
    await outcome;
    finishLoading();
    await Promise.resolve();
    expect(window.listenerCount("closed")).toBe(0);
    expect(vi.getTimerCount()).toBe(0);
  });

  it("首帧已经到达时，页面加载失败仍按原始错误收口", async () => {
    const window = new EventEmitter();
    let failLoading!: (error: Error) => void;
    const contentLoaded = new Promise<void>((_resolve, reject) => { failLoading = reject; });
    const firstPaint = watchWindowFirstPaint(window as never, contentLoaded);
    const error = new Error("聊天页面加载失败");
    const outcome = expect(firstPaint.ready).rejects.toBe(error);
    window.emit("ready-to-show");
    failLoading(error);
    await outcome;
    expect(window.listenerCount("closed")).toBe(0);
  });
});
