import { describe, expect, it } from "vitest";
import type { ActivitySnapshot } from "../types/backendEvents";
import { shouldAcceptActivitySnapshot } from "./activitySnapshotAcceptance";

const RUNTIME_A = "00000000-0000-4000-8000-0000000000aa";
const RUNTIME_B = "00000000-0000-4000-8000-0000000000bb";

function snapshot(revision: number, runtimeId: string): ActivitySnapshot {
  return { revision, runtime_id: runtimeId, active: [], recent: [] };
}

describe("shouldAcceptActivitySnapshot", () => {
  it("当前无快照时接受", () => {
    expect(shouldAcceptActivitySnapshot(null, snapshot(0, RUNTIME_A))).toBe(true);
  });

  it("runtime 不同无条件替换，即使 revision 更小", () => {
    expect(shouldAcceptActivitySnapshot(snapshot(9, RUNTIME_A), snapshot(0, RUNTIME_B))).toBe(true);
  });

  it("同一 runtime 只接受更高 revision", () => {
    expect(shouldAcceptActivitySnapshot(snapshot(3, RUNTIME_A), snapshot(4, RUNTIME_A))).toBe(true);
    expect(shouldAcceptActivitySnapshot(snapshot(3, RUNTIME_A), snapshot(3, RUNTIME_A))).toBe(false);
    expect(shouldAcceptActivitySnapshot(snapshot(3, RUNTIME_A), snapshot(2, RUNTIME_A))).toBe(false);
  });
});
