import { mkdtempSync, mkdirSync, readFileSync, symlinkSync, writeFileSync } from "node:fs";
import { rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";

describe("createManagedMaterialAssetStore", () => {
  const cleanupPaths: string[] = [];

  afterEach(async () => {
    await Promise.all(cleanupPaths.splice(0).map(path => rm(path, { recursive: true, force: true })));
  });

  it("只按已登记 ID 删除项目在专用目录中创建的副本", async () => {
    const { createManagedMaterialAssetStore } = await import("./managedMaterialAssetStore");
    const workspace = mkdtempSync(join(tmpdir(), "managed-material-assets-"));
    cleanupPaths.push(workspace);
    const rootDir = join(workspace, "runtime-assets");
    mkdirSync(rootDir, { recursive: true });
    const generatedPath = join(rootDir, "selected-text.txt");
    writeFileSync(generatedPath, "copy", "utf8");
    const store = createManagedMaterialAssetStore({
      rootDir,
      idGenerator: () => "asset-1"
    });

    const managedAssetId = store.registerGeneratedAsset(generatedPath);

    await expect(store.release(managedAssetId)).resolves.toBe(true);
    expect(() => readFileSync(generatedPath)).toThrow();
  });

  it("未知 ID 不会删除任何文件", async () => {
    const { createManagedMaterialAssetStore } = await import("./managedMaterialAssetStore");
    const workspace = mkdtempSync(join(tmpdir(), "managed-material-assets-"));
    cleanupPaths.push(workspace);
    const rootDir = join(workspace, "runtime-assets");
    mkdirSync(rootDir, { recursive: true });
    const generatedPath = join(rootDir, "keep.txt");
    writeFileSync(generatedPath, "keep", "utf8");
    const store = createManagedMaterialAssetStore({ rootDir });

    await expect(store.release("unknown-id")).resolves.toBe(false);
    expect(readFileSync(generatedPath, "utf8")).toBe("keep");
  });

  it("同一个真实路径重复登记时复用原资产编号", async () => {
    const { createManagedMaterialAssetStore } = await import("./managedMaterialAssetStore");
    const workspace = mkdtempSync(join(tmpdir(), "managed-material-assets-"));
    cleanupPaths.push(workspace);
    const rootDir = join(workspace, "runtime-assets");
    mkdirSync(rootDir, { recursive: true });
    const generatedPath = join(rootDir, "same.png");
    writeFileSync(generatedPath, "copy", "utf8");
    const idGenerator = vi.fn(() => "asset-one");
    const store = createManagedMaterialAssetStore({ rootDir, idGenerator });

    expect(store.registerGeneratedAsset(generatedPath)).toBe("asset-one");
    expect(store.registerGeneratedAsset(generatedPath)).toBe("asset-one");
    expect(idGenerator).toHaveBeenCalledOnce();
  });

  it("拒绝登记专用目录之外的用户原文件", async () => {
    const { createManagedMaterialAssetStore } = await import("./managedMaterialAssetStore");
    const workspace = mkdtempSync(join(tmpdir(), "managed-material-assets-"));
    cleanupPaths.push(workspace);
    const rootDir = join(workspace, "runtime-assets");
    const userFilePath = join(workspace, "original-photo.png");
    mkdirSync(rootDir, { recursive: true });
    writeFileSync(userFilePath, "original", "utf8");
    const store = createManagedMaterialAssetStore({ rootDir });

    expect(() => store.registerGeneratedAsset(userFilePath)).toThrow("受管资产必须位于项目专用临时目录内");
    expect(readFileSync(userFilePath, "utf8")).toBe("original");
  });

  it("释放前重新验证真实路径并拒绝登记后的路径逃逸", async () => {
    const { createManagedMaterialAssetStore } = await import("./managedMaterialAssetStore");
    const workspace = mkdtempSync(join(tmpdir(), "managed-material-assets-"));
    cleanupPaths.push(workspace);
    const rootDir = join(workspace, "runtime-assets");
    const generatedPath = join(rootDir, "selected-bundle");
    const userDirectory = join(workspace, "user-documents");
    const userFilePath = join(userDirectory, "original.txt");
    mkdirSync(generatedPath, { recursive: true });
    mkdirSync(userDirectory, { recursive: true });
    writeFileSync(join(generatedPath, "copy.txt"), "copy", "utf8");
    writeFileSync(userFilePath, "original", "utf8");
    const warn = vi.fn();
    const store = createManagedMaterialAssetStore({
      rootDir,
      idGenerator: () => "asset-escaped",
      logger: { warn }
    });
    const managedAssetId = store.registerGeneratedAsset(generatedPath);
    await rm(generatedPath, { recursive: true, force: true });
    symlinkSync(userDirectory, generatedPath, "junction");

    await expect(store.release(managedAssetId)).resolves.toBe(false);
    expect(warn).toHaveBeenCalledOnce();
    expect(readFileSync(userFilePath, "utf8")).toBe("original");
  });

  it("启动清理只重建项目专用临时目录", async () => {
    const { createManagedMaterialAssetStore } = await import("./managedMaterialAssetStore");
    const workspace = mkdtempSync(join(tmpdir(), "managed-material-assets-"));
    cleanupPaths.push(workspace);
    const rootDir = join(workspace, "runtime-assets");
    const staleDirectory = join(rootDir, "selected-web-old");
    const userFilePath = join(workspace, "original.txt");
    mkdirSync(staleDirectory, { recursive: true });
    writeFileSync(join(staleDirectory, "content.json"), "stale", "utf8");
    writeFileSync(userFilePath, "original", "utf8");
    const store = createManagedMaterialAssetStore({ rootDir });

    await store.prepareRuntime();

    expect(() => readFileSync(join(staleDirectory, "content.json"))).toThrow();
    expect(readFileSync(userFilePath, "utf8")).toBe("original");
  });

  it("正常退出同步清空全部已登记项目副本", async () => {
    const { createManagedMaterialAssetStore } = await import("./managedMaterialAssetStore");
    const workspace = mkdtempSync(join(tmpdir(), "managed-material-assets-"));
    cleanupPaths.push(workspace);
    const rootDir = join(workspace, "runtime-assets");
    mkdirSync(rootDir, { recursive: true });
    const generatedPath = join(rootDir, "selected-image.png");
    writeFileSync(generatedPath, "copy", "utf8");
    const store = createManagedMaterialAssetStore({ rootDir });
    store.registerGeneratedAsset(generatedPath);

    store.releaseAllSync();

    expect(() => readFileSync(generatedPath)).toThrow();
  });
});
