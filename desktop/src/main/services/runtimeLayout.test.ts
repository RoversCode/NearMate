import { mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { readRuntimeManifest, resolveNearMateRuntimeLayout } from "./runtimeLayout";

function createManifest(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    schema_version: 3,
    product: "NearMate",
    platform: "windows-x64",
    python_version: "3.12.13",
    profile: "cuda",
    paths: {
      python: "python/Scripts/python.exe",
      backend: "backend",
      native: "native",
      shared: "shared",
      console: "console/dist",
      aec_package: "python/Lib/site-packages/audio_aec_sidecar",
      model_catalog: "model-catalog.json",
      seed_character_cards: "backend/seed-character-cards",
      assets: "assets"
    },
    required_files: ["python/Scripts/python.exe", "assets/icon.png"],
    files: [
      { path: "python/Scripts/python.exe", size: 0, sha256: "0".repeat(64) },
      { path: "assets/icon.png", size: 0, sha256: "0".repeat(64) }
    ],
    ...overrides
  };
}

function writeManifest(manifest: Record<string, unknown>): string {
  const root = mkdtempSync(join(tmpdir(), "nearmate-runtime-layout-"));
  const runtimeRoot = join(root, "runtime");
  mkdirSync(runtimeRoot, { recursive: true });
  const manifestPath = join(runtimeRoot, "runtime-manifest.json");
  writeFileSync(manifestPath, JSON.stringify(manifest), "utf8");
  return manifestPath;
}

describe("RuntimeLayout 清单边界", () => {
  it("旧清单保留外部部署兼容，但不臆造内置媒体服务路径", () => {
    const manifestPath = writeManifest(createManifest());
    const resourcesPath = join(manifestPath, "..", "..");
    expect(resolveNearMateRuntimeLayout({ isPackaged: true, resourcesPath }).liveKitServerExe).toBeNull();
  });

  it("内置媒体服务路径必须安全且登记为必验文件", () => {
    const base = createManifest();
    const paths = { ...base.paths as Record<string, string>, livekit_server: "tools/livekit/livekit-server.exe" };
    const incomplete = writeManifest(createManifest({ paths }));
    expect(() => readRuntimeManifest(incomplete)).toThrow("livekit_server 必须登记为 required_files");
    const invalid = writeManifest(createManifest({ paths: { ...paths, livekit_server: "../outside.exe" } }));
    expect(() => readRuntimeManifest(invalid)).toThrow("RuntimeLayout 路径");
    const complete = writeManifest(createManifest({
      paths, required_files: [...base.required_files as string[], paths.livekit_server],
      files: [...base.files as object[], { path: paths.livekit_server, size: 1, sha256: "0".repeat(64) }]
    }));
    expect(resolveNearMateRuntimeLayout({ isPackaged: true, resourcesPath: join(complete, "..", "..") }).liveKitServerExe).toContain("livekit-server.exe");
  });
  it("读取合法 schema v3 清单并解析 profile 与路径", () => {
    const manifestPath = writeManifest(createManifest());
    const manifest = readRuntimeManifest(manifestPath);

    expect(manifest.profile).toBe("cuda");
    expect(manifest.paths.python).toBe("python/Scripts/python.exe");
  });

  it.each([
    "../outside",
    "folder/../outside",
    "folder//file",
    "C:relative/file",
    "/absolute/file",
    "folder/./file",
    "CON/file",
    "folder/name."
  ])("拒绝危险相对路径 %s", path => {
    const manifestPath = writeManifest(createManifest({
      paths: { ...createManifest().paths as Record<string, string>, assets: path }
    }));

    expect(() => readRuntimeManifest(manifestPath)).toThrow(/RuntimeLayout 路径/);
  });

  it("拒绝 files 摘要记录中的重复路径、非法大小或摘要", () => {
    const manifestPath = writeManifest(createManifest({
      files: [
        { path: "assets/icon.png", size: 0, sha256: "not-a-hash" }
      ]
    }));

    expect(() => readRuntimeManifest(manifestPath)).toThrow(/files 记录无效/);
  });

  it("按 Windows 不区分大小写拒绝重复摘要路径", () => {
    const manifestPath = writeManifest(createManifest({
      files: [
        { path: "assets/icon.png", size: 0, sha256: "0".repeat(64) },
        { path: "ASSETS/ICON.PNG", size: 0, sha256: "0".repeat(64) }
      ]
    }));

    expect(() => readRuntimeManifest(manifestPath)).toThrow(/重复路径/);
  });

  it("安装态布局只从 manifest 路径派生绝对路径", () => {
    const root = mkdtempSync(join(tmpdir(), "nearmate-runtime-layout-"));
    const runtimeRoot = join(root, "runtime");
    mkdirSync(runtimeRoot, { recursive: true });
    writeFileSync(join(runtimeRoot, "runtime-manifest.json"), JSON.stringify(createManifest({ profile: "universal" })), "utf8");

    const layout = resolveNearMateRuntimeLayout({ isPackaged: true, resourcesPath: root });

    expect(layout.profile).toBe("universal");
    expect(layout.pythonExe).toBe(join(runtimeRoot, "python", "Scripts", "python.exe"));
    expect(layout.assetsRoot).toBe(join(runtimeRoot, "assets"));
  });
});
