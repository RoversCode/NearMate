import { describe, expect, it, vi } from "vitest";
import { resolveMaterialThumbnailDataUrl } from "./materialThumbnailService";

function createImage(dataUrl: string, isEmpty = false) {
  return {
    isEmpty: vi.fn(() => isEmpty),
    toDataURL: vi.fn(() => dataUrl)
  };
}

describe("resolveMaterialThumbnailDataUrl", () => {
  it("内容缩略图成功时直接返回缩略图，不再读取系统图标", async () => {
    const contentThumbnail = createImage("data:image/png;base64,content");
    const getContentThumbnail = vi.fn(async () => contentThumbnail);
    const getFileIcon = vi.fn(async () => createImage("data:image/png;base64,icon"));

    const result = await resolveMaterialThumbnailDataUrl({
      path: "G:\\docs\\photo.png",
      getContentThumbnail,
      getFileIcon
    });

    expect(getContentThumbnail).toHaveBeenCalledWith("G:\\docs\\photo.png", {
      width: 96,
      height: 96
    });
    expect(getFileIcon).not.toHaveBeenCalled();
    expect(result).toBe("data:image/png;base64,content");
  });

  it("内容缩略图为空时返回系统文件图标", async () => {
    const getContentThumbnail = vi.fn(async () => createImage("data:image/png;base64,empty", true));
    const getFileIcon = vi.fn(async () => createImage("data:image/png;base64,icon"));

    const result = await resolveMaterialThumbnailDataUrl({
      path: "G:\\docs\\note.txt",
      getContentThumbnail,
      getFileIcon
    });

    expect(getFileIcon).toHaveBeenCalledWith("G:\\docs\\note.txt", {
      size: "normal"
    });
    expect(result).toBe("data:image/png;base64,icon");
  });

  it("内容缩略图读取失败时仍返回系统文件图标", async () => {
    const getContentThumbnail = vi.fn(async () => {
      throw new Error("thumbnail failed");
    });
    const getFileIcon = vi.fn(async () => createImage("data:image/png;base64,icon"));

    const result = await resolveMaterialThumbnailDataUrl({
      path: "G:\\docs\\shortcut.lnk",
      getContentThumbnail,
      getFileIcon
    });

    expect(getFileIcon).toHaveBeenCalledWith("G:\\docs\\shortcut.lnk", {
      size: "normal"
    });
    expect(result).toBe("data:image/png;base64,icon");
  });

  it("系统文件图标为空或读取失败时返回 null", async () => {
    const emptyIconResult = await resolveMaterialThumbnailDataUrl({
      path: "G:\\docs\\empty.bin",
      getContentThumbnail: vi.fn(async () => createImage("data:image/png;base64,empty", true)),
      getFileIcon: vi.fn(async () => createImage("data:image/png;base64,icon", true))
    });
    const failedIconResult = await resolveMaterialThumbnailDataUrl({
      path: "G:\\docs\\failed.bin",
      getContentThumbnail: vi.fn(async () => createImage("data:image/png;base64,empty", true)),
      getFileIcon: vi.fn(async () => {
        throw new Error("icon failed");
      })
    });

    expect(emptyIconResult).toBeNull();
    expect(failedIconResult).toBeNull();
  });
});
