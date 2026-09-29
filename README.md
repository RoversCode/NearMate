# NearMate

**工作在手边，搭子在身边。**

NearMate 是常驻 Windows 桌面的 AI 工作搭子环境。用角色卡定义当前角色，配合 Live2D 与语音交流，围绕当前屏幕、选中文字和本轮材料处理日常小任务；配置后台工作并授权后，还能调用工具推进多步骤工作。

- **官方网站：** [roverscode.github.io/NearMate](https://roverscode.github.io/NearMate/)
- **安装文件与版本说明：** [GitHub Releases](https://github.com/RoversCode/NearMate/releases)
- **问题反馈：** [GitHub Issues](https://github.com/RoversCode/NearMate/issues)

本仓库保存官网与公开发行支持文件。NearMate 应用源码不在这个仓库内；仓库可公开访问，不代表应用或所含素材自动获得开源许可。

## 下载与安装

当前面向 **Windows x64**，提供两个独立安装版本：

| 版本 | 适用方向 | 运行环境 |
| --- | --- | --- |
| CUDA | 配备兼容 NVIDIA 显卡的电脑 | CUDA |
| Universal | AMD / Intel 等设备；通过首次向导确认支持情况 | DirectML、Vulkan、CPU |

1. 在官网或 Releases 选择一个版本。
2. 下载该版本的安装程序 `.exe` 和**全部** `.bin` 分卷。
3. 把这些文件放在同一文件夹，保留原文件名，再运行 `.exe`。不要混用 CUDA 与 Universal 分卷。
4. 跟随应用内向导完成授权、模型 API 配置、设备检查、语音模型下载和试听。

Releases 同时提供 `SHA256SUMS.txt`，可用 PowerShell 核对文件摘要：

```powershell
Get-FileHash -Algorithm SHA256 -LiteralPath '你下载的安装文件完整路径'
```

### 24 小时试用与模型费用

可以在应用内申请 24 小时试用，**无需先填写 NearMate Key**。服务开放时间、每日额度和设备资格会影响领取结果，请以应用内显示为准。试用的新会话需要联网。

NearMate 试用不包含模型 API 额度。模型 API 需要自行准备，费用由对应厂商单独收取；后台工作也需要配置相应服务与凭据。

### 数据与运行边界

NearMate 在本机运行桌面界面和部分语音能力，模型推理、授权和后台工作可能访问外部服务。对话和选用的屏幕、材料内容会根据功能需要发送给已配置的服务。请依据服务厂商的数据政策选择使用内容。

近期对话与任务上下文只在后台进程运行期间保留，退出后不继续保留这些运行期记忆。角色卡和设置单独保存。

## 官网开发

网站使用原生 HTML、CSS 与少量 JavaScript，不依赖前端框架或第三方运行时包。需要 Node.js 22 或更新版本执行校验与构建，不需要 `npm install`。

```powershell
npm run check
npm run build
python -m http.server 4173 --bind 127.0.0.1 --directory dist
```

在浏览器打开 [localhost:4173](http://localhost:4173/)。请通过 HTTP 预览，直接双击 HTML 会让浏览器阻止读取下载清单。

```text
index.html                      页面内容、语义结构与 FAQ
assets/css/site.css             响应式布局、主题和无障碍状态
assets/js/site.js               手机导航、安装版本选择和下载列表
assets/js/release-manifest.js   浏览器与构建共用的发行清单校验
assets/images/nearmate.png      NearMate 品牌图
data/release.json               当前版本、下载附件、大小与 SHA256
scripts/check.mjs               本地资源、页面锚点与发行清单校验
scripts/build.mjs               校验后复制公开资源到 dist
.github/workflows/pages.yml     GitHub Pages 自动部署
releases/                      首版说明、版本说明模板与发布流程
```

页面采用系统字体，不加载统计脚本、外部字体或第三方前端库。未发布、网络失败和清单错误时都会保留 Releases 入口，不推测安装文件地址。禁用 JavaScript 时仍可访问 Releases。

## GitHub Pages

1. 在仓库 **Settings → Pages → Build and deployment** 中选择 **GitHub Actions**。
2. 将官网更新推送到 `main`；也可以手动运行 **Deploy GitHub Pages** 工作流。
3. 工作流先校验清单和静态资源，只把 `dist` 上传到 Pages。

网站地址为 **https://roverscode.github.io/NearMate/**。所有站内资源采用相对路径，兼容仓库名子路径。大型安装包由 GitHub Releases 托管，不放在 Git 或 Pages 产物中。

## 发布新版本

参见 [发布流程](releases/PUBLISHING.md)。发布顺序是：**核对并上传完整安装文件 → 发布 Release → 验证附件可访问 → 更新下载清单 → 部署官网**。

`data/release.json` 中 `status: "pending"` 表示安装包尚未对外发布。此时网页只显示准备状态与 Releases 入口。只有全部文件已真实发布并验证后，才切换为 `published`。

## 反馈问题

请在 Issue 中说明版本、CUDA / Universal、Windows 版本、硬件概况、复现步骤和错误提示。公开提交前，请从截图或日志中移除 API Key、授权 Key、个人文件路径、聊天正文等私密内容。
