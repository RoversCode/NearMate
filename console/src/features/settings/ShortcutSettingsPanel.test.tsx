import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { createConsoleSettingsPayload } from "../../test/consoleAppTestHarness";
import type { ConsoleSettings } from "../../api/contracts/settings";
import type { ShortcutStatusPayload } from "../../api/contracts/shortcutStatus";
import { ShortcutSettingsPanel } from "./ShortcutSettingsPanel";

function createStatusPayload(settingsRevision: number): ShortcutStatusPayload {
  return {
    items: [
      {
        shortcut_id: "followup_toggle",
        accelerator: "CommandOrControl+Alt+Space",
        enabled: true,
        status: "registered",
        settings_revision: settingsRevision,
        checked_at: "2026-10-07T19:00:00+08:00",
        message: "快捷键可用"
      },
      {
        shortcut_id: "avatar_visibility",
        accelerator: "CommandOrControl+Q",
        enabled: true,
        status: "registered",
        settings_revision: settingsRevision,
        checked_at: "2026-10-07T19:00:00+08:00",
        message: "快捷键可用"
      },
      {
        shortcut_id: "selection_capture",
        accelerator: "CommandOrControl+Alt+C",
        enabled: true,
        status: "registered",
        settings_revision: settingsRevision,
        checked_at: "2026-10-07T19:00:00+08:00",
        message: "快捷键可用"
      }
    ]
  };
}

function renderPanel(settingsRevision: number, statusRevision: number): void {
  render(
    <ShortcutSettingsPanel
      isSaving={false}
      onSave={async () => undefined}
      settings={{
        ...createConsoleSettingsPayload(),
        settings_revision: settingsRevision
      } as ConsoleSettings}
      shortcutStatus={createStatusPayload(statusRevision)}
    />
  );
}

describe("快捷键状态版本对齐", () => {
  it("旧 revision 的状态不会冒充当前设置已完成注册", () => {
    renderPanel(2, 1);

    expect(screen.getAllByText("待检测")).toHaveLength(3);
    expect(screen.queryAllByText("可用")).toHaveLength(0);
  });

  it("当前 revision 的状态可以正常显示为可用", () => {
    renderPanel(2, 2);

    expect(screen.getAllByText("可用")).toHaveLength(3);
  });
});
