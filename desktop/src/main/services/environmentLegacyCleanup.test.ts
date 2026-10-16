import { mkdir, mkdtemp, rm, stat } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { cleanupLegacyEnvironmentSnapshots } from "./environmentLegacyCleanup";

describe("cleanupLegacyEnvironmentSnapshots", () => {
  it("只删除应用专属的两个旧环境目录", async () => {
    const rootDir = await mkdtemp(join(tmpdir(), "environment-legacy-cleanup-"));
    const turnSnapshotsDir = join(rootDir, "turn-snapshots");
    const environmentSnapshotDir = join(rootDir, "environment-snapshot");
    const unrelatedDir = join(rootDir, "user-materials");
    await Promise.all([
      mkdir(turnSnapshotsDir, { recursive: true }),
      mkdir(environmentSnapshotDir, { recursive: true }),
      mkdir(unrelatedDir, { recursive: true })
    ]);

    try {
      await cleanupLegacyEnvironmentSnapshots(rootDir);

      await expect(stat(turnSnapshotsDir)).rejects.toMatchObject({ code: "ENOENT" });
      await expect(stat(environmentSnapshotDir)).rejects.toMatchObject({ code: "ENOENT" });
      await expect(stat(unrelatedDir)).resolves.toBeDefined();
    } finally {
      await rm(rootDir, { recursive: true, force: true });
    }
  });
});
