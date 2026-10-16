# NearMate Windows 安装构建

构建机需要 uv、pnpm 9.12.2 与 Inno Setup 6。Python 3.12.13、PortableGit、CPU 输入模型、ASR 模型和原生库都由固定公开来源准备。StreamVox 2.0.0 使用公开 GitHub Release wheel，依赖锁文件固定下载地址与摘要，首次构建会自动下载并校验。先用 `prepare_python_runtime.ps1` 冻结随包 Python，再用 `build_profile.ps1` 复制已经完成验证的 staging 资源；两个脚本都不会下载 TTS 权重，也不会复制用户数据。NearMate 与内置 StreamVox 免费使用，构建不需要激活材料或授权服务。业务 Python 源码保留在随包的 `site-packages` 中。

两版冻结 Python 都通过 `prepare_msvc_runtime.ps1` 携带 Microsoft Visual C++ 2015-2022 x64 Runtime 14.42.34433.0 的完整 10 个 DLL，放在解释器同级的 `python/Scripts`。固定清单 `installer/msvc-runtime.json` 校验每个 DLL 的 SHA256、文件版本及 Microsoft 签名；构建可复用 Visual Studio 2022 的固定 `VC/Redist/MSVC/14.42.34433/x64/Microsoft.VC143.CRT` 目录；缺失时由 `prepare_msvc_download.ps1` 从固定 Microsoft 地址下载同版本归档并核对摘要。自定义构建机位置可设置 `NEARMATE_MSVC_RUNTIME_DIRECTORY`，单独准备 Python 时也可传 `-MsvcRuntimeDirectory`；不能从 `System32` 收集运行库。来源说明和未改动的官方中英文 RTF 许可保存在 `python/licenses/msvc-runtime`，安装器不写 Windows 系统目录、不要求客户预装全局 VC Redistributable。

`build_profile.ps1` 同时调用 `prepare_livekit_runtime.ps1`，把官方 LiveKit Server 1.12.0 Windows x64 程序与 `LICENSE` 放入 `runtime/tools/livekit`。构建缓存位于 `.artifacts/runtime-tools/livekit-1.12.0`，缺少 ZIP 时才从固定 GitHub Release 地址下载；缓存、下载结果和提取后的 EXE 都经过固定 SHA256 校验。ZIP 摘要为 `b2cb4f5648ffb61f8b73e9c529bec3e6ed3456915b7774a341a3250e620cfb77`，EXE 摘要为 `35894ab530ac4d8113a1b791e01d2ad6fc2c3f740be46741f9e6d468035e8705`。应用运行时不下载此组件。

安装版默认由 Desktop 启动和关闭随包 LiveKit，每次启动生成随机凭据和端口；完整的外部 `LIVEKIT_URL`、`LIVEKIT_API_KEY`、`LIVEKIT_API_SECRET` 配置仍可覆盖本机模式。信令与管理接口只监听 `127.0.0.1`，RTC 音频通过本机网卡候选建立加密连接，不需要云端媒体服务器。不能把媒体也强制为 `127.0.0.1`：随包原生 SDK 不生成环回网卡候选，这会出现健康检查通过、真实入房一直失败。

生成 runtime 清单前，脚本只清理 staging 中的 `.gitkeep` 占位文件，避免 Electron 过滤它们后出现清单与封装资源不一致；源码占位文件保留。

```powershell
pwsh installer/prepare_python_runtime.ps1 -Profile cuda
pwsh installer/prepare_python_runtime.ps1 -Profile universal
pwsh installer/build_profile.ps1 -Profile cuda
pwsh installer/build_profile.ps1 -Profile universal
pnpm installer:package:cuda
pnpm installer:package:universal
pwsh installer/compile_setup.ps1 -Profile cuda
pwsh installer/compile_setup.ps1 -Profile universal
# 需要本机已安装 Inno Setup 6；使用极小 fixture 验证首次安装、升级清理和拒绝条件
pnpm installer:test:release-cleanup
```

`electron-builder` 的 unpacked 目录是 Inno Setup 的唯一输入，目录中已经包含 Desktop、Console、Backend、冻结 Python 和对应 profile 的运行库。安装程序只复制该目录；TTS 权重在首次启动向导中由 StreamVox SDK 下载到用户数据目录。

