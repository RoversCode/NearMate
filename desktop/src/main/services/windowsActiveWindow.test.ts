import { beforeEach, describe, expect, it, vi } from "vitest";
import { createActiveWindowTitleProvider } from "./windowsActiveWindow";

const koffiMock = vi.hoisted(() => {
  const getForegroundWindow = vi.fn(() => ({ handle: true }));
  const getWindowText = vi.fn((_window: unknown, buffer: Buffer) => {
    buffer.write("Code", "utf16le");
    return 4;
  });
  const func = vi
    .fn()
    .mockReturnValueOnce(getForegroundWindow)
    .mockReturnValueOnce(getWindowText);
  return {
    load: vi.fn(() => ({ func })),
    func,
    getForegroundWindow,
    getWindowText
  };
});

describe("createActiveWindowTitleProvider", () => {
  beforeEach(() => {
    koffiMock.load.mockClear();
    koffiMock.getForegroundWindow.mockClear();
    koffiMock.getWindowText.mockClear();
  });

  it("首次读取时才加载 Win32 API 并复用函数声明", () => {
    const provider = createActiveWindowTitleProvider("win32", () => ({ load: koffiMock.load as never }));

    expect(koffiMock.load).not.toHaveBeenCalled();
    expect(provider.readTitle()).toBe("Code");
    expect(provider.readTitle()).toBe("Code");
    expect(koffiMock.load).toHaveBeenCalledOnce();
    expect(koffiMock.getWindowText).toHaveBeenCalledTimes(2);
  });

  it("非 Windows 平台不加载原生库", () => {
    const provider = createActiveWindowTitleProvider("linux", () => ({ load: koffiMock.load as never }));

    expect(provider.readTitle()).toBeNull();
    expect(koffiMock.load).not.toHaveBeenCalled();
  });
});
