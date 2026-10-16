import { randomUUID } from "node:crypto";
import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";

import type { ManagedMaterialAssetStore } from "./managedMaterialAssetStore";

type ClipboardImage = {
  isEmpty(): boolean;
  toPNG(): Buffer;
};

type ClipboardImageMaterialServiceOptions = {
  clipboard: { readImage(): ClipboardImage };
  attachmentDir: string;
  managedAssetStore: Pick<ManagedMaterialAssetStore, "registerGeneratedAsset">;
  writeFile?: (path: string, data: Buffer) => void;
  ensureDir?: (path: string) => void;
  idGenerator?: () => string;
  now?: () => Date;
};

export type ClipboardImageMaterial = {
  kind: "clipboard_image";
  source: "clipboard";
  display_name: string;
  path: string;
  managed_asset_id: string;
};

/**
 * 创建只负责把剪贴板图片物化为受管 PNG 的材料服务。
 *
 * Args:
 *   options: 剪贴板图片端口、受管目录与可注入文件能力。
 *
 * Returns:
 *   剪贴板为空时返回 null，否则返回只携带路径的图片材料。
 */
export function createClipboardImageMaterialService(options: ClipboardImageMaterialServiceOptions) {
  const writeFile = options.writeFile ?? writeFileSync;
  const ensureDir = options.ensureDir ?? ((path: string) => mkdirSync(path, { recursive: true }));
  const idGenerator = options.idGenerator ?? randomUUID;
  const now = options.now ?? (() => new Date());

  return {
    async captureClipboardImageMaterial(): Promise<ClipboardImageMaterial | null> {
      const image = options.clipboard.readImage();
      if (image.isEmpty()) {
        return null;
      }

      const capturedAt = now();
      const fileName = `clipboard-image-${safeTimestamp(capturedAt)}-${idGenerator()}.png`;
      const filePath = join(options.attachmentDir, fileName);
      ensureDir(options.attachmentDir);
      writeFile(filePath, image.toPNG());
      const managedAssetId = options.managedAssetStore.registerGeneratedAsset(filePath);
      return {
        kind: "clipboard_image",
        source: "clipboard",
        display_name: `剪贴板图片 ${humanTimestamp(capturedAt)}`,
        path: filePath,
        managed_asset_id: managedAssetId
      };
    }
  };
}

function safeTimestamp(date: Date): string {
  return date.toISOString().replace(/[:.]/g, "-");
}

function humanTimestamp(date: Date): string {
  return safeTimestamp(date).slice(0, 19).replace("T", " ");
}
