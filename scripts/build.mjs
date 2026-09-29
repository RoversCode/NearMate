import { cp, mkdir, lstat, rm } from "node:fs/promises";
import { resolve, dirname, basename } from "node:path";
import { fileURLToPath } from "node:url";
import { checkSite } from "./check.mjs";

const siteRoot = fileURLToPath(new URL("../", import.meta.url));
const outputDirectory = resolve(siteRoot, "dist");

await checkSite();

// 只清理仓库自己的 dist，拒绝符号链接，避免误删旁边的项目或用户目录。
if (dirname(outputDirectory) !== resolve(siteRoot) || basename(outputDirectory) !== "dist") {
  throw new Error("网站输出目录不在预期位置。");
}
const existingOutput = await lstat(outputDirectory).catch((error) => {
  if (error.code === "ENOENT") return null;
  throw error;
});
if (existingOutput?.isSymbolicLink()) throw new Error("dist 不得是符号链接或目录联接。");
await rm(outputDirectory, { recursive: true, force: true });
await mkdir(outputDirectory, { recursive: true });

// 明确列出公开资源，不把仓库文档、工作流或任何安装包复制到 Pages。
for (const entry of ["index.html", "assets", "data", ".nojekyll"]) {
  await cp(resolve(siteRoot, entry), resolve(outputDirectory, entry), { recursive: true });
}
console.log(`静态网站已生成：${outputDirectory}`);
