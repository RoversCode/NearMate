# 发行与下载清单维护

## 仓库边界

这里维护官网及发行说明。应用在私有工程中构建并完成发行验收，输出的安装程序与分卷只上传 GitHub Releases，不提交到这个仓库。

官网可以先发布：保持 `data/release.json` 为 `pending`，下载区会解释安装文件仍在准备，并提供有效的 Releases 入口。不要提前填入推测的附件地址。

## 发布顺序

1. 在私有工程完成两个 profile 的构建与实际安装验收。检查所有分卷，确认没有客户数据、开发凭据或不该公开的材料。
2. 对最终 `.exe` 和全部 `.bin` 计算 SHA256，生成 `SHA256SUMS.txt`。摘要格式为每行 `小写 SHA256`、两个空格、`文件名`。
3. 从 [模板](TEMPLATE.md) 编写对应的 `v<版本号>.md`。首版说明已在 [v0.1.0.md](v0.1.0.md)。
4. 创建 `v<版本号>` 的 draft Release，上传两个 profile 的全部文件与校验文件。逐一核对文件名、大小和 SHA256。
5. 发布 Release，验证匿名用户能访问每个附件。实际下载抽查并确认 SHA256 一致。
6. 更新 `data/release.json`，运行 `npm run check` 和 `npm run build`。
7. 推送 `main`，等待 GitHub Pages 工作流成功；在官网分别检查两个版本的下载列表与校验链接。

一次点击只下载一个文件。官网不自动批量触发下载，以免浏览器拦截或只保存部分分卷。

## 清单格式

待发布状态必须保留空附件列表：

```json
{
  "schemaVersion": 1,
  "version": "0.1.0",
  "status": "pending",
  "publishedAt": null,
  "releaseUrl": "https://github.com/RoversCode/NearMate/releases",
  "checksumsUrl": null,
  "profiles": {
    "universal": { "assets": [] },
    "cuda": { "assets": [] }
  }
}
```

已发布后修改这些字段：

| 字段 | 要求 |
| --- | --- |
| `version` | 与 Release tag 一致的版本号，不带 `v` |
| `status` | `published` |
| `publishedAt` | 实际发布日期，`YYYY-MM-DD` |
| `releaseUrl` | `https://github.com/RoversCode/NearMate/releases/tag/v<版本号>` |
| `checksumsUrl` | 同一 Release 下 `SHA256SUMS.txt` 或 `SHA256SUMS` 的完整附件地址 |
| `profiles.<profile>.assets` | 对应版本的完整附件数组，建议安装程序在前、分卷按序排列 |

同一程序版本的安装修复包可使用 `v0.1.0-rebuild.1` 这类独立 tag。清单中的 `version` 必须保留完整版本号，网页会将 `-rebuild.<序号>` 显示为“安装修复版”。原 Release 与附件应保留，用户下载时不能混用原版与修复版分卷。

每个安装附件包含四个字段：

| 字段 | 要求 |
| --- | --- |
| `name` | 实际附件名，包含 profile 名称，后缀为 `.exe` 或 `.bin` |
| `url` | `https://github.com/RoversCode/NearMate/releases/download/v<版本号>/<实际附件名>` |
| `size` | 实际字节数，整数；页面会转换为 MiB / GiB |
| `sha256` | 最终文件的 64 位小写 SHA256，与校验文件一致 |

每个 profile 必须有且只有一个 `.exe`，并包含至少一个 `.bin`。分卷名遵循 Inno Setup 的规则：若安装程序为 `<基础名>.exe`，分卷必须连续命名为 `<基础名>-1.bin`、`<基础名>-2.bin` 等。清单校验会拒绝重复文件、分卷漏号、混用 profile、其他仓库链接、错误版本地址或缺失校验值。

清单本身无法证明最后一卷是否被遗漏。生成清单时，必须把本次构建的完整输出、GitHub Release 附件列表和清单逐项对账，确认文件数量、名称、字节数与 SHA256 全部一致。

文件大小和摘要必须从最终文件读取：

```powershell
Get-ChildItem -LiteralPath '最终安装包所在目录' -File |
  Where-Object { $_.Extension -in '.exe', '.bin' } |
  ForEach-Object {
    [pscustomobject]@{
      name = $_.Name
      size = $_.Length
      sha256 = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
    }
  }
```

## 网站验收

- 桌面与手机宽度下无横向溢出；正文、按钮和文件名可读。
- 手机导航可打开、点击跳转与按 Escape 关闭。
- 键盘可到达下载选择、所有附件、FAQ 与外部链接。
- 切换 Universal / CUDA 时，`aria-pressed`、文件列表和总大小一致。
- 每个下载链接、校验文件与 Release 版本相符，可匿名访问。
- `pending`、清单无法读取、禁用 JavaScript 时都有可用的 Releases 入口。
- 操作系统启用减少动画时，页面关闭平滑滚动与动效。

`npm run check` 负责本地结构与清单静态检查；附件存在性、实际内容、安装效果和浏览器视觉效果仍需在发布时核对。

## 修改文案或布局

保持产品边界准确：不要承诺永久记忆、全部本地处理、免费模型额度或保证随时能领取试用。新增截图必须来自可公开的真实产品界面，品牌形象或场景示意需要如实标注。
