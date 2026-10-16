import { describe, expect, it } from "vitest";

import { createSkillLibraryResponse } from "../../test/skillFixtures";
import { createConsoleSettingsPayload } from "../../test/consoleAppTestHarness";
import { isShortcutStatusPayload } from "./shortcutStatus";
import { isSkillLibraryResponse } from "./skills";
import { isConsoleSettings } from "./settings";

const UNSAFE_SETTINGS_REVISION = Number.MAX_SAFE_INTEGER + 1;

describe("settings revision contracts", () => {
  it("普通设置快照拒绝超出安全整数范围的版本", () => {
    expect(isConsoleSettings({
      ...createConsoleSettingsPayload(),
      settings_revision: UNSAFE_SETTINGS_REVISION
    })).toBe(false);
  });

  it("快捷键状态快照拒绝超出安全整数范围的版本", () => {
    expect(isShortcutStatusPayload({
      items: [{
        shortcut_id: "followup_toggle",
        accelerator: "CommandOrControl+Alt+Space",
        enabled: true,
        status: "registered",
        settings_revision: UNSAFE_SETTINGS_REVISION,
        checked_at: "2026-08-12T10:00:00+08:00",
        message: "快捷键可用"
      }]
    })).toBe(false);
  });

  it("技能库快照拒绝超出安全整数范围的版本", () => {
    expect(isSkillLibraryResponse(createSkillLibraryResponse({
      revision: UNSAFE_SETTINGS_REVISION
    }))).toBe(false);
  });

  it("三个契约都接受安全整数上限", () => {
    expect(isConsoleSettings({
      ...createConsoleSettingsPayload(),
      settings_revision: Number.MAX_SAFE_INTEGER
    })).toBe(true);
    expect(isShortcutStatusPayload({
      items: [{
        shortcut_id: "followup_toggle",
        accelerator: "CommandOrControl+Alt+Space",
        enabled: true,
        status: "registered",
        settings_revision: Number.MAX_SAFE_INTEGER,
        checked_at: "2026-08-12T10:00:00+08:00",
        message: "快捷键可用"
      }]
    })).toBe(true);
    expect(isSkillLibraryResponse(createSkillLibraryResponse({
      revision: Number.MAX_SAFE_INTEGER
    }))).toBe(true);
  });
});
