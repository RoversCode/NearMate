# 官网维护

网站使用原生 HTML、CSS 与少量 JavaScript，不依赖前端框架或第三方运行时包。需要 Node.js 22 或更新版本执行校验与构建，不需要 `npm install`。

## 本地校验与预览

```powershell
npm run check
npm test
npm run build
python -m http.server 4173 --bind 127.0.0.1 --directory dist
```

在浏览器打开 [localhost:4173](http://localhost:4173/)。通过 HTTP 预览，避免浏览器阻止直接读取下载清单。

```text
index.html                      页面内容、语义结构与 FAQ
guide/                          用户教程的唯一源文件，五个静态 HTML 页面
assets/css/guide.css             教程正文、章节导航和移动端布局
assets/css/site.css             响应式布局、主题和无障碍状态
assets/js/site.js               手机导航、安装版本选择和下载列表
assets/js/release-manifest.js   浏览器与构建共用的发行清单校验
assets/images/                 品牌图与真实社区二维码
data/release.json               当前版本、下载附件、大小与 SHA256
scripts/check.mjs               本地资源、页面锚点与发行清单校验
scripts/check.test.mjs          嵌套路径、跨页锚点和越界文件回归检查
scripts/build.mjs               校验后复制公开资源到 dist
.github/workflows/pages.yml     GitHub Pages 自动部署
releases/                      版本说明、模板与发布流程
```

页面采用系统字体，不加载统计脚本、外部字体或第三方前端库。未发布、网络失败和清单错误时保留 Releases 入口，不推测安装文件地址；禁用 JavaScript 时仍可访问 Releases。

## GitHub Pages

1. 在仓库 **Settings → Pages → Build and deployment** 中选择 **GitHub Actions**。
2. 将官网更新推送到 `main`，或手动运行 **Deploy GitHub Pages** 工作流。
3. 工作流先校验清单和静态资源，只把 `dist` 上传到 Pages。

站点地址为 **https://roverscode.github.io/NearMate/**。所有站内资源采用相对路径，兼容仓库名子路径。大型安装包托管在 GitHub Releases，不放在 Git 或 Pages 产物中。

## 使用手册维护

公开教程只维护 `guide/` 中的 HTML；应用仓库只引用在线地址，避免出现两份正文。`guide/index.html` 是目录，首次初始化、聊天与语音、选配功能、问题排查各有独立页面。教程不加载首页的下载脚本，禁用 JavaScript 也可以阅读。

页面中的本地链接以当前 HTML 所在目录为起点，使用 `./` 或 `../`，不能写 `/assets/` 或 `/guide/`。应用内章节链接使用固定英文 ID，例如 `first-run.html#deepseek`、`first-run.html#qwen`、`chat-and-voice.html#voice`；修改这些 ID 前须同步应用入口。

`npm run check` 会检查首页与指南全部页面的标题、语言、viewport、唯一 ID、本地资源及跨页锚点，拒绝越界路径、指向站点外的符号链接，以及源码中存在但未进入公开白名单的链接目标。构建与链接检查共用同一白名单，仅复制首页、指南、资产、发行清单和 `.nojekyll`。不要把开发文档或测试授权材料加入公开资源。

发布前在 `/NearMate/` 子路径下预览教程，并检查桌面、手机宽度及键盘导航。新增截图应来自改后的真实界面，移除密钥和私人信息；不要制作厂商控制台的示意截图冒充真实操作。

## 发行维护

参见 [发行与下载清单维护](../releases/PUBLISHING.md)。只有完整安装文件已经发布并验证后，才能将下载清单切换为 `published`。

公开 README 面向用户，只提供产品能力、下载、首次使用、费用、社区与反馈信息。维护流程放在本目录和 `releases/`，不作为用户主页面的教程。
