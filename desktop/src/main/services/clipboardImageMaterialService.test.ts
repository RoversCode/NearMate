import { describe, expect, it, vi } from "vitest";
import { createClipboardImageMaterialService } from "./clipboardImageMaterialService";

describe("clipboardImageMaterialService", () => {
  it("剪贴板图片会保存成受管 PNG 并只返回路径材料", async () => {
    const imagePng = Buffer.from("png-bytes");
    const writeFile = vi.fn();
    const service = createClipboardImageMaterialService({
      clipboard: {
        readImage: () => ({ isEmpty: () => false, toPNG: () => imagePng })
      },
      attachmentDir: "G:\\UserData\\clipboard-attachments",
      managedAssetStore: createManagedAssetStore("asset-clipboard-image"),
      writeFile,
      idGenerator: () => "capture-1",
      now: () => new Date("2026-07-07T02:00:00.000Z")
    });

    await expect(service.captureClipboardImageMaterial()).resolves.toEqual({
      kind: "clipboard_image",
      source: "clipboard",
      display_name: "剪贴板图片 2026-07-07 02-00-00",
      path: "G:\\UserData\\clipboard-attachments\\clipboard-image-2026-07-07T02-00-00-000Z-capture-1.png",
      managed_asset_id: "asset-clipboard-image"
    });
    expect(writeFile).toHaveBeenCalledWith(
      "G:\\UserData\\clipboard-attachments\\clipboard-image-2026-07-07T02-00-00-000Z-capture-1.png",
      imagePng
    );
  });

  it("剪贴板没有图片时返回 null", async () => {
    const service = createClipboardImageMaterialService({
      clipboard: {
        readImage: () => ({ isEmpty: () => true, toPNG: () => Buffer.from("") })
      },
      attachmentDir: "G:\\UserData\\clipboard-attachments",
      managedAssetStore: createManagedAssetStore()
    });

    await expect(service.captureClipboardImageMaterial()).resolves.toBeNull();
  });
});

function createManagedAssetStore(managedAssetId = "asset-test") {
  return { registerGeneratedAsset: vi.fn(() => managedAssetId) };
}