固定版本的 `electron-builder` 在解包 Electron 时重建 `win-unpacked` 输出目录；配置使用实际工具的 schema 做回归校验，不添加它不支持的 `removeOutDir` 字段。`compile_setup.ps1` 还会拒绝根级重解析点，
然后在本次 unpacked 根目录生成 `release-root-files.txt`。这是升级清理的唯一程序文件所有权
合同，Inno Setup 会先校验清单，再按旧包清单删除根级程序文件和目录；它不再依赖手写 Electron
DLL 名单。`userData` 位于安装目录之外，不会进入该清单，也不会因升级被删除。

只有同时包含 NearMate 主程序、schema v3 `resources\runtime\runtime-manifest.json` 和
`release-root-files.txt` 的目录才会被识别为可升级安装。schema v2 或早期 v3 目录会被视为
未知非空目录：请先从系统卸载旧版本（必要时备份用户数据），或选择一个空的安装目录，不能
要求安装器猜测旧文件的所有权。

发行运行时准备阶段通过 `runtime_pruning.ps1` 移除 pytest、构建工具、第三方测试套件、C/C++ 头文件和静态导入库；保留 SciPy 正常导入所需的 `numpy.testing` 等 `testing` 公共模块、librosa 懒加载所需的 `.pyi` 声明，以及 soundcard 的 CFFI 头文件。Python 文件不按 `*_test.py` 名字裁剪，避免误删 `scipy.stats._page_trend_test` 这类统计检验算法。CUDA/NVIDIA `bin` 下的运行 DLL 会完整保留。CUDA 版本体积主要来自随包提供的 CUDA/cuDNN DLL，不应把它们误判为开发文件。准备阶段会对 StreamVox、ONNX Runtime、sherpa-onnx 等关键模块执行导入烟测，并拒绝 Torch 或多种 ONNX Runtime 同时存在。

CUDA 与 Universal 必须分别在干净环境中构建。Universal staging 发现 CUDA、cuDNN、cuBLAS、NVRTC 或 GPU ONNX Runtime 文件时会直接失败。

两版的唤醒和声纹固定使用同源 CPU `sherpa-onnx==1.12.20` 和 `sherpa-onnx-core==1.12.20`，通过 Windows wheel 直链及 SHA256 锁定，与 StreamVox 固定的 ONNX Runtime 1.21.1 共用应用预载的 DLL。不能只按 Python import 成功就升级 sherpa：1.13.2 的原生扩展要求 ORT API 24，在 ORT 1.21.1 下首次创建模型会直接崩溃。安装态启动会先拒绝未经验证的依赖组合；ASR、TTS 的 CUDA/DirectML profile 边界不变。

`compile_setup.ps1` 每次先清空并重建当前 profile 的 `Output` 子目录，再优先复用指向本仓库的短盘符；首选盘符被占用时自动选择空闲盘符，再调用 Inno Setup。这样可以避免历史 `.bin` 分卷混入交付目录，也能把随包 Python 依赖的深层路径压回 Windows 传统路径长度限制。编译结束或失败后只释放本次创建的映射，保留已有映射；编译成功还会验证本次 setup exe 和连续编号的全部 `.bin` 分卷。

Python 准备脚本会复制固定版本的基础解释器、标准库和动态扩展库，用完整解释器替换虚拟环境启动器并删除 `pyvenv.cfg`。准备完成及复制到 staging 后均执行 `verify_python_runtime.py`，核实解释器、标准库、关键依赖和派生 Python 的来源都在各自运行时目录内。校验还会导入四个 StreamVox 模型家族的实际运行模块，并用临时参考音频执行读取、重采样和 Mel 频谱计算，提前发现顶层导入无法覆盖的懒加载依赖缺失；不下载权重，也不宣称真实模型试听已经验收。

同一校验会检查随包 C++ 运行库闭包和许可摘要，并在真实 ORT/语音模块导入后查询 `MSVCP140`、`MSVCP140_1`、`VCRUNTIME140`、`VCRUNTIME140_1` 的实际已加载路径；任何 DLL 来自 `System32` 或其它目录都阻断构建，不能让构建机已有的运行库掩盖漏包。完整 10 个 DLL、清单和许可同时登记在 schema v3 的 `required_files`，应用启动仍只检查关键文件。

