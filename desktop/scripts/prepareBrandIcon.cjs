const { mkdir, writeFile } = require("node:fs/promises");
const { resolve, dirname } = require("node:path");
const sharp = require("sharp");

/**
 * 在打包前把唯一品牌 PNG 转成 Windows 可执行文件和安装器共用的多尺寸 ICO。
 *
 * Args:
 *   context: electron-builder 提供的构建上下文，packager.projectDir 指向 desktop 目录。
 *
 * Returns:
 *   Promise<void>；生成 desktop/dist-brand/icon.ico，素材缺失或损坏时拒绝继续打包。
 */
module.exports = async function prepareBrandIcon(context) {
  const projectDir = context.packager.projectDir;
  const sourcePath = resolve(projectDir, "../assets/icon.png");
  const outputPath = resolve(projectDir, "dist-brand/icon.ico");
  const sizes = [16, 24, 32, 48, 64, 128, 256];
  const frames = await Promise.all(sizes.map(size =>
    sharp(sourcePath).resize(size, size, { fit: "contain" }).ensureAlpha().png().toBuffer()
  ));

  // Windows 会按桌面缩放比例选择图层；ICO 目录中的零宽高表示 256，而不是空图片。
  const directory = Buffer.alloc(6 + frames.length * 16);
  directory.writeUInt16LE(1, 2);
  directory.writeUInt16LE(frames.length, 4);
  let frameOffset = directory.length;
  for (const [index, frame] of frames.entries()) {
    const entryOffset = 6 + index * 16;
    directory[entryOffset] = sizes[index] === 256 ? 0 : sizes[index];
    directory[entryOffset + 1] = directory[entryOffset];
    directory.writeUInt16LE(1, entryOffset + 4);
    directory.writeUInt16LE(32, entryOffset + 6);
    directory.writeUInt32LE(frame.length, entryOffset + 8);
    directory.writeUInt32LE(frameOffset, entryOffset + 12);
    frameOffset += frame.length;
  }

  await mkdir(dirname(outputPath), { recursive: true });
  await writeFile(outputPath, Buffer.concat([directory, ...frames]));
};
