import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, mkdir, writeFile, readFile, rm, symlink } from "node:fs/promises";
import { tmpdir } from "node:os";
import { resolve, dirname, relative, sep, isAbsolute } from "node:path";
import { checkSite } from "./check.mjs";

/**
 * 建立独立小网站，让路径与锚点检查覆盖真实文件结构。
 * @param {import('node:test').TestContext} context 当前测试及清理登记入口。
 * @returns {Promise<{rootDirectory: string, container: string}>} 样例站点和仅供本测试使用的临时目录。
 */
async function createSite(context) {
  const temporaryRoot = resolve(tmpdir());
  const container = await mkdtemp(resolve(temporaryRoot, "nearmate-site-check-"));
  const rootDirectory = resolve(container, "site");
  context.after(async () => {
    // 清理前确认仍是本测试在系统临时目录创建的独占目录。
    const cleanupPath = relative(temporaryRoot, container);
    assert.ok(cleanupPath.startsWith("nearmate-site-check-") && !cleanupPath.includes(sep) && !isAbsolute(cleanupPath));
    await rm(container, { recursive: true, force: true });
  });
  const manifest = {
    schemaVersion: 1, version: "0.1.0", status: "pending", publishedAt: null,
    releaseUrl: "https://github.com/RoversCode/NearMate/releases", checksumsUrl: null,
    profiles: { universal: { assets: [] }, cuda: { assets: [] } },
  };
  const files = {
    "index.html": page('<a href="./guide/#topic">手册</a><h1 id="main">首页</h1>'),
    "guide/index.html": page('<link rel="stylesheet" href="../assets/site.css?v=1"><h1 id="topic">主题</h1><a href="./next.html#voice">继续</a>'),
    "guide/next.html": page('<h1 id="voice">语音</h1><a href="../index.html#main">官网</a>'),
    "README.md": "[手册](./guide/index.html#topic)",
    "assets/site.css": "body { margin: 0; }",
    "assets/js/release-manifest.js": "export {};",
    "data/release.json": JSON.stringify(manifest),
  };
  for (const [name, content] of Object.entries(files)) {
    const path = resolve(rootDirectory, name);
    await mkdir(dirname(path), { recursive: true });
    await writeFile(path, content);
  }
  return { rootDirectory, container };
}

/**
 * 给样例正文补上必需页面元数据，测试只需表达关注的链接或内容。
 * @param {string} content 样例页面正文。
 * @returns {string} 含中文语言、标题和 viewport 的 HTML。
 */
function page(content) {
  return `<!doctype html><html lang="zh-CN"><head><title>测试手册</title><meta name="viewport" content="width=device-width"></head><body>${content}</body></html>`;
}

test("允许嵌套页引用站内父目录资源、带查询参数的资源和跨页锚点", async (context) => {
  const { rootDirectory } = await createSite(context);
  await checkSite({ rootDirectory, log: () => {} });
});

for (const [name, content, expected] of [
  ["缺少资源", '<img src="../assets/missing.png">', /ENOENT/],
  ["当前页错误锚点", '<a href="#missing">链接</a>', /页面锚点不存在/],
  ["跨页错误锚点", '<a href="./next.html#missing">链接</a>', /页面锚点不存在/],
  ["根路径破坏 Pages 子路径", '<a href="/guide/">链接</a>', /必须使用相对地址/],
  ["相对路径越界", '<a href="../../outside.txt">链接</a>', /超出了网站目录/],
  ["编码路径越界", '<a href="../%2e%2e/outside.txt">链接</a>', /超出了网站目录/],
  ["反斜杠路径", '<a href="..\\outside.txt">链接</a>', /必须使用相对地址/],
  ["重复 ID", '<h1 id="same">一</h1><h2 id="same">二</h2>', /重复的元素 ID/],
]) {
  test(`拒绝${name}`, async (context) => {
    const { rootDirectory } = await createSite(context);
    await writeFile(resolve(rootDirectory, "guide/next.html"), page(`<h1 id="voice">语音</h1>${content}`));
    await assert.rejects(checkSite({ rootDirectory, log: () => {} }), expected);
  });
}

for (const [name, replaced, replacement] of [
  ["标题", "<title>测试手册</title>", "<title> </title>"],
  ["语言", 'lang="zh-CN"', 'lang="en"'],
  ["viewport", 'name="viewport"', 'name="other"'],
]) {
  test(`拒绝指南页面缺少${name}`, async (context) => {
    const { rootDirectory } = await createSite(context);
    const path = resolve(rootDirectory, "guide/next.html");
    await writeFile(path, (await readFile(path, "utf8")).replace(replaced, replacement));
    await assert.rejects(checkSite({ rootDirectory, log: () => {} }), /缺少/);
  });
}

test("README 的 Markdown 资源也必须存在", async (context) => {
  const { rootDirectory } = await createSite(context);
  await writeFile(resolve(rootDirectory, "README.md"), "![截图](./assets/missing.png)");
  await assert.rejects(checkSite({ rootDirectory, log: () => {} }), /ENOENT/);
});

test("拒绝引用源码中存在但不会部署的 README", async (context) => {
  const { rootDirectory } = await createSite(context);
  await writeFile(resolve(rootDirectory, "guide/next.html"), page('<h1 id="voice">语音</h1><a href="../README.md">仓库说明</a>'));
  await assert.rejects(checkSite({ rootDirectory, log: () => {} }), /未包含在公开发布资源中/);
});

test("拒绝通过目录联接读取站点外的资源", async (context) => {
  const { rootDirectory, container } = await createSite(context);
  const outside = resolve(container, "outside");
  await mkdir(outside);
  await writeFile(resolve(outside, "private.txt"), "不能公开");
  await symlink(outside, resolve(rootDirectory, "assets/external"), process.platform === "win32" ? "junction" : "dir");
  await writeFile(resolve(rootDirectory, "guide/next.html"), page('<h1 id="voice">语音</h1><a href="../assets/external/private.txt">越界</a>'));
  await assert.rejects(checkSite({ rootDirectory, log: () => {} }), /超出了网站目录/);
});

test("拒绝复制树内未被页面引用的外部目录联接", async (context) => {
  const { rootDirectory, container } = await createSite(context);
  const outside = resolve(container, "unreferenced-private-assets");
  await mkdir(outside);
  await writeFile(resolve(outside, "private.txt"), "不能因为页面没有引用就把它公开");
  await symlink(outside, resolve(rootDirectory, "assets/unreferenced"), process.platform === "win32" ? "junction" : "dir");
  await assert.rejects(checkSite({ rootDirectory, log: () => {} }), /符号链接|目录联接|超出了网站目录/);
});

test("拒绝把指南根目录替换成站点外的目录联接", async (context) => {
  const { rootDirectory, container } = await createSite(context);
  const outside = resolve(container, "outside-guide");
  await mkdir(outside);
  await writeFile(resolve(outside, "index.html"), page('<h1 id="topic">私有页面</h1>'));
  const guide = resolve(rootDirectory, "guide");
  // guide 是样例站点中明确创建的目录，不使用用户路径或计算出的上级目录。
  assert.equal(dirname(guide), rootDirectory);
  await rm(guide, { recursive: true });
  await symlink(outside, guide, process.platform === "win32" ? "junction" : "dir");
  await assert.rejects(checkSite({ rootDirectory, log: () => {} }), /指南目录不能包含/);
});
