// @vitest-environment jsdom

import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useMaterialFeedback } from "./useMaterialFeedback";

describe("useMaterialFeedback", () => {
  let container: HTMLDivElement;
  let root: ReturnType<typeof createRoot>;
  let latest: ReturnType<typeof useMaterialFeedback> | null;

  beforeEach(() => {
    vi.useFakeTimers();
    (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
    latest = null;
    act(() => root.render(<Probe />));
  });

  afterEach(() => {
    act(() => root.unmount());
    expect(vi.getTimerCount()).toBe(0);
    vi.useRealTimers();
    container.remove();
  });

  it("处理中反馈持续展示，最终反馈三秒后自动清理", () => {
    act(() => latest?.show({ kind: "progress", text: "正在检查 40 项…" }, false));
    act(() => vi.advanceTimersByTime(10_000));
    expect(container.textContent).toBe("正在检查 40 项…");

    act(() => latest?.show({ kind: "success", text: "已添加 40 项" }));
    act(() => vi.advanceTimersByTime(2_999));
    expect(container.textContent).toBe("已添加 40 项");
    act(() => vi.advanceTimersByTime(1));
    expect(container.textContent).toBe("");
  });

  it("新反馈替换旧反馈并重新计算清理时间", () => {
    act(() => latest?.show({ kind: "success", text: "已添加 1 项" }));
    act(() => vi.advanceTimersByTime(2_000));
    act(() => latest?.show({ kind: "warning", text: "1 项无法读取，未添加" }));
    act(() => vi.advanceTimersByTime(1_500));

    expect(container.textContent).toBe("1 项无法读取，未添加");
  });

  function Probe() {
    latest = useMaterialFeedback();
    return <div>{latest.feedback?.text ?? ""}</div>;
  }
});
