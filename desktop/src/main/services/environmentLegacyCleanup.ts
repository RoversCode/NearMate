import { rm } from "node:fs/promises";
import { join } from "node:path";

/**
 * 异步删除旧版环境截图专用目录。
 *
 * Args:
 *   userDataDir: Electron 当前应用专属数据根目录。
 *
 * Returns:
 *   两个专用历史目录都完成清理后的 Promise。
 */
export async function cleanupLegacyEnvironmentSnapshots(userDataDir: string): Promise<void> {
  await Promise.all([
    rm(join(userDataDir, "turn-snapshots"), { recursive: true, force: true }),
    rm(join(userDataDir, "environment-snapshot"), { recursive: true, force: true })
  ]);
}
