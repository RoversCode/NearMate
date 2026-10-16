import { describe, expect, it } from "vitest";
import type { TimedReminderSnapshot } from "../types/backendEvents";
import { shouldAcceptTimedReminderSnapshot } from "./timedReminderSnapshotAcceptance";

const RUNTIME_A = "00000000-0000-4000-8000-0000000000aa";
const RUNTIME_B = "00000000-0000-4000-8000-0000000000bb";

function snapshot(revision: number, runtimeId: string): TimedReminderSnapshot {
  return { revision, runtime_id: runtimeId, active: [], recent: [] };
}

describe("shouldAcceptTimedReminderSnapshot", () => {
  it("首帧和新 Backend 运行期无条件接管", () => {
    expect(shouldAcceptTimedReminderSnapshot(null, snapshot(0, RUNTIME_A))).toBe(true);
    expect(
      shouldAcceptTimedReminderSnapshot(snapshot(9, RUNTIME_A), snapshot(0, RUNTIME_B))
    ).toBe(true);
  });

  it("同一运行期只接受更高版本", () => {
    expect(
      shouldAcceptTimedReminderSnapshot(snapshot(3, RUNTIME_A), snapshot(4, RUNTIME_A))
    ).toBe(true);
    expect(
      shouldAcceptTimedReminderSnapshot(snapshot(3, RUNTIME_A), snapshot(3, RUNTIME_A))
    ).toBe(false);
    expect(
      shouldAcceptTimedReminderSnapshot(snapshot(3, RUNTIME_A), snapshot(2, RUNTIME_A))
    ).toBe(false);
  });
});
