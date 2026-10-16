import { randomUUID } from "node:crypto";
import { mkdir, writeFile } from "node:fs/promises";
import { join } from "node:path";
import type { ManagedMaterialAssetStore } from "./managedMaterialAssetStore";
import type { MaterialItemPayload } from "./materialsClient";
import type { CapturedMaterial } from "./capturedMaterialClassifier";

export type SelectedContentFileStore = {
  writeTextFile(directory: string, fileName: string, content: string): Promise<string>;
  writeJsonFile(directory: string, fileName: string, content: Record<string, unknown>): Promise<string>;
  writePngFile(directory: string, fileName: string, content: Buffer): Promise<string>;
};

type ManagedMaterialWriterOptions = {
  attachmentDir: string;
  managedAssetStore: Pick<ManagedMaterialAssetStore, "registerGeneratedAsset">;
  fileStore: SelectedContentFileStore;
  clock?: { now(): Date };
  idGenerator?: { nextId(): string };
};

/**
 * 把需要项目副本的分类结果写入受管目录，原始文件路径保持引用语义。
 *
 * @param captured 纯分类器返回的材料结果。
 * @param options 受管目录、文件端口和资产登记仓储。
 * @returns 可直接交给材料命令服务的载荷数组。
 */
export async function writeCapturedMaterial(
  captured: CapturedMaterial,
  options: ManagedMaterialWriterOptions
): Promise<MaterialItemPayload[]> {
  if (captured.kind === "empty") return [];
  if (captured.kind === "paths") return captured.items;

  const clock = options.clock ?? { now: () => new Date() };
  const idGenerator = options.idGenerator ?? { nextId: () => randomUUID() };
  const suffix = `${formatTimestamp(clock.now())}-${safeFileSuffix(idGenerator.nextId())}`;
  if (captured.kind === "web") {
    const path = await options.fileStore.writeJsonFile(options.attachmentDir, `selected-web-${suffix}.json`, captured.selection);
    return [managedItem(options, path, "selected_web", "网页选区")];
  }
  if (captured.kind === "text") {
    const path = await options.fileStore.writeTextFile(options.attachmentDir, `selected-text-${suffix}.txt`, captured.text);
    return [managedItem(options, path, "selected_text", "选中文本.txt")];
  }
  const path = await options.fileStore.writePngFile(options.attachmentDir, `selected-image-${suffix}.png`, captured.png);
  return [managedItem(options, path, "selected_image", "选中图片.png")];
}

/** 创建磁盘文件端口，所有写入都会先确保受管根目录存在。 */
export function createDiskSelectedContentFileStore(): SelectedContentFileStore {
  async function write(directory: string, fileName: string, content: string | Buffer): Promise<string> {
    await mkdir(directory, { recursive: true });
    const path = join(directory, fileName);
    await writeFile(path, content);
    return path;
  }
  return {
    writeTextFile: (directory, fileName, content) => write(directory, fileName, content),
    writeJsonFile: (directory, fileName, content) => write(directory, fileName, JSON.stringify(content, null, 2)),
    writePngFile: (directory, fileName, content) => write(directory, fileName, content)
  };
}

function managedItem(
  options: ManagedMaterialWriterOptions,
  path: string,
  kind: "selected_web" | "selected_text" | "selected_image",
  displayName: string
): MaterialItemPayload {
  return {
    kind,
    source: "selection_capture",
    display_name: displayName,
    path,
    managed_asset_id: options.managedAssetStore.registerGeneratedAsset(path)
  };
}

function formatTimestamp(date: Date): string {
  const values = [date.getUTCMonth() + 1, date.getUTCDate(), date.getUTCHours(), date.getUTCMinutes(), date.getUTCSeconds()];
  return `${date.getUTCFullYear()}${values[0].toString().padStart(2, "0")}${values[1].toString().padStart(2, "0")}-${values.slice(2).map(value => value.toString().padStart(2, "0")).join("")}`;
}

function safeFileSuffix(value: string): string {
  return value.replace(/[^a-zA-Z0-9_-]/g, "").slice(0, 16) || "capture";
}
