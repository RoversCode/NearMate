/**
 * 当前仓库内随应用一起打包的 Cubism Core 运行时，最高只支持到 moc v5。
 * 这个上限来自 `public/live2d/runtime/live2dcubismcore.min.js` 当前暴露的版本常量，
 * 因此 main 目录扫描与 renderer 运行时预检都必须共享同一份判断口径。
 */
export const BUNDLED_CUBISM_CORE_MAX_MOC_VERSION = 5;

/**
 * MOC3_HEADER_BYTE_LENGTH 表示读取 moc3 版本所需的最小头长度。
 * 前 4 个字节是 `MOC3` 魔数，后 4 个字节是 little-endian 版本号。
 */
const MOC3_HEADER_BYTE_LENGTH = 8;

/**
 * readCubismMocVersionFromBuffer 负责从 moc3 二进制头里提取版本号。
 * 核心入参是二进制 buffer 或其视图；输出为正整数版本号，无法识别时返回 null。
 * 边界异常：头长度不足、魔数不匹配、或版本号不是正整数时，都按“无法识别”处理，避免错误拦截正常模型。
 */
export function readCubismMocVersionFromBuffer(
  input: ArrayBufferLike | ArrayBufferView
): number | null {
  const normalizedView = normalizeToUint8Array(input);

  if (normalizedView.byteLength < MOC3_HEADER_BYTE_LENGTH) {
    return null;
  }

  /**
   * moc3 文件头必须以 `MOC3` 开头。
   * 只有确认魔数成立后，后续版本号才有读取意义。
   */
  const headerMagic = String.fromCharCode(
    normalizedView[0] ?? 0,
    normalizedView[1] ?? 0,
    normalizedView[2] ?? 0,
    normalizedView[3] ?? 0
  );

  if (headerMagic !== "MOC3") {
    return null;
  }

  const versionDataView = new DataView(
    normalizedView.buffer,
    normalizedView.byteOffset,
    normalizedView.byteLength
  );
  const mocVersion = versionDataView.getUint32(4, true);

  if (!Number.isInteger(mocVersion) || mocVersion <= 0) {
    return null;
  }

  return mocVersion;
}

/**
 * isCubismMocVersionSupported 统一判断当前 bundled core 能否加载指定 moc 版本。
 * 核心入参是待判断的 moc 版本与可选上限；输出为布尔值。
 */
export function isCubismMocVersionSupported(
  mocVersion: number,
  maxSupportedMocVersion = BUNDLED_CUBISM_CORE_MAX_MOC_VERSION
): boolean {
  return Number.isInteger(mocVersion) && mocVersion > 0 && mocVersion <= maxSupportedMocVersion;
}

/**
 * createUnsupportedCubismMocVersionMessage 统一生成“模型版本超出运行时支持范围”的用户可读文案。
 * 这样 panel、avatar 与终端日志可以共用同一段错误语义，避免多处手写文案漂移。
 */
export function createUnsupportedCubismMocVersionMessage(
  mocVersion: number,
  maxSupportedMocVersion = BUNDLED_CUBISM_CORE_MAX_MOC_VERSION
): string {
  return `当前内置 Live2D Core 不支持该人物模型版本（moc v${mocVersion}，当前最高支持 v${maxSupportedMocVersion}）。`;
}

/**
 * normalizeToUint8Array 负责把 ArrayBuffer 或其视图收敛成统一的 Uint8Array。
 * 这一步只做表示层归一化，不改变底层字节内容，便于 main 与 renderer 共享同一份解析逻辑。
 */
function normalizeToUint8Array(input: ArrayBufferLike | ArrayBufferView): Uint8Array {
  if (ArrayBuffer.isView(input)) {
    return new Uint8Array(input.buffer, input.byteOffset, input.byteLength);
  }

  return new Uint8Array(input);
}