staging 审计后使用独立用户目录、临时配置和空闲端口启动完整 Backend：先校验 schema v3 清单与 required files 摘要，再以 `python -I` 检查 Backend/AEC 导入、当前 profile 的 native DLL 和默认实时语音资产。`verify_native_audio_runtime.py` 按正式后台的预载顺序实际创建唤醒/声纹运行时，执行音频解码和声纹嵌入，再用默认角色的真实录音运行当前 profile 的 FP16/F16 ASR，核对非空转写与实际 ONNX/llama 后端，并确认 Windows 应用目录预热能够交还启动线程；发生原生进程崩溃或超时会停止打包，阶段与退出诊断写入本轮 `native-audio.log`。随后验证 `/healthz`、隔离 Bootstrap、Runtime Profile、五模型 TTS 目录与 Console。smoke 不继承构建机 LiveKit 凭据，安装包的 `release-env.yaml` 也必须保持凭据为空。不通过时停止打包，日志保存在 `.artifacts/build-smoke`。

`verify_livekit_runtime.py` 还会实际启动随包媒体程序，使用当次随机凭据完成认证健康检查，并让两个 RTC 参与者入房、发布与接收非零合成音频。它不打开麦克风，不读取用户数据或 TTS 权重；单纯 HTTP 200 无法通过此检查。RTC 原生库在独立子进程运行，崩溃或超时也会收口本次媒体服务。诊断分别写入 `livekit-smoke.log` 和 `livekit-server.log`。这些检查覆盖本机媒体传输，但不代表 TTS 下载、模型回答和完整对话已验收。

## 一键构建

NearMate 只消费已经发布的固定 StreamVox wheel，不会进入 StreamVox 仓库或触发 StreamVox 编译。默认同时构建 CUDA 和 Universal：

```powershell
pnpm installer:build
```

只构建一个版本：

```powershell
pnpm installer:build:cuda
pnpm installer:build:universal
```

最终输出为 `installer\Output\cuda` 和 `installer\Output\universal`。每个目录包含 `NearMate-<profile>-setup.exe`、对应的 `.bin` 分卷和 `SHA256SUMS.txt`。复制或交付时携带整个版本目录，运行其中的 `.exe` 安装；不能只复制安装程序。压缩使用 LZMA2 max，单分卷上限 2,000,000,000 字节（约 1.86 GiB），避免大运行时超过单文件安装程序限制。

修复版需要保留上一轮输出时，直接给 `build_all.ps1` 传 `-OutputRoot <独立目录>`；`compile_setup.ps1` 也接受同一参数，默认输出路径保持兼容。每次只清理所选输出根下的当前 profile 子目录。

两版均随包提供 FFmpeg 8.1.2 essentials 与 ffprobe，固定归档 SHA256 为 `db580001caa24ac104c8cb856cd113a87b0a443f7bdf47d8c12b1d740584a2ec`。程序位于 `resources/runtime/tools/ffmpeg/bin`，GPL v3 许可及对应源码地址保留在其上层目录。Desktop 通过 Backend 私有 PATH 提供该工具，不修改系统 PATH。staging smoke 实际检查三个媒体 Skill 的依赖，并生成音频、用 ffprobe 核对参数。

安装包独立包含 `resources/icon.ico`，桌面、开始菜单和卸载入口都引用它。编译前核对独立 ICO 与本次品牌输入一致，随后核对 Desktop EXE 与安装程序的实际图标图层，不能仅凭配置声明品牌图标正确。

五个 TTS 模型的固定清单随仓库保存在 `installer/model-catalog.json`；它只描述首次语音配置要下载的权重，不把 TTS 权重塞进安装包。`build_profile.ps1` 自动按 `realtime-assets.json` 准备实时输入模型，再从已验证的公开 StreamVox wheel 复制同版本 ASR DLL；PortableGit 缺失时调用 `prepare_portable_git.ps1`。来源、许可与手动准备命令见 [公开运行资产](runtime-assets.md)。一键构建仍可用 `-ModelCatalog`、`-GitRuntime` 或对应 `NEARMATE_MODEL_CATALOG`、`NEARMATE_GIT_RUNTIME` 显式指定已准备的公开固定输入；PortableGit 必须包含 `bin\bash.exe` 与 `cmd\git.exe`。`prepare_python_runtime.ps1` 只在自身执行期间设置 `PYTHONPATH`、`UV_PROJECT_ENVIRONMENT` 和 `NEARMATE_RUNTIME_PROFILE`，结束或失败都会恢复调用方原值。

