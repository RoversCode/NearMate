import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFile, readdir } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const scriptPath = fileURLToPath(import.meta.url);
const workspaceRoot = path.resolve(path.dirname(scriptPath), "..");
const sourcePublicRoot = path.join(workspaceRoot, "desktop/src/renderer/public");
const assetDirectory = "live2d/resources/DeepSeekChibi";

/**
 * 检查内置 Q 版模型的引用、文件摘要和随包许可，防止源码或构建产物缺件。
 *
 * @param {string} rendererRoot 待检查的 public 目录或 renderer 构建输出目录。
 * @returns {Promise<object>} 已验证的文件数量、大小与固定上游版本。
 */
export async function verifyDeepSeekChibiAssets(rendererRoot = sourcePublicRoot) {
  const sourceRoot = path.join(sourcePublicRoot, assetDirectory);
  const targetRoot = path.join(path.resolve(rendererRoot), assetDirectory);
  const manifestBytes = await readFile(path.join(sourceRoot, "asset-manifest.json"));
  const manifest = JSON.parse(manifestBytes.toString("utf8"));
  const expectedNames = new Set(["asset-manifest.json", "NOTICE.txt"]);

  // 构建产物必须服从源码中审查过的清单，不能用产物自己生成的摘要证明自己正确。
  assert.deepEqual(await readFile(path.join(targetRoot, "asset-manifest.json")), manifestBytes,
    "构建产物的素材清单与源码不一致");
  assert.deepEqual(await readFile(path.join(targetRoot, "NOTICE.txt")),
    await readFile(path.join(sourceRoot, "NOTICE.txt")), "构建产物缺少完整素材署名与来源说明");

  let totalBytes = 0;
  for (const file of manifest.files) {
    assert.equal(typeof file.path, "string", "素材清单必须使用相对路径");
    assert.ok(!path.isAbsolute(file.path) && !file.path.includes("\\")
      && file.path.split("/").every(segment => segment !== ".." && segment !== "." && segment !== ""),
    `素材路径越界或不规范：${file.path}`);
    assert.ok(!expectedNames.has(file.path), `素材清单路径重复：${file.path}`);
    expectedNames.add(file.path);

    const bytes = await readFile(path.join(targetRoot, file.path));
    assert.equal(bytes.length, file.bytes, `素材大小不符：${file.path}`);
    assert.equal(createHash("sha256").update(bytes).digest("hex"), file.sha256,
      `素材 SHA256 不符：${file.path}`);
    assert.equal(createHash("sha1").update(`blob ${bytes.length}\0`).update(bytes).digest("hex"),
      file.gitBlobSha1, `素材与固定上游 Git blob 不符：${file.path}`);
    totalBytes += bytes.length;
  }

  const model = JSON.parse(await readFile(path.join(targetRoot, manifest.modelFile), "utf8"));
  const refs = model.FileReferences;
  const modelReferences = [
    refs.Moc, refs.Physics, refs.DisplayInfo, ...refs.Textures,
    ...Object.values(refs.Motions).flat().map(motion => motion.File),
    ...refs.Expressions.map(expression => expression.File)
  ];
  const modelAndLicenseFiles = new Set([
    manifest.modelFile, ...modelReferences, "LICENSE", "使用须知.txt"
  ]);
  assert.deepEqual([...modelAndLicenseFiles].sort(), manifest.files.map(file => file.path).sort(),
    "完整性清单必须恰好覆盖模型入口、全部引用和两份上游许可文件");

  // 目录中的额外文件也会被 Vite 打包；拒绝混入私人素材或上游插件配置。
  const actualNames = await readdir(targetRoot, { recursive: true, withFileTypes: true });
  assert.ok(actualNames.every(entry => entry.isFile() || entry.isDirectory()),
    "素材目录不能包含链接或其它无法直接校验的条目");
  const actualFiles = actualNames.filter(entry => entry.isFile()).map(entry =>
    path.relative(targetRoot, path.join(entry.parentPath, entry.name)).split(path.sep).join("/"));
  assert.deepEqual(actualFiles.sort(), [...expectedNames].sort(),
    "素材目录缺少必要文件或混入了清单外内容");

  return {
    modelId: "DeepSeekChibi",
    fileCount: expectedNames.size,
    modelReferenceCount: modelReferences.length,
    upstreamFileBytes: totalBytes,
    upstreamRevision: manifest.upstreamRevision
  };
}

if (process.argv[1] && path.resolve(process.argv[1]) === scriptPath) {
  try {
    assert.ok(process.argv.length <= 3,
      "用法：node scripts/verify-live2d-assets.mjs [renderer-public-or-build-root]");
    console.log(JSON.stringify(await verifyDeepSeekChibiAssets(process.argv[2]), null, 2));
  } catch (error) {
    console.error(error);
    process.exitCode = 1;
  }
}
