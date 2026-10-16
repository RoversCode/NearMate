import { describe, expect, it, vi } from "vitest";
import { createAuthorizedIpcMain } from "./authorizedIpcMain";
import { createWindowIdentityRegistry } from "./windowIdentityRegistry";

describe("authorized IPC main", () => {
  it("send/on 只执行来源窗口被允许的消息", () => {
    const on = vi.fn();
    const authorizedIpcMain = createAuthorizedIpcMain({
      on,
      handle: vi.fn()
    } as never, createRegistryWithSenders());
    const listener = vi.fn();

    authorizedIpcMain.on("avatar:hover-enter", ["avatar"], listener);
    const registeredListener = on.mock.calls[0]?.[1] as (
      event: { sender: Electron.WebContents },
      payload: unknown
    ) => void;
    registeredListener({ sender: panelSender }, { source: "panel" });
    registeredListener({ sender: avatarSender }, { source: "avatar" });

    expect(listener).toHaveBeenCalledOnce();
    expect(listener).toHaveBeenCalledWith(
      expect.objectContaining({ sender: avatarSender }),
      { source: "avatar" }
    );
  });

  it("invoke/handle 对越权窗口明确拒绝，只执行获准调用", async () => {
    const handle = vi.fn();
    const authorizedIpcMain = createAuthorizedIpcMain({
      on: vi.fn(),
      handle
    } as never, createRegistryWithSenders());
    const handler = vi.fn(async () => ({ accepted: true }));

    authorizedIpcMain.handle("turn-submission:reserve", ["chat"], handler);
    const registeredHandler = handle.mock.calls[0]?.[1] as (
      event: { sender: Electron.WebContents }
    ) => unknown;

    expect(() => registeredHandler({ sender: avatarSender })).toThrow(
      "unauthorized IPC sender for channel: turn-submission:reserve"
    );
    await expect(registeredHandler({ sender: chatSender })).resolves.toEqual({
      accepted: true
    });
    expect(handler).toHaveBeenCalledOnce();
  });
});

const avatarSender = {} as Electron.WebContents;
const panelSender = {} as Electron.WebContents;
const chatSender = {} as Electron.WebContents;

/**
 * createRegistryWithSenders 创建包含三类可信测试窗口的身份表。
 *
 * Returns:
 *   已登记 Avatar、Panel 和 Chat sender 的 Registry。
 */
function createRegistryWithSenders() {
  const registry = createWindowIdentityRegistry();
  registry.bind(avatarSender, "avatar");
  registry.bind(panelSender, "panel");
  registry.bind(chatSender, "chat");
  return registry;
}
