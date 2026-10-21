<div align="center">
  <img src="./website/assets/images/nearmate.png" width="180" alt="NearMate 品牌形象">
  <h1>NearMate</h1>
  <p><strong>实时语音交互的智能桌面助手（Voice Agent）</strong></p>
  <p>用语音交代任务，让助手理解屏幕、操作电脑与网页、处理文件；执行中继续交流，也能用语音纠正或取消任务。</p>
  <p>
    <a href="https://roverscode.github.io/NearMate/">官方网站</a> ·
    <a href="https://roverscode.github.io/NearMate/guide/">使用手册</a> ·
    <a href="https://github.com/RoversCode/NearMate/releases/latest">下载 Windows 版</a> ·
    <a href="https://roverscode.github.io/NearMate/#community">加入体验交流</a> ·
    <a href="https://github.com/RoversCode/NearMate/issues">问题反馈</a>
  </p>
  <p>Windows x64 · CUDA / Universal 双版本 · Apache-2.0 · 免费开源</p>
</div>

## 能力总览

NearMate 把实时语音、屏幕理解和 Agent 执行结合在同一个 Windows 桌面助手里。**语音贯穿任务的发起、执行中的沟通和停止。** 你说出目标，助手结合当前画面与提交的材料，调用工具和技能完成多步骤工作；任务在后台执行时，你仍可以继续和它交谈。

| 核心能力 | 实际使用体验 |
| --- | --- |
| **实时语音交互** | 用唤醒词开始交流，支持插话打断当前回复；开启跟进模式后，可以连续补充，不必每句话都重新唤醒。也支持文字输入。 |
| **用语音指挥任务** | 直接交代目标，执行中问「做到哪一步了？」、回答追问、明确允许或拒绝权限请求。听错或理解错时可以纠正目标，也可以说「取消刚才那个任务」。 |
| **理解屏幕与材料** | 围绕当前屏幕提问，例如「这个报错是什么意思？」；提交截图、文档、音视频或网页片段，让助手结合材料理解问题、执行任务。 |
| **电脑操作 · Computer Use** | 观察 Windows 应用和窗口，点击、输入、滚动、使用快捷键，完成应用内操作并检查结果，例如「打开计算器，算一下 6 × 7」。 |
| **网页操作 · Browser Use** | 访问和读取网页、搜索、点击链接、填写表单、上传或下载文件，按目标完成网页上的多步骤操作。 |
| **文件、脚本与技能** | 查找、创建和修改文件，编写代码、运行脚本与命令；结合已启用的技能处理数据、转换音视频、转写录音、总结视频或制作文档。 |
| **进展反馈与成果交付** | 在聊天窗口查看任务进展，需要信息时助手会提问，完成后播报结果概况。完整成果和新文件通过结果卡交付；要求修改已有文件时，则修改所提供的文件并说明结果。 |

这些能力可以在一次任务中组合使用，例如查找网页资料、用脚本处理数据，再生成交付文件。后台 Agent 是当前角色执行任务的能力，交代目标、追问进展和接收结果都在同一段交流中完成。

### 用语音完成一项任务

1. **交代目标。** 唤醒后说「查一下 Python 在 Windows 上的安装步骤，整理成一份文档」。助手查找资料、整理内容并生成文件。
2. **执行中交流。** 可以继续问「现在做到哪一步了？」；如果助手需要补充信息或授权，也可以直接用语音回答。
3. **按需纠正或取消。** 如果刚才说错了，可以说「不是 Python，是 Node.js，改查 Node.js 在 Windows 上的安装步骤并整理成文档」；不再需要时，说「取消刚才整理安装文档的任务」。
4. **听结果、取文件。** 任务正常完成后，助手会告诉你结果，并指引你在聊天窗口的结果卡中查看文档。

插话打断的是当前回复；停止正在执行的工作，需要明确要求取消该任务。取消用于停止后续执行，已经发生的文件修改和外部操作不会自动撤销。

### 角色与日常使用

日常可以聊天、联网查资料、分析问题，也可以说「打开记事本」启动已安装的应用，或说「十分钟后提醒我休息」设置弹窗与语音提醒。提醒在本次运行期间有效，可查看和取消。

