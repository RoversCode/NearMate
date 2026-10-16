import { describe, expect, it, vi } from "vitest";
import { createComposerTextMaterialService } from "./composerTextMaterialService";

describe("composerTextMaterialService", () => {
  it("超限粘贴文本会完整落盘并只返回路径材料", async () => {
    const writeFile = vi.fn();
    const service = createComposerTextMaterialService({
      attachmentDir: "G:\\UserData\\clipboard-attachments",
      managedAssetStore: { registerGeneratedAsset: vi.fn(() => "asset-long-text") },
      writeFile,
      idGenerator: () => "capture-2",
      now: () => new Date("2026-07-07T02:00:00.000Z")
    });

    await expect(service.createComposerTextMaterial("完整长文本")).resolves.toEqual({
      kind: "clipboard_text",
      source: "composer_paste",
      display_name: "粘贴文本.txt",
      path: "G:\\UserData\\clipboard-attachments\\pasted-text-2026-07-07T02-00-00-000Z-capture-2.txt",
      managed_asset_id: "asset-long-text"
    });
    expect(writeFile).toHaveBeenCalledWith(
      "G:\\UserData\\clipboard-attachments\\pasted-text-2026-07-07T02-00-00-000Z-capture-2.txt",
      "完整长文本"
    );
  });
});
