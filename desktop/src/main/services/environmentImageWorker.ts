import { parentPort } from "node:worker_threads";
import sharp from "sharp";

type EncodeRequest = {
  requestId: number;
  width: number;
  height: number;
  bitmap: ArrayBuffer;
};

if (parentPort === null) {
  throw new Error("environment image worker requires a parent port");
}

parentPort.on("message", (request: EncodeRequest) => {
  void encodePng(request)
    .then(png => {
      const bytes = new ArrayBuffer(png.byteLength);
      new Uint8Array(bytes).set(png);
      parentPort?.postMessage({ requestId: request.requestId, bytes }, [bytes]);
    })
    .catch((error: unknown) => {
      parentPort?.postMessage({
        requestId: request.requestId,
        error: error instanceof Error ? error.message : String(error)
      });
    });
});

/**
 * 把 Windows BGRA 原始位图转换为适合界面文字识别的 PNG。
 *
 * Args:
 *   request: 请求编号、位图尺寸和可转移的原始像素缓冲区。
 *
 * Returns:
 *   编码后的 PNG 字节。
 */
async function encodePng(request: EncodeRequest): Promise<Buffer> {
  const rgba = Buffer.from(request.bitmap);
  for (let offset = 0; offset < rgba.length; offset += 4) {
    const blue = rgba[offset];
    rgba[offset] = rgba[offset + 2] ?? 0;
    rgba[offset + 2] = blue ?? 0;
  }
  return sharp(rgba, {
    raw: { width: request.width, height: request.height, channels: 4 }
  }).png({ compressionLevel: 4 }).toBuffer();
}
