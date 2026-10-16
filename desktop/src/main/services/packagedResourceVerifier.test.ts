import { createHash } from "node:crypto";
import { join } from "node:path";
import { beforeEach, describe, expect, it, vi } from "vitest";

/**
 * 文件系统观察口：测试通过集合表达缺失、目录、符号链接和真实内容。
 * vi.mock 工厂会被提升，因此所有可变状态都放在 vi.hoisted 返回值中。
 */
const fsState = vi.hoisted(() => ({
  missingPaths: new Set<string>(),
  nonFilePaths: new Set<string>(),
  symbolicLinkPaths: new Set<string>(),
  realPathOverrides: new Map<string, string>(),
  fileContents: new Map<string, Buffer>(),
  ordinaryFilePaths: new Set<string>(),
  manifestContent: "{}"
}));

vi.mock("node:fs", () => {
  const realpath = vi.fn((path: unknown) => {
    const value = String(path);
    return fsState.realPathOverrides.get(value) ?? value;
  });
  return {
    existsSync: vi.fn((path: unknown) => !fsState.missingPaths.has(String(path))),
    lstatSync: vi.fn((path: unknown) => {
      const value = String(path);
      if (fsState.missingPaths.has(value)) {
        throw Object.assign(new Error(`ENOENT: ${value}`), { code: "ENOENT" });
      }
      const isSymbolicLink = fsState.symbolicLinkPaths.has(value);
      const isFile = !fsState.nonFilePaths.has(value) && (
        fsState.fileContents.has(value) || fsState.ordinaryFilePaths.has(value)
      );
      return {
        size: fsState.fileContents.get(value)?.byteLength ?? 0,
        isFile: () => isFile,
        isSymbolicLink: () => isSymbolicLink
      };
    }),
    readFileSync: vi.fn((path: unknown) => {
      const value = String(path);
      if (fsState.missingPaths.has(value)) {
        throw Object.assign(new Error(`ENOENT: ${value}`), { code: "ENOENT" });
      }
      if (value.endsWith("runtime-manifest.json")) return fsState.manifestContent;
      const content = fsState.fileContents.get(value);
      if (!content) throw Object.assign(new Error(`ENOENT: ${value}`), { code: "ENOENT" });
      return content;
    }),
    realpathSync: Object.assign(realpath, { native: realpath })
  };
});

import { assertPackagedResourcesIntact } from "./packagedResourceVerifier";

const INPUT = {
  resourcesPath: "C:/NearMate/resources",
  mainModuleDir: "C:/NearMate/resources/app.asar/dist-electron/main"
};
const PYTHON_CONTENT = Buffer.from("packaged-python", "utf8");
const ICON_CONTENT = Buffer.from("packaged-icon", "utf8");

function digest(content: Buffer): string {
  return createHash("sha256").update(content).digest("hex");
}

function runtimePath(relativePath: string): string {
  return join(INPUT.resourcesPath, "runtime", relativePath);
}

function createManifest(overrides: Record<string, unknown> = {}): string {
  return JSON.stringify({
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
      {
        path: "python/Scripts/python.exe",
        size: PYTHON_CONTENT.byteLength,
        sha256: digest(PYTHON_CONTENT)
      },
      {
        path: "assets/icon.png",
        size: ICON_CONTENT.byteLength,
        sha256: digest(ICON_CONTENT)
      }
    ],
    ...overrides
  });
}

beforeEach(() => {
  fsState.missingPaths.clear();
  fsState.nonFilePaths.clear();
  fsState.symbolicLinkPaths.clear();
  fsState.realPathOverrides.clear();
  fsState.fileContents.clear();
  fsState.ordinaryFilePaths.clear();
  fsState.fileContents.set(runtimePath("python/Scripts/python.exe"), PYTHON_CONTENT);
  fsState.fileContents.set(runtimePath("assets/icon.png"), ICON_CONTENT);
  for (const kind of ["avatar", "panel", "chat"]) {
    fsState.ordinaryFilePaths.add(
      join(INPUT.mainModuleDir, "..", "..", "dist", "renderer", `${kind}.html`)
    );
  }
  fsState.ordinaryFilePaths.add(join(INPUT.mainModuleDir, "..", "preload", "sandbox.cjs"));
  fsState.manifestContent = createManifest();
});

describe("packaged 随包资源验证", () => {
  it("全部关键资源是普通文件且摘要匹配时不抛错", () => {
    expect(() => assertPackagedResourcesIntact(INPUT)).not.toThrow();
  });

  it("runtime-manifest.json 缺失时报资源损坏并建议重装", () => {
    fsState.missingPaths.add(runtimePath("runtime-manifest.json"));

    expect(() => assertPackagedResourcesIntact(INPUT)).toThrow(
      /NearMate 安装资源损坏或缺失：[\s\S]*runtime-manifest\.json[\s\S]*请重新安装 NearMate。/
    );
  });

  it("runtime-manifest.json 内容损坏时同样判定安装不完整", () => {
    fsState.manifestContent = "{ 这不是合法 JSON";

    expect(() => assertPackagedResourcesIntact(INPUT)).toThrow(/runtime-manifest\.json/);
  });

  it("缺少非空 files 摘要表时拒绝继续启动", () => {
    fsState.manifestContent = createManifest({ files: [] });

    expect(() => assertPackagedResourcesIntact(INPUT)).toThrow(/缺少非空 files/);
  });

  it("任一窗口入口 html 缺失时列出缺失路径", () => {
    fsState.missingPaths.add(
      join(INPUT.mainModuleDir, "..", "..", "dist", "renderer", "chat.html")
    );

    expect(() => assertPackagedResourcesIntact(INPUT)).toThrow(/chat\.html/);
  });

  it("preload 与托盘图标缺失时一次性列出全部缺失项", () => {
    fsState.missingPaths.add(join(INPUT.mainModuleDir, "..", "preload", "sandbox.cjs"));
    fsState.missingPaths.add(runtimePath("assets/icon.png"));

    let message = "";
    try {
      assertPackagedResourcesIntact(INPUT);
    } catch (error) {
      message = error instanceof Error ? error.message : String(error);
    }

    expect(message).toContain("sandbox.cjs");
    expect(message).toContain("icon.png");
    expect(message).toContain("请重新安装 NearMate。");
  });

  it("required_files 被目录替换时拒绝启动", () => {
    fsState.nonFilePaths.add(runtimePath("python/Scripts/python.exe"));

    expect(() => assertPackagedResourcesIntact(INPUT)).toThrow(/python\.exe[\s\S]*不是普通文件/);
  });

  it("required_files 大小或 SHA256 不符时拒绝启动", () => {
    fsState.fileContents.set(
      runtimePath("python/Scripts/python.exe"),
      Buffer.from("tampered-python", "utf8")
    );
    fsState.fileContents.set(runtimePath("assets/icon.png"), Buffer.from("altered-icon!!", "utf8"));

    const message = captureFailure();

    expect(message).toContain("python.exe");
    expect(message).toContain("SHA256 摘要不符");
    expect(message).toContain("icon.png");
    expect(message).toContain("文件大小不符");
  });

  it("required_files 经过符号链接或目录联接时拒绝启动", () => {
    fsState.symbolicLinkPaths.add(runtimePath("python"));

    expect(() => assertPackagedResourcesIntact(INPUT)).toThrow(/符号链接或目录联接/);
  });
});

function captureFailure(): string {
  try {
    assertPackagedResourcesIntact(INPUT);
    return "";
  } catch (error) {
    return error instanceof Error ? error.message : String(error);
  }
}
