import { randomUUID } from "node:crypto";
import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";

import type { ManagedMaterialAssetStore } from "./managedMaterialAssetStore";

type ComposerTextMaterialServiceOptions = {
  attachmentDir: string;
  managedAssetStore: Pick<ManagedMaterialAssetStore, "registerGeneratedAsset">;
  writeFile?: (path: string, data: string) => void;
  ensureDir?: (path: string) => void;
  idGenerator?: () => string;
  now?: () => Date;
};

export type ComposerTextMaterial = {
  kind: "clipboard_text";
  source: "composer_paste";
  display_name: string;
  path: string;
  managed_asset_id: string;
};

/**
 * 创建只负责把输入框超限文本物化为受管 TXT 的材料服务。
 *
 * Args:
 *   options: 受管目录与可注入文件能力。
 *
 * Returns:
 *   可把完整文本写入文件并返回路径材料的服务。
 */
export function createComposerTextMaterialService(options: ComposerTextMaterialServiceOptions) {
  const writeFile = options.writeFile ?? writeFileSync;
  const ensureDir = options.ensureDir ?? ((path: string) => mkdirSync(path, { recursive: true }));
  const idGenerator = options.idGenerator ?? randomUUID;
  const now = options.now ?? (() => new Date());

  return {
    async createComposerTextMaterial(text: string): Promise<ComposerTextMaterial> {
      const capturedAt = now();
      const fileName = `pasted-text-${safeTimestamp(capturedAt)}-${idGenerator()}.txt`;
      const filePath = join(options.attachmentDir, fileName);
      ensureDir(options.attachmentDir);
      writeFile(filePath, text);
      const managedAssetId = options.managedAssetStore.registerGeneratedAsset(filePath);
      return {
        kind: "clipboard_text",
        source: "composer_paste",
        display_name: "粘贴文本.txt",
        path: filePath,
        managed_asset_id: managedAssetId
      };
    }
  };
}

function safeTimestamp(date: Date): string {
  return date.toISOString().replace(/[:.]/g, "-");
}
