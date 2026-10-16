import { describe, expect, it } from "vitest";
import { WINDOW_KINDS } from "../contracts/windowKinds";
import { windowEntryInputs } from "./windowEntries";

describe("windowEntryInputs", () => {
  it("声明单一 sandbox preload 和三个 renderer 入口", () => {
    expect(windowEntryInputs.preload).toEqual({
      sandbox: expect.stringMatching(/src\/preload\/index\.ts$/)
    });
    expect(Object.keys(windowEntryInputs.renderer)).toEqual(WINDOW_KINDS);
    expect(windowEntryInputs.renderer.avatar.endsWith("src/renderer/avatar.html")).toBe(true);
    expect(windowEntryInputs.renderer.panel.endsWith("src/renderer/panel.html")).toBe(true);
    expect(windowEntryInputs.renderer.chat.endsWith("src/renderer/chat.html")).toBe(true);
  });
});
