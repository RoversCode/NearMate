import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import { resolveWindowPreloadPaths } from "./windowContent";

const temporaryDirectories: string[] = [];

afterEach(() => {
  for (const directory of temporaryDirectories.splice(0)) {
    rmSync(directory, { recursive: true, force: true });
  }
});

describe("resolveWindowPreloadPaths", () => {
  it("三个 sandbox 窗口共用一个自包含 CommonJS preload", () => {
    const buildRoot = mkdtempSync(join(tmpdir(), "nearmate-preload-"));
    temporaryDirectories.push(buildRoot);
    const mainModuleDir = join(buildRoot, "main");
    const preloadDir = join(buildRoot, "preload");
    const sandboxPreloadPath = join(preloadDir, "sandbox.cjs");
    mkdirSync(mainModuleDir, { recursive: true });
    mkdirSync(preloadDir, { recursive: true });
    writeFileSync(sandboxPreloadPath, "");

    expect(resolveWindowPreloadPaths(mainModuleDir)).toEqual({
      avatar: sandboxPreloadPath,
      panel: sandboxPreloadPath,
      chat: sandboxPreloadPath
    });
  });
});
