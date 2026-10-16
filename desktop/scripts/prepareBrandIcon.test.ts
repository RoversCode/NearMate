import assert from "node:assert/strict";
import { mkdtemp, mkdir, readFile, copyFile, rm } from "node:fs/promises";
import { createRequire } from "node:module";
import { tmpdir } from "node:os";
import { resolve, join } from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "vitest";
import sharp from "sharp";

const require = createRequire(import.meta.url);
const projectDir = fileURLToPath(new URL("..", import.meta.url));
const sourceIcon = resolve(projectDir, "../assets/icon.png");

test("发行配置必须写入 EXE 图标，并让安装器和快捷方式使用同一品牌", async () => {
  const builder = await readFile(join(projectDir, "electron-builder.yml"), "utf8");
  const installer = await readFile(resolve(projectDir, "../installer/NearMate.iss"), "utf8");
  assert.match(builder, /^beforePack: .\/scripts\/prepareBrandIcon\.cjs$/m);
  assert.match(builder, /^  icon: dist-brand\/icon\.ico$/m);
  assert.match(builder, /^  signAndEditExecutable: true$/m);
  assert.match(builder, /^  - from: dist-brand\/icon\.ico\r?\n    to: icon\.ico$/m);
  assert.match(installer, /^SetupIconFile=\.\.\\desktop\\dist-brand\\icon\.ico$/m);
  assert.match(installer, /^UninstallDisplayIcon=\{app\}\\resources\\icon\.ico$/m);
  assert.equal((installer.match(/IconFilename: "\{app\}\\resources\\icon\.ico"/g) ?? []).length, 2);
});

test("Windows ICO 的所有尺寸均来自当前品牌 PNG，重复构建得到相同内容", async () => {
  const prepareBrandIcon = require("./prepareBrandIcon.cjs");
  const fixtureRoot = await mkdtemp(join(tmpdir(), "nearmate-brand-"));
  const fixtureDesktop = join(fixtureRoot, "desktop");
  try {
    await mkdir(join(fixtureRoot, "assets"), { recursive: true });
    await copyFile(sourceIcon, join(fixtureRoot, "assets/icon.png"));
    const context = { packager: { projectDir: fixtureDesktop } };
    await prepareBrandIcon(context);
    const iconPath = join(fixtureDesktop, "dist-brand/icon.ico");
    const icon = await readFile(iconPath);
    assert.equal(icon.readUInt16LE(0), 0);
    assert.equal(icon.readUInt16LE(2), 1);
    const sizes = [16, 24, 32, 48, 64, 128, 256];
    assert.equal(icon.readUInt16LE(4), sizes.length);
    for (const [index, size] of sizes.entries()) {
      const entry = 6 + index * 16;
      assert.equal(icon[entry] || 256, size);
      assert.equal(icon[entry + 1] || 256, size);
      const length = icon.readUInt32LE(entry + 8);
      const offset = icon.readUInt32LE(entry + 12);
      const frame = icon.subarray(offset, offset + length);
      const actual = await sharp(frame).ensureAlpha().raw().toBuffer({ resolveWithObject: true });
      const expected = await sharp(sourceIcon).resize(size, size, { fit: "contain" }).ensureAlpha().raw().toBuffer();
      assert.equal(actual.info.width, size);
      assert.equal(actual.info.height, size);
      assert.deepEqual(actual.data, expected);
    }
    await prepareBrandIcon(context);
    assert.deepEqual(await readFile(iconPath), icon);
  } finally {
    // 测试只清理 mkdtemp 创建的独占目录，不接触真实品牌资产或发行产物。
    await rm(fixtureRoot, { recursive: true, force: true });
  }
});
