import { describe, expect, it } from "vitest";
import {
  buildConsoleTargetUrl,
  readConsoleTarget,
  readPanelView
} from "./shellNavigation";

describe("shellNavigation", () => {
  it("只接受 models 面板视图对象", () => {
    expect(readPanelView({ view: "tasks" })).toBeNull();
    expect(readPanelView({ view: "models" })).toBe("models");
    expect(readPanelView("tasks")).toBeNull();
    expect(readPanelView({ view: "console" })).toBeNull();
  });

  it("只接受 settings/tasks 和合法 UUID，不允许 renderer 注入地址", () => {
    expect(readConsoleTarget({ section: "settings", url: "https://evil.test" })).toEqual({
      section: "settings"
    });
    expect(readConsoleTarget({ section: "tasks" })).toEqual({ section: "tasks" });
    expect(
      readConsoleTarget({
        section: "tasks",
        activityId: "01890F3E-7CC2-7D00-8ABC-1234567890AB"
      })
    ).toEqual({
      section: "tasks",
      activityId: "01890f3e-7cc2-7d00-8abc-1234567890ab"
    });
    expect(readConsoleTarget({ section: "tasks", activityId: "../settings" })).toBeNull();
    expect(readConsoleTarget({ section: "logs" })).toBeNull();
  });

  it("主进程从白名单目标拼接本地 Console 深链接", () => {
    expect(
      buildConsoleTargetUrl("http://127.0.0.1:8765/console", {
        section: "tasks",
        activityId: "123e4567-e89b-42d3-a456-426614174000"
      })
    ).toBe(
      "http://127.0.0.1:8765/console?section=tasks&activity=123e4567-e89b-42d3-a456-426614174000"
    );
  });
});
