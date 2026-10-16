import { afterEach, describe, expect, it, vi } from "vitest";
import { createAvatarFirstShowGate } from "./avatarFirstShowGate";

afterEach(() => vi.useRealTimers());

describe("安装版首显交接", () => {
  it("阻挡计时兜底和展示档案首显，放行时只显示一次", async () => {
    vi.useFakeTimers();
    const window = { show: vi.fn(), isDestroyed: () => false };
    const gate = createAvatarFirstShowGate();
    gate.hold();
    gate.schedule(window as never);
    gate.showIfNeeded(window as never);
    await vi.advanceTimersByTimeAsync(500);
    expect(window.show).not.toHaveBeenCalled();
    gate.release();
    gate.release();
    expect(window.show).toHaveBeenCalledOnce();
  });

  it("失败收口清除待显示窗口，迟到放行不能复活旧窗口", () => {
    const window = { show: vi.fn(), isDestroyed: () => false };
    const gate = createAvatarFirstShowGate();
    gate.hold();
    gate.showIfNeeded(window as never);
    gate.reset();
    gate.release();
    expect(window.show).not.toHaveBeenCalled();
  });
});