内置默认角色「火花」。你可以通过角色卡调整身份、语气、关系和唤醒词，为角色准备音色，并单独选择 Live2D 虚拟形象。上方人物是 NearMate 品牌形象，实际桌面虚拟形象可以在应用内选择。

材料可拖入聊天输入区，也可用快捷键收集选中的文字、图片和网页片段，再随问题一起提交。

### 启用这些能力

- **实时交流与日常助手：** 完成首次向导，连接模型 API、检查设备并准备好本地语音。软件与内置语音免费，模型 API 由用户自行准备；实时天气查询需另配和风天气。详见[首次配置](#首次配置与开始使用)。
- **Agent 任务执行：** 在控制台配置独立的后台工作服务、主模型与凭据。Computer Use 和 Browser Use 还需在技能库开启「电脑与浏览器操作」，并按所选权限模式处理授权。网页自动化使用 NearMate 专用浏览器环境，登录网站需在该环境中完成登录。详见[后台工作配置](https://roverscode.github.io/NearMate/guide/optional-services.html#background-work)。

## 它在桌面上是什么样

<div align="center">
  <a href="./website/assets/images/desktop-demo.jpg"><img src="./website/assets/images/desktop-demo.jpg" width="1000" alt="NearMate 实机画面：聊天窗口展示音频转换结果与交付文件，右侧是用户配置的桌面虚拟形象"></a>
</div>

实机演示：把音频转成 MP3，在聊天窗口查看结果卡与交付文件。右侧为用户配置的虚拟形象。点击图片可以查看大图。

## 视频演示（2 分 37 秒）

影片展示默认角色「火花」的语音与角色卡设置，了解身份、语气、关系和声音如何影响实际交流。

<!-- 更新影片时请重新上传 GitHub 视频附件，并让附件地址单独成段；Releases 下载地址不会生成播放器。 -->

https://github.com/user-attachments/assets/57d22044-e02c-4726-94b5-f83983805162

**播放后请开启声音。** [在官网观看完整宣传片](https://roverscode.github.io/NearMate/#film) · [下载 1080p MP4](https://github.com/RoversCode/NearMate/releases/download/v0.2.0/NearMate-Product-Film-1080p.mp4)

## 下载与安装

前往 [官网下载页](https://roverscode.github.io/NearMate/#download) 或 [GitHub Releases](https://github.com/RoversCode/NearMate/releases/latest)，选择适合这台电脑的版本：

| 版本 | 适合的电脑 |
| --- | --- |
| **CUDA · NVIDIA 版** | 配备兼容 NVIDIA 显卡的 Windows x64 电脑 |
| **Universal · 通用版** | AMD / Intel 等设备；提供 DirectML、Vulkan 与 CPU 运行方式，通过首次向导确认支持情况 |

两种版本提供相同的产品功能，实际运行速度取决于硬件与所选模型。

1. 下载所选版本的安装程序 **`.exe` 和全部 `.bin` 分卷**。
2. 把所有文件放在同一文件夹，保留原文件名，再双击 `.exe` 安装。
3. 打开 NearMate，跟随首次向导完成配置。

**不要混用 CUDA / Universal，也不要混用不同发行的分卷。** Releases 附带 `SHA256SUMS.txt`，可用于核对下载文件是否完整。当前安装程序尚未使用 Windows 代码签名。

## 首次配置与开始使用

从安装到第一次聊天，请按 [使用手册](https://roverscode.github.io/NearMate/guide/) 操作。首次向导分为五步：连接 DeepSeek、连接 Qwen、检查电脑、准备语音、试听并开始。

1. **连接模型 API。** 准备支持的模型服务 API Key，并按向导配置。软件免费使用，模型服务需自行准备。
2. **准备语音与角色。** 按向导检查设备、下载所需语音模型、完成试听，再选择角色卡与虚拟形象。
3. **开始使用。** 把鼠标移到桌面虚拟形象上，点击聊天气泡图标输入文字；使用默认角色时，也可以说「你好火花」或「火花同学」唤醒语音。先试试「这个报错是什么意思？」或「十分钟后提醒我休息」。

需要文档处理、音视频转换或电脑操作时，进入控制台的 **设置 → AI 与服务 → 后台工作**，配置 Claude Code 兼容服务的地址、主模型和凭据，再选择合适的权限。配置方法见[后台工作使用说明](https://roverscode.github.io/NearMate/guide/optional-services.html#background-work)。

### 免费与服务费用

NearMate 自有代码采用 Apache-2.0，软件及内置语音免费使用。模型 API 需要自行准备，费用由对应厂商单独收取；后台工作所接入的服务也可能产生费用。第三方模型、运行库和素材适用各自许可。

### 数据与记忆

桌面界面和部分语音能力在本机运行；模型推理和后台工作可能访问外部服务。对话及选用的屏幕、材料内容会按功能需要发送给已配置的服务，请结合厂商的数据政策选择使用内容。

近期对话和任务上下文在后台进程运行期间保留，退出后台进程后不继续保留。角色卡与设置单独保存。

## 从源码开发

支持 Windows x64，使用 Python 3.12、uv、Node.js 和 pnpm 9。所有命令在仓库根目录执行：

```powershell
pnpm install --frozen-lockfile
pnpm python:venv
pnpm python:install
pnpm dev
```

非 NVIDIA 设备使用 `pnpm python:install:universal` 安装 Python 依赖。首次启动按控制台提示连接模型服务、准备角色语音。依赖来自公开固定发行物，无需申请产品激活码。

文本分词器 `backend/ckpt/tokenizer.json` 随源码提供，安装 Python 依赖后可直接运行 `pnpm backend:test`。分词器的来源与许可见 [第三方声明](THIRD_PARTY_NOTICES.md)。正式测试位于各产品包中，个人试验脚本和本机数据不进入 Git。

源码启动会检查实时输入模型，缺失或损坏时自动从 [ModelScope NearMate](https://www.modelscope.cn/models/ChengHee/NearMate) 的固定版本下载；完整资源约 2.3 GB。控制台会显示准备进度，失败后可重试，尚未配置 LLM 时也能准备资源。已有模型通过大小和 SHA256 校验后直接复用，无需联网检查更新。模型来源、版本与摘要见 [实时资源清单](installer/realtime-assets.json)；原生 DLL 从已安装的固定 StreamVox wheel 准备。角色语音模型继续通过控制台的首次配置流程管理。

安装包构建见 [安装包构建](installer/README.md)。官网源码位于 `website/`，在该目录运行 `npm test`、`npm run check` 和 `npm run build`。

## 一起体验 NearMate

分享角色搭配、交流使用方法，或反馈遇到的问题。

**QQ 体验交流群：1029360240**。用手机 QQ 扫描下方二维码，也可以搜索群号加入。

<div align="center">
  <a href="./website/assets/images/community-qq.jpg"><img src="./website/assets/images/community-qq.jpg" width="300" alt="QQ 体验交流群二维码，群号 1029360240；图中群名为 StreamPet 体验交流群"></a>
</div>

二维码与群内名称仍显示历史名称「StreamPet 体验交流群」，即本产品的体验群。

## 反馈问题

请通过 [GitHub Issues](https://github.com/RoversCode/NearMate/issues) 提交问题，说明 NearMate 版本、CUDA / Universal、Windows 版本、硬件概况、复现步骤和错误提示；也欢迎在体验群里交流。

公开提交截图或日志前，请移除 API Key、个人文件路径及私密聊天内容。

## 开源许可

NearMate 自有代码采用 [Apache-2.0](LICENSE)。第三方依赖、Live2D 运行库与模型、语音模型和参考素材分别适用原有许可，详见 [第三方声明](THIRD_PARTY_NOTICES.md)。

内置 Q 版大肥鱼虚拟形象是社区同人模型，按 **CC BY-NC-SA 4.0（署名、非商业、相同方式共享）**独立分发，不属于 Apache-2.0 授权，也不是 DeepSeek 官方形象。模型及其改编须保留署名并遵守上述条件；商业使用需另获授权。来源与完整署名见[模型说明](desktop/src/renderer/public/live2d/resources/DeepSeekChibi/NOTICE.txt)。

## 支持项目

如果 NearMate 对你有用，欢迎给项目点一个 **Star ⭐**，也欢迎分享给需要桌面 AI 搭子的朋友。

想请作者喝杯咖啡，可以使用微信扫描下方二维码。**Star 和赞赏完全自愿，不影响任何功能。**

<div align="center">
  <img src="./website/assets/images/wechat-support.png" width="260" alt="微信自愿赞赏二维码：请作者喝杯咖啡">
</div>
