import { readFile, readdir, realpath, stat, lstat } from "node:fs/promises";
import { resolve, dirname, relative, isAbsolute, sep, extname } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { validateReleaseManifest } from "../assets/js/release-manifest.js";

const defaultSiteRoot = fileURLToPath(new URL("../", import.meta.url));

/** 公开产物的唯一白名单；检查与复制必须覆盖同一批目录，避免漏检未引用资源。 */
export const PUBLIC_SITE_ENTRIES = ["index.html", "guide", "assets", "data", ".nojekyll"];

/**
 * 检查每一页教程、官网和公开 README，避免断图、坏锚点或越界文件进入部署。
 * @param {object} [options] 可选的站点目录和输出函数，方便对独立样例执行检查。
 * @param {string} [options.rootDirectory] 网站源文件所在目录，默认使用当前仓库。
 * @param {(message: string) => void} [options.log] 校验通过后的输出函数。
 * @returns {Promise<void>} 所有页面、资源和发行清单校验通过后结束；失败时抛出错误。
 */
export async function checkSite({ rootDirectory = defaultSiteRoot, log = console.log } = {}) {
  const siteRoot = await realpath(rootDirectory);
  // 构建会复制整个资源目录，页面没有引用的文件也必须属于本站，不能夹带外部链接。
  for (const entry of PUBLIC_SITE_ENTRIES) {
    await checkPublicTree(siteRoot, resolve(siteRoot, entry));
  }
  const manifest = JSON.parse(await readFile(resolve(siteRoot, "data/release.json"), "utf8"));
  validateReleaseManifest(manifest);

  const pagePaths = [resolve(siteRoot, "index.html"), ...await findGuidePages(resolve(siteRoot, "guide"))];
  const pages = new Map();

  // 先收齐每页的 ID，再检查跨页链接，文件遍历顺序不会影响锚点判断。
  for (const pagePath of pagePaths) {
    const html = await readFile(pagePath, "utf8");
    const label = relative(siteRoot, pagePath);
    if (!/<title>\s*[^<]*\S[^<]*<\/title>/i.test(html)) throw new Error(`${label} 缺少页面标题。`);
    if (!/<html\b[^>]*\blang=["']zh-CN["']/i.test(html) || !/<meta\b[^>]*\bname=["']viewport["']/i.test(html)) {
      throw new Error(`${label} 缺少中文语言或移动端 viewport 声明。`);
    }
    const ids = [...html.matchAll(/\bid=["']([^"']+)["']/g)].map((match) => match[1]);
    if (ids.length !== new Set(ids).size) throw new Error(`${label} 存在重复的元素 ID。`);
    pages.set(pagePath, { ids: new Set(ids), references: readReferences(html) });
  }

  const readmePath = resolve(siteRoot, "README.md");
  const referencesByFile = [...pages].map(([path, page]) => [path, page.references]);
  referencesByFile.push([readmePath, readReferences(await readFile(readmePath, "utf8"))]);
  for (const [sourcePath, references] of referencesByFile) {
    for (const reference of references) {
      if (reference.startsWith("https://")) continue;
      const [pathAndQuery, encodedFragment = ""] = reference.split("#", 2);
      const encodedPath = pathAndQuery.split("?", 1)[0];
      if (encodedPath && !encodedPath.startsWith("./") && !encodedPath.startsWith("../")) {
        throw new Error(`${relative(siteRoot, sourcePath)} 的本地资源必须使用相对地址：${reference}`);
      }
      const decodedPath = decodeURIComponent(encodedPath);
      if (decodedPath.includes("\\")) throw new Error(`资源路径不能包含反斜杠：${reference}`);
      let target = decodedPath ? resolve(dirname(sourcePath), decodedPath) : sourcePath;
      ensureInsideSite(siteRoot, target, reference);
      let targetInfo = await stat(target);
      if (targetInfo.isDirectory()) {
        target = resolve(target, "index.html");
        targetInfo = await stat(target);
      }
      if (!targetInfo.isFile()) throw new Error(`资源不是普通文件：${reference}`);
      // 目录联接和符号链接也必须留在站点内，不能只检查表面上的相对路径。
      ensureInsideSite(siteRoot, await realpath(target), reference);
      // 源码里存在的文档不一定会部署；链接目标必须和构建使用同一份公开白名单。
      const publishedPath = relative(siteRoot, target);
      if (!PUBLIC_SITE_ENTRIES.some(entry => publishedPath === entry || publishedPath.startsWith(`${entry}${sep}`))) {
        throw new Error(`资源未包含在公开发布资源中：${reference}`);
      }
      if (!encodedFragment) continue;
      const fragment = decodeURIComponent(encodedFragment);
      const page = pages.get(target);
      if (!page || !page.ids.has(fragment)) throw new Error(`页面锚点不存在：${reference}`);
    }
  }
  await stat(resolve(siteRoot, "assets/js/release-manifest.js"));
  log(`校验通过：${pages.size} 个 HTML 页面、README、站内资源与锚点及 v${manifest.version} 发行清单（${manifest.status}）。`);
}

/**
 * 找出指南目录中的 HTML，每个页面都必须经过同一套发布检查。
 * @param {string} directory 指南目录或其中的子目录。
 * @returns {Promise<string[]>} 需要检查的 HTML 绝对路径；没有指南目录时返回空列表。
 */
async function findGuidePages(directory) {
  const directoryInfo = await lstat(directory).catch((error) => {
    if (error.code === "ENOENT") return null;
    throw error;
  });
  if (!directoryInfo) return [];
  if (directoryInfo.isSymbolicLink()) throw new Error("指南目录不能包含符号链接或目录联接。");
  const entries = await readdir(directory, { withFileTypes: true });
  const paths = [];
  for (const entry of entries) {
    const path = resolve(directory, entry.name);
    if (entry.isSymbolicLink()) throw new Error(`指南目录不能包含符号链接：${entry.name}`);
    if (entry.isDirectory()) paths.push(...await findGuidePages(path));
    else if (extname(entry.name).toLowerCase() === ".html") paths.push(path);
  }
  return paths;
}

/**
 * 检查整个公开复制树，拒绝目录联接、符号链接和非普通文件。
 * @param {string} siteRoot 网站真实根目录。
 * @param {string} path 白名单目录或其中一项资源的绝对路径。
 * @returns {Promise<void>} 当前资源及其全部子项安全时结束，否则抛出错误。
 */
async function checkPublicTree(siteRoot, path) {
  const info = await lstat(path).catch(error => {
    if (error.code === "ENOENT") return null;
    throw error;
  });
  if (!info) return;
  const label = relative(siteRoot, path);
  if (info.isSymbolicLink()) {
    if (label === "guide" || label.startsWith(`guide${sep}`)) {
      throw new Error(`指南目录不能包含符号链接或目录联接：${label}`);
    }
    ensureInsideSite(siteRoot, await realpath(path), label);
    throw new Error(`公开资源不能包含符号链接或目录联接：${label}`);
  }
  ensureInsideSite(siteRoot, await realpath(path), label);
  if (info.isFile()) return;
  if (!info.isDirectory()) throw new Error(`公开资源不是普通文件或目录：${label}`);
  for (const entry of await readdir(path)) {
    await checkPublicTree(siteRoot, resolve(path, entry));
  }
}

/**
 * 收集静态 HTML 和 README 引用；外链保留给发布后的在线验收。
 * @param {string} content 页面或 README 的原文。
 * @returns {string[]} href、src 及 Markdown 链接中的地址。
 */
function readReferences(content) {
  return [
    ...content.matchAll(/\b(?:href|src)=["']([^"']*)["']/g),
    ...content.matchAll(/!?\[[^\]]*\]\(([^\s)]+)(?:\s+["'][^"']*["'])?\)/g),
  ].map((match) => match[1]);
}

/**
 * 阻止教程链接指向网站之外的文件，即使相对路径看起来合法也要核对边界。
 * @param {string} siteRoot 网站的真实根目录。
 * @param {string} target 待检查的绝对路径。
 * @param {string} reference 用于报错的原始链接。
 * @returns {void} 路径在站点内时继续，否则抛出错误。
 */
function ensureInsideSite(siteRoot, target, reference) {
  const relativePath = relative(siteRoot, target);
  if (relativePath === ".." || relativePath.startsWith(`..${sep}`) || isAbsolute(relativePath)) {
    throw new Error(`资源路径超出了网站目录：${reference}`);
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  await checkSite();
}
