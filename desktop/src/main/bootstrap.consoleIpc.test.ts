import { describe, expect, it, vi } from "vitest";
import { setupBootstrapHarness } from "./bootstrap.testHarness";
describe("bootstrap console IPC", () => {
  it.each([
    {
      name: "环境变量缺失时打开默认本地 console 页面",
      controlHttpUrl: undefined,
      expectedUrl: "http://127.0.0.1:8765/console?section=settings"
    },
    {
      name: "环境变量是服务根路径且带末尾斜杠时补齐 console 页面",
      controlHttpUrl: "http://127.0.0.1:8765/",
      expectedUrl: "http://127.0.0.1:8765/console?section=settings"
    },
    {
      name: "环境变量误配为 console 页面时先还原根地址再打开 console 页面",
      controlHttpUrl: "http://127.0.0.1:9000/console",
      expectedUrl: "http://127.0.0.1:9000/console?section=settings"
    },
    {
      name: "环境变量误配为带斜杠的 console 页面时先还原根地址再打开 console 页面",
      controlHttpUrl: "http://127.0.0.1:9000/console/",
      expectedUrl: "http://127.0.0.1:9000/console?section=settings"
    }
  ])("desktop:open-console 会归一化控制台地址：$name", async ({
    controlHttpUrl,
    expectedUrl
  }) => {
    const { ipcMainMock, openExternal } = await setupBootstrapHarness({
      controlHttpUrl
    });

    ipcMainMock.emit("desktop:open-console", undefined, { section: "settings" });

    expect(openExternal).toHaveBeenCalledWith(expectedUrl);
  });

  it("desktop:open-console 打开失败时会记录错误且不向 send 监听器外抛拒绝", async () => {
    const openConsoleError = new Error("浏览器打开失败");
    const consoleErrorSpy = vi.spyOn(console, "error").mockImplementation(() => undefined);
    const {
      ipcMainMock,
      openExternal
    } = await setupBootstrapHarness();
    openExternal.mockRejectedValueOnce(openConsoleError);

    const listenerResults = ipcMainMock.emit(
      "desktop:open-console",
      undefined,
      { section: "settings" }
    );

    await expect(Promise.all(listenerResults)).resolves.toEqual([undefined]);
    expect(consoleErrorSpy).toHaveBeenCalledWith(
      "[companion-main] open-console failed",
      openConsoleError
    );
  });

  it("重复 bootstrap 不会累积 desktop:open-console 监听器", async () => {
    const {
      bootstrap,
      ipcMainMock,
      openExternal
    } = await setupBootstrapHarness();

    await bootstrap();
    ipcMainMock.emit("desktop:open-console", undefined, { section: "settings" });

    expect(ipcMainMock.removeAllListeners).toHaveBeenCalledWith("desktop:open-console");
    expect(ipcMainMock.removeHandler).toHaveBeenCalledWith("livekit:create-session");
    expect(openExternal).toHaveBeenCalledTimes(1);
  });

});
