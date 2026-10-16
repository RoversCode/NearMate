import { describe, expect, it, vi } from "vitest";

import {
  CHARACTER_CARD_COVER_ASPECT,
  CHARACTER_CARD_COVER_MAX_OUTPUT_BYTES,
  CHARACTER_CARD_COVER_MAX_SOURCE_BYTES,
  createCroppedCoverFile,
  validateCoverSourceFile
} from "./coverImageProcessing";

describe("coverImageProcessing", () => {
  it("校验原图类型和 20MB 原图上限", () => {
    expect(CHARACTER_CARD_COVER_ASPECT).toBeCloseTo(4 / 5.2);
    expect(CHARACTER_CARD_COVER_MAX_SOURCE_BYTES).toBe(20 * 1024 * 1024);
    expect(CHARACTER_CARD_COVER_MAX_OUTPUT_BYTES).toBe(2 * 1024 * 1024);

    expect(validateCoverSourceFile(new File(["image"], "cover.png", { type: "image/png" }))).toBeNull();
    expect(validateCoverSourceFile(new File(["image"], "cover.gif", { type: "image/gif" }))).toBe(
      "形象照片只支持 PNG 或 JPEG"
    );
    expect(
      validateCoverSourceFile(
        new File([new Uint8Array(CHARACTER_CARD_COVER_MAX_SOURCE_BYTES + 1)], "cover.jpg", { type: "image/jpeg" })
      )
    ).toBe("原图不能超过 20MB");
  });

  it("按裁剪区域导出 1200x1560 的 JPEG 成品文件", async () => {
    const originalCreateElement = document.createElement.bind(document);
    const toBlob = vi.fn((callback: BlobCallback, type?: string, quality?: unknown) => {
      callback(new Blob(["jpeg"], { type }));
      expect(type).toBe("image/jpeg");
      expect(quality).toBe(0.86);
    });
    const drawImage = vi.fn();
    vi.spyOn(document, "createElement").mockImplementation((tagName: string) => {
      if (tagName === "canvas") {
        return {
          width: 0,
          height: 0,
          getContext: () => ({ drawImage }),
          toBlob
        } as unknown as HTMLCanvasElement;
      }
      return originalCreateElement(tagName);
    });

    const file = await createCroppedCoverFile({
      image: { naturalWidth: 2000, naturalHeight: 2600 } as HTMLImageElement,
      cropPixels: { x: 100, y: 120, width: 1000, height: 1300 }
    });

    expect(file.name).toBe("icon.jpg");
    expect(file.type).toBe("image/jpeg");
    expect(drawImage).toHaveBeenCalledWith(
      expect.any(Object),
      100,
      120,
      1000,
      1300,
      0,
      0,
      1200,
      1560
    );
  });

  it("压缩阶梯仍超过 2MB 时返回明确错误", async () => {
    const originalCreateElement = document.createElement.bind(document);
    vi.spyOn(document, "createElement").mockImplementation((tagName: string) => {
      if (tagName === "canvas") {
        return {
          width: 0,
          height: 0,
          getContext: () => ({ drawImage: vi.fn() }),
          toBlob: (callback: BlobCallback, type?: string) => {
            callback(new Blob([new Uint8Array(CHARACTER_CARD_COVER_MAX_OUTPUT_BYTES + 1)], { type }));
          }
        } as unknown as HTMLCanvasElement;
      }
      return originalCreateElement(tagName);
    });

    await expect(
      createCroppedCoverFile({
        image: { naturalWidth: 2000, naturalHeight: 2600 } as HTMLImageElement,
        cropPixels: { x: 0, y: 0, width: 1000, height: 1300 }
      })
    ).rejects.toThrow("裁剪后的形象照片仍超过 2MB，请换一张图或重新裁剪");
  });
});
