import { describe, expect, it } from "vitest";
import { createWindowIdentityRegistry } from "./windowIdentityRegistry";

describe("window identity registry", () => {
  it("只按主进程登记的 WebContents 对象身份解析窗口类型", () => {
    const registry = createWindowIdentityRegistry();
    const avatarSender = {} as Electron.WebContents;
    const panelSender = {} as Electron.WebContents;
    const unknownSender = {} as Electron.WebContents;

    registry.bind(avatarSender, "avatar");
    registry.bind(panelSender, "panel");

    expect(registry.resolve(avatarSender)).toBe("avatar");
    expect(registry.resolve(panelSender)).toBe("panel");
    expect(registry.resolve(unknownSender)).toBeNull();
  });
});
