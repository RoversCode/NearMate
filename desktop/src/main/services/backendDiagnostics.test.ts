import { mkdtemp, readFile, stat, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { appendBackendDiagnostic } from "./backendDiagnostics";

describe("后台诊断日志", () => {
  it("按 UTF-8 字节限制超大输出，保留完整的最新文字", async () => {
    const directory = await mkdtemp(join(tmpdir(), "nearmate-backend-log-"));
    const path = join(directory, "logs", "backend-startup.log");
    appendBackendDiagnostic(path, `${"初".repeat(800_000)}最新故障`);
    const content = await readFile(path, "utf8");
    expect((await stat(path)).size).toBeLessThanOrEqual(2 * 1024 * 1024);
    expect(content.endsWith("最新故障")).toBe(true);
    expect(content.includes("\uFFFD")).toBe(false);
  });

  it("多次写满只保留最近的备份，后续应用启动仍能继续记录", async () => {
    const directory = await mkdtemp(join(tmpdir(), "nearmate-backend-log-"));
    const path = join(directory, "backend-startup.log");
    const limit = 2 * 1024 * 1024;
    appendBackendDiagnostic(path, "a".repeat(limit));
    appendBackendDiagnostic(path, "b".repeat(limit));
    appendBackendDiagnostic(path, "new launch\n");
    expect(await readFile(path, "utf8")).toBe("new launch\n");
    expect((await readFile(`${path}.1`, "utf8")) === "b".repeat(limit)).toBe(true);
  });

  it("日志目录不可用时不向进程监督器抛异常", async () => {
    const directory = await mkdtemp(join(tmpdir(), "nearmate-backend-log-"));
    const blockedDirectory = join(directory, "occupied");
    await writeFile(blockedDirectory, "this is a file");
    expect(() => appendBackendDiagnostic(join(blockedDirectory, "backend-startup.log"), "failure")).not.toThrow();
  });
});
