import { readFile, access } from "node:fs/promises";
import { resolve, dirname, relative, isAbsolute } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { validateReleaseManifest } from "../assets/js/release-manifest.js";

const siteRoot = fileURLToPath(new URL("../", import.meta.url));

/**
 * 检查官网、公开 README 的资源和下载清单，阻止缺文件或错误链接进入公开部署。
 * @returns {Promise<void>} 全部校验通过后结束；失败时抛出错误。
 */
export async function checkSite() {
  const htmlPath = resolve(siteRoot, "index.html");
  const html = await readFile(htmlPath, "utf8");
  const readme = await readFile(resolve(siteRoot, "README.md"), "utf8");
  const manifest = JSON.parse(await readFile(resolve(siteRoot, "data/release.json"), "utf8"));
  validateReleaseManifest(manifest);

  const ids = [...html.matchAll(/\bid="([^"]+)"/g)].map((match) => match[1]);
  if (ids.length !== new Set(ids).size) throw new Error("HTML 中存在重复的元素 ID。");
  if (!html.includes('lang="zh-CN"') || !html.includes('name="viewport"')) {
    throw new Error("HTML 缺少中文语言或移动端 viewport 声明。");
  }

  // README 与官网共用公开图片，校验两处引用可以及时发现改名后留下的断图。
  const references = [html, readme].flatMap((content) =>
    [...content.matchAll(/\b(?:href|src)="([^"]*)"/g)].map((match) => match[1]),
  );
  for (const reference of references) {
    if (reference.startsWith("https://")) continue;
    if (reference.startsWith("#")) {
      if (reference.length > 1 && !ids.includes(reference.slice(1))) {
        throw new Error(`页面锚点不存在：${reference}`);
      }
      continue;
    }
    if (!reference.startsWith("./")) throw new Error(`本地资源必须使用相对地址：${reference}`);
    const target = resolve(dirname(htmlPath), reference.split(/[?#]/, 1)[0]);
    const relativePath = relative(siteRoot, target);
    if (relativePath.startsWith("..") || isAbsolute(relativePath)) {
      throw new Error(`资源路径超出了网站目录：${reference}`);
    }
    await access(target);
  }
  await access(resolve(siteRoot, "assets/js/release-manifest.js"));
  console.log(`校验通过：HTML 锚点、官网与 README 资源及 v${manifest.version} 发行清单（${manifest.status}）。`);
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  await checkSite();
}
