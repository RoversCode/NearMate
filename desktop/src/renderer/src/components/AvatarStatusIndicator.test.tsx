// @vitest-environment jsdom

import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { CompanionStatusPresentation } from "../companion/companionStatus";
import { AvatarStatusIndicator } from "./AvatarStatusIndicator";

const READY_STATUS: CompanionStatusPresentation = {
  state: "available",
  label: "可以继续说",
  description: "角色当前可以接受表达",
  wakeMode: "followup",
  wakeLabel: "跟进模式已开启",
  manualToggleRevision: 0
};
const PET_BOUNDS = { x: 100, y: 40, width: 200, height: 400 };

describe("AvatarStatusIndicator", () => {
  let container: HTMLDivElement;
  let root: ReturnType<typeof createRoot>;

  beforeEach(() => {
    (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
    vi.useRealTimers();
  });

  it("只展示图案，并以无障碍名称说明状态，不增加文字、悬浮提示或点击热区", () => {
    act(() => root.render(<AvatarStatusIndicator status={READY_STATUS} petBounds={PET_BOUNDS} />));

    const indicator = container.querySelector<HTMLElement>("[data-avatar-status]");
    expect(indicator?.dataset.avatarStatus).toBe("available");
    expect(indicator?.dataset.avatarWakeMode).toBe("followup");
    expect(indicator?.getAttribute("aria-label")).toContain("跟进模式已开启");
    expect(indicator?.style.left).toBe("260px");
    expect(indicator?.style.top).toBe("142px");
    expect(container.textContent).toBe("");
    expect(container.querySelector("[title], button, [data-avatar-interactive]")).toBeNull();
    expect(indicator?.querySelector("svg")).not.toBeNull();
  });

  it("把图案钳在四边工作区内，人物或可见空间不足时收起", () => {
    const visibleArea = { visibleLeft: 220, visibleRight: 360, visibleTop: 180, visibleBottom: 320 };
    act(() => root.render(<AvatarStatusIndicator status={READY_STATUS} petBounds={PET_BOUNDS} visibleArea={visibleArea} />));
    let indicator = container.querySelector<HTMLElement>("[data-avatar-status]");
    expect(indicator?.style.top).toBe("204px");

    act(() => root.render(
      <AvatarStatusIndicator
        status={READY_STATUS}
        petBounds={{ x: 700, y: 800, width: 300, height: 500 }}
        visibleArea={visibleArea}
      />
    ));
    indicator = container.querySelector<HTMLElement>("[data-avatar-status]");
    expect(indicator?.style.left).toBe("336px");
    expect(indicator?.style.top).toBe("296px");

    act(() => root.render(<AvatarStatusIndicator status={READY_STATUS} petBounds={null} />));
    expect(container.querySelector("[data-avatar-status]")).toBeNull();
    act(() => root.render(
      <AvatarStatusIndicator status={READY_STATUS} petBounds={PET_BOUNDS} visibleArea={{ ...visibleArea, visibleRight: 240 }} />
    ));
    expect(container.querySelector("[data-avatar-status]")).toBeNull();
  });

  it("首次水合不强调，后端确认手动切换后强调两秒，重复版本不重新触发", () => {
    vi.useFakeTimers();
    act(() => root.render(<AvatarStatusIndicator status={{ ...READY_STATUS, manualToggleRevision: 12 }} petBounds={PET_BOUNDS} />));
    expect(container.querySelector(".avatar-status-indicator--emphasized")).toBeNull();

    const nextStatus = { ...READY_STATUS, wakeMode: "wake_required" as const, manualToggleRevision: 13 };
    act(() => root.render(<AvatarStatusIndicator status={nextStatus} petBounds={PET_BOUNDS} />));
    expect(container.querySelector(".avatar-status-indicator--emphasized")).not.toBeNull();
    act(() => vi.advanceTimersByTime(1900));
    act(() => root.render(<AvatarStatusIndicator status={{ ...nextStatus, state: "thinking" }} petBounds={PET_BOUNDS} />));
    act(() => vi.advanceTimersByTime(100));
    expect(container.querySelector(".avatar-status-indicator--emphasized")).toBeNull();

    act(() => root.render(<AvatarStatusIndicator status={{ ...READY_STATUS, manualToggleRevision: 2 }} petBounds={PET_BOUNDS} />));
    expect(container.querySelector(".avatar-status-indicator--emphasized")).toBeNull();
  });

  it("普通初始快照没有手动版本时，第一次真实手动切换仍会给出确认光环", () => {
    act(() => root.render(<AvatarStatusIndicator status={{ ...READY_STATUS, manualToggleRevision: null }} petBounds={PET_BOUNDS} />));
    expect(container.querySelector(".avatar-status-indicator--emphasized")).toBeNull();
    act(() => root.render(
      <AvatarStatusIndicator status={{ ...READY_STATUS, wakeMode: "wake_required", manualToggleRevision: 1 }} petBounds={PET_BOUNDS} />
    ));
    expect(container.querySelector(".avatar-status-indicator--emphasized")).not.toBeNull();
  });

  it("连续确认只重建光环以重新播放动画，普通状态变化不重播", () => {
    vi.useFakeTimers();
    act(() => root.render(<AvatarStatusIndicator status={READY_STATUS} petBounds={PET_BOUNDS} />));
    act(() => root.render(<AvatarStatusIndicator status={{ ...READY_STATUS, manualToggleRevision: 1 }} petBounds={PET_BOUNDS} />));
    const indicator = container.querySelector(".avatar-status-indicator");
    const firstHalo = container.querySelector(".avatar-status-halo");
    act(() => vi.advanceTimersByTime(700));
    const nextStatus = { ...READY_STATUS, manualToggleRevision: 2 };
    act(() => root.render(<AvatarStatusIndicator status={nextStatus} petBounds={PET_BOUNDS} />));
    const nextHalo = container.querySelector(".avatar-status-halo");
    expect(nextHalo).not.toBe(firstHalo);
    expect(container.querySelector(".avatar-status-indicator")).toBe(indicator);
    act(() => root.render(<AvatarStatusIndicator status={{ ...nextStatus, state: "listening" }} petBounds={PET_BOUNDS} />));
    expect(container.querySelector(".avatar-status-halo")).toBe(nextHalo);
    act(() => vi.advanceTimersByTime(1999));
    expect(container.querySelector(".avatar-status-indicator--emphasized")).not.toBeNull();
    act(() => vi.advanceTimersByTime(1));
    expect(container.querySelector(".avatar-status-indicator--emphasized")).toBeNull();
  });

  it("手动确认后正常唤醒模式变化不会取消或延长两秒强调", () => {
    vi.useFakeTimers();
    act(() => root.render(<AvatarStatusIndicator status={READY_STATUS} petBounds={PET_BOUNDS} />));
    const toggledStatus = { ...READY_STATUS, wakeMode: "wake_required" as const, manualToggleRevision: 1 };
    act(() => root.render(<AvatarStatusIndicator status={toggledStatus} petBounds={PET_BOUNDS} />));
    expect(container.querySelector(".avatar-status-indicator--emphasized")).not.toBeNull();

    act(() => vi.advanceTimersByTime(700));
    act(() => root.render(
      <AvatarStatusIndicator
        status={{ ...toggledStatus, state: "listening", wakeMode: "followup" }}
        petBounds={PET_BOUNDS}
      />
    ));
    expect(container.querySelector(".avatar-status-indicator--emphasized")).not.toBeNull();
    act(() => vi.advanceTimersByTime(1299));
    expect(container.querySelector(".avatar-status-indicator--emphasized")).not.toBeNull();
    act(() => vi.advanceTimersByTime(1));
    expect(container.querySelector(".avatar-status-indicator--emphasized")).toBeNull();
  });

  it("连接状态未知时立即清掉强调，新连接水合后重新建立手动版本基线", () => {
    vi.useFakeTimers();
    act(() => root.render(<AvatarStatusIndicator status={READY_STATUS} petBounds={PET_BOUNDS} />));
    act(() => root.render(
      <AvatarStatusIndicator status={{ ...READY_STATUS, manualToggleRevision: 15 }} petBounds={PET_BOUNDS} />
    ));
    expect(container.querySelector(".avatar-status-indicator--emphasized")).not.toBeNull();
    act(() => vi.advanceTimersByTime(700));

    act(() => root.render(
      <AvatarStatusIndicator
        status={{ ...READY_STATUS, state: "syncing", wakeMode: "unknown", manualToggleRevision: null }}
        petBounds={PET_BOUNDS}
      />
    ));
    expect(container.querySelector(".avatar-status-indicator--emphasized")).toBeNull();
    act(() => root.render(<AvatarStatusIndicator status={READY_STATUS} petBounds={PET_BOUNDS} />));
    expect(container.querySelector(".avatar-status-indicator--emphasized")).toBeNull();
    act(() => root.render(
      <AvatarStatusIndicator status={{ ...READY_STATUS, manualToggleRevision: 1 }} petBounds={PET_BOUNDS} />
    ));
    expect(container.querySelector(".avatar-status-indicator--emphasized")).not.toBeNull();

    act(() => vi.advanceTimersByTime(1300));
    expect(container.querySelector(".avatar-status-indicator--emphasized")).not.toBeNull();
    act(() => vi.advanceTimersByTime(700));
    expect(container.querySelector(".avatar-status-indicator--emphasized")).toBeNull();
  });

  it.each([1, 12])("新输入 Runtime 的手动版本 %s 不受旧 Runtime 版本基线影响", revision => {
    vi.useFakeTimers();
    const previousRuntimeStatus = { ...READY_STATUS, inputRuntimeId: "runtime-old", manualToggleRevision: 12 };
    act(() => root.render(<AvatarStatusIndicator status={previousRuntimeStatus} petBounds={PET_BOUNDS} />));
    expect(container.querySelector(".avatar-status-indicator--emphasized")).toBeNull();

    // React 可能合并 Runtime 更替与快捷键回执；没有中间 unknown 渲染也必须识别新版本轴。
    const newRuntimeStatus = { ...READY_STATUS, inputRuntimeId: "runtime-new", manualToggleRevision: revision };
    act(() => root.render(<AvatarStatusIndicator status={newRuntimeStatus} petBounds={PET_BOUNDS} />));
    expect(container.querySelector(".avatar-status-indicator--emphasized")).not.toBeNull();
    act(() => vi.advanceTimersByTime(2000));
    expect(container.querySelector(".avatar-status-indicator--emphasized")).toBeNull();
  });
});