本版 Inno Setup 的目标文件仍受 Windows 传统路径长度限制，请使用默认安装目录或较短目录。深层临时目录的实测安装出现 `MoveFile failed; code 3`，记录在构建报告中；较短目录的验证不能代表超长路径受支持。

脚本默认复用 `.artifacts\wheels\streamvox-2.0.0-cp312-none-win_amd64.whl`；缺失时从[公开 Release](https://github.com/RoversCode/StreamVox/releases/tag/v2.0.0) 下载，不需要 StreamVox 源码或内部 wheel。固定下载输入如下：

- URL：`https://github.com/RoversCode/StreamVox/releases/download/v2.0.0/streamvox-2.0.0-cp312-none-win_amd64.whl`
- 文件大小：`242814050` 字节
- SHA256：`e6d52851088de43b08e6f9a000dbedbc15259089031022e8dfbcd789636b4ba3`

脚本先把下载写入临时文件，验证后才放入固定缓存；大小或摘要不符会停止构建。缓存和输入路径经过重解析点检查。`uv sync --locked` 从同一公开 URL 安装依赖，并再次使用锁文件摘要验证。CUDA 选择 `streamvox[llama-cuda,onnx-cuda]`，Universal 选择 `streamvox[directml]`。

如果已下载 wheel，可直接指定路径；校验通过后复制到当前 checkout 的独立缓存：

```powershell
pwsh -NoProfile -File installer/build_all.ps1 `
  -Profile all `
  -StreamVoxWheel 'D:\releases\streamvox-2.0.0-cp312-none-win_amd64.whl'
```

TTS 权重不进入安装包，首次启动时下载到 `%APPDATA%\NearMate\streamvox\ckpts`。

## 单文件下载归档

`package_download_archives.py` 为 CUDA 和 Universal 各生成一个完整 ZIP64，供用户下载后在本地“全部解压”再安装。脚本只接纳 `status=passed`、没有失败项的 `<profile>-installer-verification.json`，先核对原始 EXE、连续编号的全部 BIN、大小、SHA256 和原始 `SHA256SUMS.txt`。归档保留原始安装文件、`README.txt`、`dependencies.json` 和校验清单，并加入中文 `INSTALL.txt`、归档内容清单与摘要；不会改写安装文件。含有构建路径的安装产物验收报告保留在 ZIP 旁，供发行审计使用。

Inno 分卷已经采用 LZMA2/max 和 solid 压缩，外层 ZIP 使用标准 `Stored` 方法直接归档，避免重复压缩，支持 Windows 资源管理器常规 ZIP 解压所需的基础格式。生成后脚本实际完整解压、检查 CRC，再重读解压文件核对大小和 SHA256；报告明确区分基础格式检查与真实资源管理器操作、真实安装验收。

```powershell
./.venv/Scripts/python.exe installer/package_download_archives.py `
  --installer-root .artifacts/install-fixes-20261001/installers `
  --verification-root .artifacts/install-fixes-20261001 `
  --output-root .artifacts/distribution-promo-20261001/downloads `
  --release-label v0.2.0
```

输出包括两个 `NearMate-<profile>-<release-label>-windows-x64.zip`、`downloads-manifest.json`、总 `SHA256SUMS.txt`、两份 `<profile>-archive-verification.json` 和原始安装产物验收报告。解压证据默认保留在输出目录同级的 `archive-verification/<profile>`，可用 `--extraction-root` 指定独立位置。已有下载文件或核验目录会被拒绝覆盖；重复执行时选择新的输出与核验目录。

[GitHub 官方 Release 容量说明](https://docs.github.com/en/repositories/releasing-projects-on-github/about-releases#storage-and-bandwidth-quotas)规定单个附件必须小于 **2 GiB（2,147,483,648 字节）**，一个 Release 最多 1000 个附件，总体积与带宽没有限额。2026-10-01 修复产物的 EXE 和 BIN 总量已分别为 CUDA 4,168,471,006 字节、Universal 3,019,190,459 字节，因此这两份完整 Stored ZIP 都超过 GitHub 单附件上限。单个 ZIP 下载链接需要能承载对应完整文件的托管位置；脚本在下载清单中按实际归档大小记录是否满足 GitHub 限制，不自动上传。
