import { describe, expect, it, vi } from "vitest";
import { applyWindowNavigationPolicy } from "./windowNavigationPolicy";

describe("window navigation policy", () => {
  it("拒绝新窗口和未知导航，只允许当前 renderer 入口", () => {
    const listeners = new Map<string, (event: { preventDefault(): void }, url: string) => void>();
    const window = {
      webContents: {
        setWindowOpenHandler: vi.fn(),
        on: vi.fn((eventName: string, listener: (event: { preventDefault(): void }, url: string) => void) => {
          listeners.set(eventName, listener);
        })
      }
    };

    applyWindowNavigationPolicy(
      window as never,
      "http://127.0.0.1:5173/avatar.html"
    );

    expect(window.webContents.setWindowOpenHandler.mock.calls[0]?.[0]()).toEqual({
      action: "deny"
    });

    const trustedEvent = { preventDefault: vi.fn() };
    listeners.get("will-navigate")?.(
      trustedEvent,
      "http://127.0.0.1:5173/avatar.html#current"
    );
    expect(trustedEvent.preventDefault).not.toHaveBeenCalled();

    const externalEvent = { preventDefault: vi.fn() };
    listeners.get("will-redirect")?.(externalEvent, "https://example.com/login");
    expect(externalEvent.preventDefault).toHaveBeenCalledOnce();
  });
});
