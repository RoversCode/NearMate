import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { afterEach, describe, expect, it } from "vitest";
import { createAvatarModelLayoutStore } from "./avatarModelLayoutStore";

describe("createAvatarModelLayoutStore", () => {
  let temporaryDirectoryPath: string | null = null;

  afterEach(() => {
    if (!temporaryDirectoryPath) {
      return;
    }

    rmSync(temporaryDirectoryPath, { recursive: true, force: true });
    temporaryDirectoryPath = null;
  });

  /**
   * createStoreFilePath 为每个用例创建独立存储文件路径。
   * 业务意图是避免布局偏好测试之间共享磁盘状态，保证 round-trip 断言稳定。
   */
  function createStoreFilePath(): string {
    temporaryDirectoryPath = mkdtempSync(join(tmpdir(), "avatar-layout-store-"));
    return join(temporaryDirectoryPath, "avatar-model-layouts.json");
  }

  it("按 modelId 独立保存和读取窗口几何", () => {
    const store = createAvatarModelLayoutStore(createStoreFilePath());
    const workArea = { x: 0, y: 0, width: 1440, height: 860 };

    store.save("Haru", { x: 120, y: 140, width: 420, height: 520 });
    store.save("Natori", { x: 500, y: 160, width: 360, height: 480 });

    expect(store.read("Haru", workArea)).toEqual({
      x: 120,
      y: 140,
      width: 420,
      height: 520
    });
    expect(store.read("Natori", workArea)).toEqual({
      x: 500,
      y: 160,
      width: 360,
      height: 480
    });
    expect(store.read("Unknown", workArea)).toBeNull();
  });

  it("读取时忽略非法记录，并把屏幕外位置夹回工作区", () => {
    const filePath = createStoreFilePath();
    const store = createAvatarModelLayoutStore(filePath);

    writeFileSync(
      filePath,
      JSON.stringify({
        version: 1,
        layouts: {
          Haru: { x: "bad", y: 140, width: 420, height: 520 },
          Natori: { x: -5000, y: 9000, width: 300, height: 400 }
        }
      }),
      "utf-8"
    );

    expect(store.read("Haru", { x: 0, y: 0, width: 1280, height: 720 })).toBeNull();
    expect(store.read("Natori", { x: 0, y: 0, width: 1280, height: 720 })).toEqual({
      x: 0,
      y: 320,
      width: 300,
      height: 400
    });
  });

  it("重置当前 modelId 时只删除对应布局", () => {
    const store = createAvatarModelLayoutStore(createStoreFilePath());
    const workArea = { x: 0, y: 0, width: 1440, height: 860 };

    store.save("Haru", { x: 120, y: 140, width: 420, height: 520 });
    store.save("Natori", { x: 500, y: 160, width: 360, height: 480 });
    store.reset("Haru");

    expect(store.read("Haru", workArea)).toBeNull();
    expect(store.read("Natori", workArea)).toEqual({
      x: 500,
      y: 160,
      width: 360,
      height: 480
    });
  });
});
