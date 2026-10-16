import assert from "node:assert/strict";
import { mkdir, mkdtemp, readFile, rm, utimes, writeFile } from "node:fs/promises";
import { createRequire } from "node:module";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { latestFileMtime, readRegularFileMtime, shouldBuildConsole } from "./backend-core-dev.mjs";

const REPOSITORY_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

test("静态 Console 不存在时需要构建", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "nearmate-console-missing-"));
  try {
    await mkdir(path.join(root, "assets"), { recursive: true });
    await writeFile(path.join(root, "assets", "icon.png"), "icon");
    assert.equal(await shouldBuildConsole(root), true);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("源码晚于 index.html 时需要构建，否则不重复构建", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "nearmate-console-build-"));
  try {
    const sourceDir = path.join(root, "console", "src");
    const distDir = path.join(root, "console", "dist");
    const assetsDir = path.join(root, "assets");
    await mkdir(sourceDir, { recursive: true });
    await mkdir(distDir, { recursive: true });
    await mkdir(assetsDir, { recursive: true });
    const source = path.join(sourceDir, "App.tsx");
    const index = path.join(distDir, "index.html");
    const icon = path.join(assetsDir, "icon.png");
    await writeFile(source, "source");
    await writeFile(index, "dist");
    await writeFile(icon, "icon");
    const now = Date.now() / 1000;
    await utimes(source, now - 10, now - 10);
    await utimes(index, now, now);
    await utimes(icon, now - 10, now - 10);
    assert.equal(await shouldBuildConsole(root), false);
    await utimes(source, now + 10, now + 10);
    assert.equal(await shouldBuildConsole(root), true);
    assert.equal(await latestFileMtime(sourceDir) > 0, true);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("Console 外部品牌资产晚于 index.html 时需要构建", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "nearmate-console-external-asset-"));
  try {
    const sourceDir = path.join(root, "console", "src");
    const distDir = path.join(root, "console", "dist");
    const assetsDir = path.join(root, "assets");
    await mkdir(sourceDir, { recursive: true });
    await mkdir(distDir, { recursive: true });
    await mkdir(assetsDir, { recursive: true });

    const source = path.join(sourceDir, "App.tsx");
    const index = path.join(distDir, "index.html");
    const icon = path.join(assetsDir, "icon.png");
    await writeFile(source, "source");
    await writeFile(index, "dist");
    await writeFile(icon, "new-icon");

    const now = Date.now() / 1000;
    await utimes(source, now - 20, now - 20);
    await utimes(index, now, now);
    await utimes(icon, now + 20, now + 20);

    assert.equal(await shouldBuildConsole(root), true);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("Console 外部品牌资产缺失时需要触发重建，不能继续复用旧产物", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "nearmate-console-missing-asset-"));
  try {
    const sourceDir = path.join(root, "console", "src");
    const distDir = path.join(root, "console", "dist");
    await mkdir(sourceDir, { recursive: true });
    await mkdir(distDir, { recursive: true });

    const source = path.join(sourceDir, "App.tsx");
    const index = path.join(distDir, "index.html");
    await writeFile(source, "source");
    await writeFile(index, "dist");

    const now = Date.now() / 1000;
    await utimes(source, now - 20, now - 20);
    await utimes(index, now, now);

    assert.equal(await readRegularFileMtime(path.join(root, "assets", "icon.png")), null);
    assert.equal(await shouldBuildConsole(root), true);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("Console 外部品牌资产是目录时需要触发重建，不能复用旧产物", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "nearmate-console-invalid-asset-dir-"));
  try {
    const sourceDir = path.join(root, "console", "src");
    const distDir = path.join(root, "console", "dist");
    const iconDir = path.join(root, "assets", "icon.png");
    await mkdir(sourceDir, { recursive: true });
    await mkdir(distDir, { recursive: true });
    await mkdir(iconDir, { recursive: true });

    const source = path.join(sourceDir, "App.tsx");
    const index = path.join(distDir, "index.html");
    const staleChild = path.join(iconDir, "stale-child");
    await writeFile(source, "source");
    await writeFile(index, "dist");
    await writeFile(staleChild, "stale");

    const now = Date.now() / 1000;
    await utimes(source, now - 20, now - 20);
    await utimes(staleChild, now - 20, now - 20);
    await utimes(index, now, now);

    assert.equal(await shouldBuildConsole(root), true);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("Console 外部品牌资产的父路径异常时需要触发重建", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "nearmate-console-invalid-asset-parent-"));
  try {
    const sourceDir = path.join(root, "console", "src");
    const distDir = path.join(root, "console", "dist");
    await mkdir(sourceDir, { recursive: true });
    await mkdir(distDir, { recursive: true });
    await writeFile(path.join(root, "assets"), "not-a-directory");

    const source = path.join(sourceDir, "App.tsx");
    const index = path.join(distDir, "index.html");
    await writeFile(source, "source");
    await writeFile(index, "dist");

    const now = Date.now() / 1000;
    await utimes(source, now - 20, now - 20);
    await utimes(index, now, now);

    assert.equal(await readRegularFileMtime(path.join(root, "assets", "icon.png")), null);
    assert.equal(await shouldBuildConsole(root), true);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("Console 外部品牌资产的 Unix epoch 修改时间仍视为有效文件", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "nearmate-console-epoch-asset-"));
  try {
    const sourceDir = path.join(root, "console", "src");
    const distDir = path.join(root, "console", "dist");
    const assetsDir = path.join(root, "assets");
    await mkdir(sourceDir, { recursive: true });
    await mkdir(distDir, { recursive: true });
    await mkdir(assetsDir, { recursive: true });

    const source = path.join(sourceDir, "App.tsx");
    const index = path.join(distDir, "index.html");
    const icon = path.join(assetsDir, "icon.png");
    await writeFile(source, "source");
    await writeFile(index, "dist");
    await writeFile(icon, "icon");

    const now = Date.now() / 1000;
    await utimes(source, now - 20, now - 20);
    await utimes(index, now, now);
    await utimes(icon, 0, 0);

    assert.equal(await shouldBuildConsole(root), false);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("安装器模板使用稳定 AppId，升级只接管 NearMate 发行目录", async () => {
  const [rootTemplate, cleanupTemplate, manifestText] = await Promise.all([
    readFile(path.join(REPOSITORY_ROOT, "installer", "NearMate.iss"), "utf8"),
    readFile(path.join(REPOSITORY_ROOT, "desktop", "installer", "releaseCleanup.iss"), "utf8"),
    readFile(path.join(REPOSITORY_ROOT, "installer", "runtime-manifest.json"), "utf8"),
  ]);
  const appIdPattern = /AppId=\{\{([0-9A-F-]{36})\}/i;
  assert.equal(rootTemplate.match(appIdPattern)?.[1], "8D5D75A7-8E4F-4F7D-9E1D-5A7E1A7E9D01");
  assert.equal(JSON.parse(manifestText).user_data_policy, "%APPDATA%\\NearMate");
  assert.match(cleanupTemplate, /HasPreviousRelease := IsPreviousNearMateRelease\(AppRoot\)/);
  assert.match(cleanupTemplate, /"product":"NearMate"/);
  assert.match(cleanupTemplate, /"platform":"windows-x64"/);
  assert.match(cleanupTemplate, /"schema_version":3/);
  assert.match(cleanupTemplate, /"required_files":/);
  assert.doesNotMatch(cleanupTemplate, /profile\.json/);
  assert.match(cleanupTemplate, /release-root-files\.txt/);
  assert.match(cleanupTemplate, /LoadOwnedReleasePaths/);
  assert.doesNotMatch(cleanupTemplate, /SetArrayLength\(OwnedPaths, 20\)/);
});

test("Electron 打包配置符合当前固定构建工具的 schema", async () => {
  const desktopRoot = path.join(REPOSITORY_ROOT, "desktop");
  // 从 Desktop 已声明的构建工具加载它自己的校验器，让测试与实际打包使用同一套依赖。
  const desktopRequire = createRequire(path.join(desktopRoot, "package.json"));
  const builderRequire = createRequire(desktopRequire.resolve("electron-builder"));
  const { DebugLogger } = builderRequire("builder-util");
  const { getConfig, validateConfiguration } = builderRequire("app-builder-lib/out/util/config/config.js");
  const config = await getConfig(desktopRoot, path.join(desktopRoot, "electron-builder.yml"), null);
  await validateConfiguration(config, new DebugLogger(false));
});
