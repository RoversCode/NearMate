import type { Area } from "react-easy-crop";

export const CHARACTER_CARD_COVER_ASPECT = 4 / 5.2;
export const CHARACTER_CARD_COVER_MAX_SOURCE_BYTES = 20 * 1024 * 1024;
export const CHARACTER_CARD_COVER_MAX_OUTPUT_BYTES = 2 * 1024 * 1024;

const COVER_OUTPUT_WIDTH = 1200;
const COVER_OUTPUT_HEIGHT = 1560;
const COVER_JPEG_QUALITIES = [0.86, 0.78, 0.7, 0.62] as const;

export type CoverImagePreview = {
  image: HTMLImageElement;
  objectUrl: string;
};

export type CroppedCoverInput = {
  image: HTMLImageElement;
  cropPixels: Area;
};

/**
 * 校验用户选择的封面原图。
 *
 * Args:
 *   file: 浏览器文件选择器返回的原始图片。
 *
 * Returns:
 *   合法时返回 null，否则返回可直接展示的中文错误。
 */
export function validateCoverSourceFile(file: File): string | null {
  if (file.type !== "image/png" && file.type !== "image/jpeg") {
    return "形象照片只支持 PNG 或 JPEG";
  }
  if (file.size > CHARACTER_CARD_COVER_MAX_SOURCE_BYTES) {
    return "原图不能超过 20MB";
  }
  return null;
}

/**
 * 创建可用于裁剪器预览的图片对象和临时 URL。
 *
 * Args:
 *   file: 已通过基础校验的原始图片。
 *
 * Returns:
 *   加载完成的图片元素和需要由调用方释放的 object URL。
 */
export async function createImagePreview(file: File): Promise<CoverImagePreview> {
  const objectUrl = URL.createObjectURL(file);
  const image = new Image();
  image.decoding = "async";
  image.src = objectUrl;

  try {
    await image.decode();
  } catch {
    URL.revokeObjectURL(objectUrl);
    throw new Error("形象照片读取失败，请换一张图片");
  }

  return { image, objectUrl };
}

/**
 * 根据裁剪像素生成统一 JPEG 封面文件。
 *
 * Args:
 *   input: 原图元素和 react-easy-crop 返回的裁剪像素区域。
 *
 * Returns:
 *   可直接提交给后端封面接口的 JPEG 文件。
 */
export async function createCroppedCoverFile(input: CroppedCoverInput): Promise<File> {
  const canvas = document.createElement("canvas");
  canvas.width = COVER_OUTPUT_WIDTH;
  canvas.height = COVER_OUTPUT_HEIGHT;
  const context = canvas.getContext("2d");
  if (context === null) {
    throw new Error("当前浏览器不支持形象照片裁剪");
  }

  // 使用固定输出尺寸让图库卡片、详情预览和本地资产始终拥有一致比例与清晰度。
  context.drawImage(
    input.image,
    input.cropPixels.x,
    input.cropPixels.y,
    input.cropPixels.width,
    input.cropPixels.height,
    0,
    0,
    COVER_OUTPUT_WIDTH,
    COVER_OUTPUT_HEIGHT
  );

  for (const quality of COVER_JPEG_QUALITIES) {
    const blob = await canvasToJpegBlob(canvas, quality);
    if (blob.size <= CHARACTER_CARD_COVER_MAX_OUTPUT_BYTES) {
      return new File([blob], "icon.jpg", { type: "image/jpeg" });
    }
  }

  throw new Error("裁剪后的形象照片仍超过 2MB，请换一张图或重新裁剪");
}

function canvasToJpegBlob(canvas: HTMLCanvasElement, quality: number): Promise<Blob> {
  return new Promise((resolve, reject) => {
    canvas.toBlob(
      (blob) => {
        if (blob === null) {
          reject(new Error("形象照片压缩失败，请换一张图片"));
          return;
        }
        resolve(blob);
      },
      "image/jpeg",
      quality
    );
  });
}
