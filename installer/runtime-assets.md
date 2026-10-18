# 公开运行资产

NearMate 构建不依赖私有目录或上游源码。`build_profile.ps1` 使用冻结 Python 调用下面的准备器，缺少缓存时从清单固定的公开地址下载；已有文件和下载文件都核对大小与 SHA256。下载先进入临时文件，校验通过才成为正式缓存；路径经过重解析点与目录越界检查。

| 输入 | 固定来源与版本 | 许可与处理 |
| --- | --- | --- |
| StreamVox | `RoversCode/StreamVox` v2.0.0 公开 wheel；URL、大小与摘要见 [安装构建](README.md) 和 `uv.lock` | Apache-2.0；CUDA 使用 `llama-cuda,onnx-cuda`，Universal 使用 `directml` |
| ASR native | 公开 wheel 的 `streamvox/bin` 与 `native-manifest.json` | llama.cpp b8683（commit `d0a6dfeb28a09831d904fc4d910ddb740da82834`）DLL 与既有 ASR 相同；OpenMP 使用 SDK 固定 LLVM 15.0.5 运行库，保留其 Apache-2.0 WITH LLVM-exception 来源 |
| 唤醒 | k2-fsa/sherpa-onnx `kws-models` 中 `sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20.tar.bz2` | Apache-2.0；只提取原有 chunk-16 模型、tokens 和公开测试关键词基准，不提取归档其它内容；角色关键词仍由运行配置生成 |
| 声纹 | k2-fsa/sherpa-onnx `speaker-recongition-models` 中 `3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx` | Apache-2.0；固定原有模型与摘要 |
| Smart Turn | `pipecat-ai/smart-turn-v3` commit `f766f81d3cfdf7737ac64aad813d91bbfd56bf93` 的 `smart-turn-v3.2-gpu.onnx` | BSD-2-Clause；保留原有 v3.2 模型，执行仍固定 CPU |
| Silero VAD | `snakers4/silero-vad` commit `bfdc0193023f121ea5b3cc7b176dbed570a68a59` 的原 ONNX | MIT；保留原有文件和摘要 |
| Qwen ASR | [NearMate runtime-assets-v0.2.0](https://github.com/RoversCode/NearMate/releases/tag/runtime-assets-v0.2.0) 的三份原 FP16/F16 转换文件 | 原模型 Qwen3-ASR-0.6B，Apache-2.0；使用既有转换权重，不替换成上游较小的 INT4/Q4_K 包。附件随带来源、转换说明和原许可 |
| Text EOU | `FireRedTeam/FireRedChat-turn-detector` commit `1a53b2145cdac39f247a8d55922630635aadfaef` | Apache-2.0；保留原中文/多语言 q8 ONNX 及 tokenizer |
| TTS 模型目录 | 仓库中的 `installer/model-catalog.json` | 保留原五模型的 revision、文件摘要与下载合同；TTS 权重在首次配置下载，分别遵循各自许可 |
| PortableGit | Git for Windows v2.44.0.windows.1 官方自解压归档 | 固定摘要由 `prepare_portable_git.ps1` 核对；随包保留原 Git 许可 |
| MSVC | Microsoft 14.42.34433.0 固定官方下载归档 | `prepare_msvc_download.ps1` 准备源目录；`prepare_msvc_runtime.ps1` 再核对 10 个 DLL 的摘要、文件版本与 Microsoft 签名，原中英文许可不变 |
| FFmpeg / LiveKit | 既有 `prepare_ffmpeg_runtime.ps1` / `prepare_livekit_runtime.ps1` 固定 URL 与摘要 | 保留 GPL v3 / Apache-2.0 原许可与来源说明 |

18 个实时模型文件的精确下载 URL、归档成员、大小与摘要统一登记在 [realtime-assets.json](realtime-assets.json)。首选国内公开镜像为 [ChengHee/NearMate](https://modelscope.cn/models/ChengHee/NearMate)，固定提交为 `4f9433aea2530e76553134d257775be4d01bc7b9`；每份文件的首选 URL 都使用该不可变提交，不跟随 `master`。原固定下载地址与唤醒归档保留为后备。Qwen ASR 的独立依赖附件文件名为：

- `qwen3_asr_encoder_frontend.fp16.onnx`
- `qwen3_asr_encoder_backend.fp16.onnx`
- `qwen3_asr_llm.f16.gguf`

手动准备或预热构建缓存：

```powershell
uv sync --locked --extra dev --extra windows-cuda
.venv/Scripts/python.exe installer/prepare_realtime_assets.py
.venv/Scripts/python.exe installer/prepare_native_runtime.py
```

已有相同公开文件的构建机可在模型准备命令加 `--source-root <旧backend/ckpt目录>`。该目录始终只读；只有大小和摘要完全匹配的文件才会复用，错误版本转用公开下载。源目录不是后续构建或客户运行的依赖。下载缓存默认为当前 checkout 的 `.artifacts/realtime-assets`，模型输出为 `backend/ckpt`；它们不进入 Git。

## 发布 ModelScope 运行资产镜像

`publish_realtime_assets.py` 只接受 `realtime-assets.json` 中逐项登记的模型，不上传完整 `backend/ckpt`。先准备独立发布目录，再通过本机 ModelScope SDK 登录发布到 `ChengHee/NearMate`；Access Token 不写入命令、源码或发布目录。发布目录之外保存下载缓存和准备器的进程锁。

```powershell
.venv/Scripts/python.exe installer/publish_realtime_assets.py `
  --manifest installer/realtime-assets.json `
  --source-root backend/ckpt `
  --staging .artifacts/modelscope-runtime-assets/package `
  --notices-root <已核验许可目录>
```

许可目录的 `sources.json` 必须逐一覆盖全部模型，并列出原许可文件、来源、归属、文件大小和 SHA256；只接受声明过的 `licenses/`、`model-cards/` 与 `SOURCES.md`。缺失归属、许可不符、路径越界或发布目录混入额外文件时停止。唤醒关键词必须是清单中的 284 字节公开测试基准；本机角色生成的关键词不具有发布资格。

确认发布包后，在同一命令追加 `--repo-id ChengHee/NearMate --revision master --publish`。SDK 以 LFS 流式上传模型，随后返回完整的提交摘要；客户端清单只引用该不可变提交的公开文件 API，保留原固定来源与归档成员作为后备。目标分支已有不同内容的同名文件时停止，避免覆盖其他版本。

发布器匿名核对发布包中全部文件的大小与 SHA256，再实际完整下载一个小文件、以 HTTP Range 读取最大模型的前 1024 字节。通过后在发布目录的父目录写入 `publication-result.json`，其中只保存仓库标识、不可变提交与公开下载地址。

SDK 上传成功但返回空 `oid` 时，仅在远程 `publication-manifest.json` 的大小和 SHA256 与本地包完全一致时，才使用该回执的文件版本定位实际提交；随后仍对固定提交中的全部文件重新做匿名核验。`verify_published_package` 可独立恢复这一步，不会再次上传模型。

镜像随带 sherpa-onnx、3D-Speaker、Qwen ASR、FireRedChat 和 BERT 的 Apache-2.0 原文，Smart Turn 的 BSD-2-Clause 原文及 Daily 版权声明，Silero VAD 与 Smart Turn 的 Whisper 骨干所需 MIT 原文。来源说明保留已有 FP16/F16 转换的归属；各资产的许可继续独立适用。

发行 smoke 在独立进程中按正式 ORT 预载顺序实际创建唤醒与声纹模型，再加载当前 profile 的 ASR，从随包默认角色录音完成 encoder 和 GGUF 转写，并核对实际执行后端。RTC smoke 另外真实入房并交换非零音频。模型文件存在或顶层导入成功都不足以通过这些检查。
