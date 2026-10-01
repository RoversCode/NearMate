# 官网维护

网站使用原生 HTML、CSS 与少量 JavaScript，不依赖前端框架或第三方运行时包。需要 Node.js 22 或更新版本执行校验与构建，不需要 `npm install`。

## 本地校验与预览

```powershell
npm run check
npm run build
python -m http.server 4173 --bind 127.0.0.1 --directory dist
```

在浏览器打开 [localhost:4173](http://localhost:4173/)。通过 HTTP 预览，避免浏览器阻止直接读取下载清单。

```text
index.html                      页面内容、语义结构与 FAQ
assets/css/site.css             响应式布局、主题和无障碍状态
assets/js/site.js               手机导航、安装版本选择和下载列表
assets/js/release-manifest.js   浏览器与构建共用的发行清单校验
assets/images/                 品牌图与真实社区二维码
data/release.json               当前版本、下载附件、大小与 SHA256
scripts/check.mjs               本地资源、页面锚点与发行清单校验
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

## 发行维护

参见 [发行与下载清单维护](../releases/PUBLISHING.md)。只有完整安装文件已经发布并验证后，才能将下载清单切换为 `published`。

公开 README 面向用户，只提供产品能力、下载、首次使用、费用、社区与反馈信息。维护流程放在本目录和 `releases/`，不作为用户主页面的教程。
