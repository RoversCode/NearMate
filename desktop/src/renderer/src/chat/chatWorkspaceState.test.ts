import { describe, expect, it } from "vitest";
import {
  canAcceptWorkspaceSnapshot,
  projectEnabledSkills,
  retainActiveRequestIds
} from "./chatWorkspaceState";
import type { ActivitySnapshot, TimedReminderSnapshot } from "../types/backendEvents";

const ACTIVITY_RUNTIME_A = "runtime-a";
const ACTIVITY_RUNTIME_B = "runtime-b";
const REMINDER_RUNTIME_A = "runtime-reminder-a";
const REMINDER_RUNTIME_B = "runtime-reminder-b";

function activitySnapshot(revision: number, runtimeId: string): ActivitySnapshot {
  return {
    revision,
    runtime_id: runtimeId,
    active: [],
    recent: []
  };
}

function reminderSnapshot(revision: number, runtimeId: string): TimedReminderSnapshot {
  return {
    revision,
    runtime_id: runtimeId,
    active: [],
    recent: []
  };
}

describe("chatWorkspaceState", () => {
  it("拒绝同一运行期的 revision 回退，但接受 Backend 重启后的低 revision", () => {
    expect(canAcceptWorkspaceSnapshot({
      currentActivity: activitySnapshot(4, ACTIVITY_RUNTIME_A),
      nextActivity: activitySnapshot(3, ACTIVITY_RUNTIME_A),
      currentReminder: null
    })).toBe(false);

    expect(canAcceptWorkspaceSnapshot({
      currentActivity: activitySnapshot(4, ACTIVITY_RUNTIME_A),
      nextActivity: activitySnapshot(0, ACTIVITY_RUNTIME_B),
      currentReminder: null
    })).toBe(true);
  });

  it("同时对提醒快照执行运行期和 revision 门禁", () => {
    expect(canAcceptWorkspaceSnapshot({
      currentActivity: null,
      currentReminder: reminderSnapshot(7, REMINDER_RUNTIME_A),
      nextReminder: reminderSnapshot(6, REMINDER_RUNTIME_A)
    })).toBe(false);
    expect(canAcceptWorkspaceSnapshot({
      currentActivity: null,
      currentReminder: reminderSnapshot(7, REMINDER_RUNTIME_A),
      nextReminder: reminderSnapshot(0, REMINDER_RUNTIME_B)
    })).toBe(true);
  });

  it("只保留权威活动快照中仍然存在的请求占位", () => {
    expect(retainActiveRequestIds(
      new Set(["activity-a", "activity-b"]),
      new Set(["activity-b", "activity-c"])
    )).toEqual(new Set(["activity-b"]));
  });

  it("只投影启用且拥有安全展示合同的 Skill", () => {
    expect(projectEnabledSkills({
      skills: [
        { id: "web.search", name: "网页搜索", enabled: true, ready: true, usable: true },
        { id: "disabled", name: "不应展示", enabled: true, ready: true, usable: true },
        { id: "not-enabled", name: "偏好关闭", enabled: true, ready: true, usable: true },
        { id: "missing-name", enabled: true, ready: true, usable: true },
        { id: "internal-path", name: "C:\\secret\\skill", enabled: true, ready: true, usable: true },
        { id: "unsafe/id", name: "不应展示", enabled: true, ready: true, usable: true },
        { id: "not-ready", name: "暂不可用", enabled: true, ready: false, usable: false }
      ]
    }, {
      "web.search": true,
      disabled: false,
      "not-enabled": false,
      "missing-name": true,
        "internal-path": true,
        "unsafe/id": true,
      "not-ready": true
    })).toEqual([
      { id: "web.search", name: "网页搜索", ready: true, usable: true },
      { id: "not-ready", name: "暂不可用", ready: false, usable: false }
    ]);

    expect(projectEnabledSkills({ skills: [{ id: "web.search", name: "网页搜索", enabled: true }] }, {
      "web.search": true
    })).toEqual([]);
  });
});
