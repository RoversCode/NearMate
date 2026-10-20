import { mkdtempSync, mkdirSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import { loadLive2dModelCatalog } from "./modelCatalog";

/**
 * 在隔离临时目录下创建一个最小模型目录。
 * 这里不需要真实模型内容，只需要让目录发现逻辑看到稳定的文件名组合。
 */
function createModelDirectory(rootDirectoryPath: string, modelId: string, modelFiles: string[]) {
  const modelDirectoryPath = join(rootDirectoryPath, modelId);
  mkdirSync(modelDirectoryPath, { recursive: true });

  for (const modelFileName of modelFiles) {
    writeFileSync(join(modelDirectoryPath, modelFileName), "{}", "utf-8");
  }
}

/**
 * createModelDirectoryWithMocVersion 用最小真实结构构造一个可被 catalog 扫描的模型目录。
 * 这里显式写出 model3.json 与 moc3 头，目的是覆盖“当前 bundled core 是否支持该模型”的过滤逻辑。
 */
function createModelDirectoryWithMocVersion(
  rootDirectoryPath: string,
  modelId: string,
  mocVersion: number
) {
  const modelDirectoryPath = join(rootDirectoryPath, modelId);
  mkdirSync(modelDirectoryPath, { recursive: true });

  writeFileSync(
    join(modelDirectoryPath, `${modelId}.model3.json`),
    JSON.stringify({
      FileReferences: {
        Moc: `${modelId}.moc3`
      }
    }),
    "utf-8"
  );

  /**
   * moc3 头的前 4 字节固定为 `MOC3`，后 4 字节是 little-endian 版本号。
   * 对目录扫描测试来说，写出这一段最小头信息就足够驱动兼容性判断。
   */
  const mocHeader = Buffer.alloc(8);
  mocHeader.write("MOC3", 0, "ascii");
  mocHeader.writeUInt32LE(mocVersion, 4);
  writeFileSync(join(modelDirectoryPath, `${modelId}.moc3`), mocHeader);
}

describe("modelCatalog", () => {
  const temporaryDirectoryPaths: string[] = [];

  afterEach(() => {
    for (const temporaryDirectoryPath of temporaryDirectoryPaths) {
      rmSync(temporaryDirectoryPath, { recursive: true, force: true });
    }

    temporaryDirectoryPaths.length = 0;
  });

  it("优先选择与目录同名的 *.model3.json，即使目录里还存在其他候选", () => {
    const resourcesRoot = mkdtempSync(join(tmpdir(), "live2d-catalog-"));
    temporaryDirectoryPaths.push(resourcesRoot);
    createModelDirectory(resourcesRoot, "Haru", [
      "Haru.basic.model3.json",
      "Haru.model3.json"
    ]);

    expect(loadLive2dModelCatalog({ resourcesRoot })).toEqual([
      {
        id: "Haru",
        label: "Haru",
        modelPath: "./live2d/resources/Haru/Haru.model3.json"
      }
    ]);
  });

  it("目录里只有一个 *.model3.json 时，回退使用唯一候选", () => {
    const resourcesRoot = mkdtempSync(join(tmpdir(), "live2d-catalog-"));
    temporaryDirectoryPaths.push(resourcesRoot);
    createModelDirectory(resourcesRoot, "Guest", ["guest-default.model3.json"]);

    expect(loadLive2dModelCatalog({ resourcesRoot })).toEqual([
      {
        id: "Guest",
        label: "Guest",
        modelPath: "./live2d/resources/Guest/guest-default.model3.json"
      }
    ]);
  });

  it("Q 版大肥鱼使用中文名称，资源路径仍保留模型原文件名", () => {
    const resourcesRoot = mkdtempSync(join(tmpdir(), "live2d-catalog-"));
    temporaryDirectoryPaths.push(resourcesRoot);
    createModelDirectory(resourcesRoot, "DeepSeekChibi", ["c_0120.model3.json"]);

    expect(loadLive2dModelCatalog({ resourcesRoot })).toEqual([
      {
        id: "DeepSeekChibi",
        label: "大肥鱼（Q版）",
        modelPath: "./live2d/resources/DeepSeekChibi/c_0120.model3.json"
      }
    ]);
  });

  it("目录里有多个候选但没有同名主文件时，排除该目录避免不稳定切换", () => {
    const resourcesRoot = mkdtempSync(join(tmpdir(), "live2d-catalog-"));
    temporaryDirectoryPaths.push(resourcesRoot);
    createModelDirectory(resourcesRoot, "Ambiguous", [
      "first.model3.json",
      "second.model3.json"
    ]);

    expect(loadLive2dModelCatalog({ resourcesRoot })).toEqual([]);
  });

  it("主模型 moc3 版本超出当前 bundled core 支持上限时，会从目录列表中过滤掉", () => {
    const resourcesRoot = mkdtempSync(join(tmpdir(), "live2d-catalog-"));
    temporaryDirectoryPaths.push(resourcesRoot);
    createModelDirectoryWithMocVersion(resourcesRoot, "Haru", 5);
    createModelDirectoryWithMocVersion(resourcesRoot, "Ren", 6);

    expect(loadLive2dModelCatalog({ resourcesRoot })).toEqual([
      {
        id: "Haru",
        label: "Haru",
        modelPath: "./live2d/resources/Haru/Haru.model3.json"
      }
    ]);
  });
});
