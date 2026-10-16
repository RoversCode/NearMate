type MaterialThumbnailImage = {
  isEmpty(): boolean;
  toDataURL(): string;
};

type MaterialContentThumbnailSize = {
  width: number;
  height: number;
};

type MaterialFileIconOptions = {
  size: "normal";
};

export type ResolveMaterialThumbnailInput = {
  path: string;
  getContentThumbnail(path: string, size: MaterialContentThumbnailSize): Promise<MaterialThumbnailImage>;
  getFileIcon(path: string, options: MaterialFileIconOptions): Promise<MaterialThumbnailImage>;
};

const CONTENT_THUMBNAIL_SIZE = {
  width: 96,
  height: 96
} as const;

/**
 * resolveMaterialThumbnailDataUrl 解析材料缩略图 data URL。
 *
 * Args:
 *   input: 本地路径、内容缩略图读取器与系统文件图标读取器。
 *
 * Returns:
 *   可直接给 renderer img 使用的 data URL；所有系统能力失败时返回 null。
 */
export async function resolveMaterialThumbnailDataUrl(
  input: ResolveMaterialThumbnailInput
): Promise<string | null> {
  /**
   * 业务意图：图片、视频、PDF 等优先显示真实内容缩略图；
   * 普通文档或快捷方式没有内容预览时，再退到 Windows 系统关联图标。
   */
  const contentThumbnailDataUrl = await readImageDataUrl(() =>
    input.getContentThumbnail(input.path, CONTENT_THUMBNAIL_SIZE)
  );
  if (contentThumbnailDataUrl) {
    return contentThumbnailDataUrl;
  }

  return readImageDataUrl(() =>
    input.getFileIcon(input.path, {
      size: "normal"
    })
  );
}

async function readImageDataUrl(readImage: () => Promise<MaterialThumbnailImage>): Promise<string | null> {
  try {
    const image = await readImage();
    if (image.isEmpty()) {
      return null;
    }

    return image.toDataURL();
  } catch {
    return null;
  }
}
