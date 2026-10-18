# 第三方组件与素材声明

NearMate 自有代码采用 Apache-2.0。根许可证不替代下列组件、模型或素材的原始权利声明。

| 组件或素材 | 来源与许可边界 |
| --- | --- |
| StreamVox 2.0.0 | [StreamVox](https://github.com/RoversCode/StreamVox/tree/v2.0.0)，自有代码为 Apache-2.0；llama.cpp 原生库保留 MIT，LLVM OpenMP 15.0.5 保留 Apache-2.0 WITH LLVM-exception；许可全文随 SDK wheel 一并分发。原生库与模型分别适用其 [第三方声明](https://github.com/RoversCode/StreamVox/blob/v2.0.0/THIRD_PARTY_NOTICES.md)。 |
| Qwen3-TTS 文本分词器 | `backend/ckpt/tokenizer.json` 原样来自 [ChengHee/qwen3-tts-clone-0.6b-gguf](https://www.modelscope.cn/models/ChengHee/qwen3-tts-clone-0.6b-gguf)，固定 revision `8ebe93d73f320eda154a893bc37b2ee8c78fde78`，上游为 [Qwen3-TTS](https://github.com/QwenLM/Qwen3-TTS)。适用 Apache-2.0，保留原 [LICENSE](installer/licenses/qwen3-tts/LICENSE.txt) 与 [NOTICE](installer/licenses/qwen3-tts/NOTICE.txt) 中的 Alibaba Cloud（Qwen Team）及转换者声明。本项目分发该文件用于文字 token 统计；对应语音模型权重仍单独下载。 |
| 实时输入模型镜像 | [NearMate ModelScope](https://www.modelscope.cn/models/ChengHee/NearMate) 仅镜像固定版本资源；各文件的原来源、许可与摘要见 [运行资产说明](installer/runtime-assets.md) 和 [实时资源清单](installer/realtime-assets.json)。模型保留各自 Apache-2.0、BSD-2-Clause 或 MIT 许可与版权声明。 |
| Live2D Cubism Core 与官方样例模型 | [Live2D Cubism SDK](https://www.live2d.com/sdk/about/)，适用 Live2D 的 SDK 与样例素材条款；不标注为 NearMate 的 Apache-2.0 资产。 |
| Electron、React、PixiJS 与其他前端依赖 | 适用各自 npm 发行包的 LICENSE/NOTICE；准确版本见 pnpm-lock.yaml。 |
| Python、ONNX Runtime、sherpa-onnx 与音频运行依赖 | 适用各自发行包携带的许可；准确版本与来源见 uv.lock。 |
| CUDA/cuDNN 与 Microsoft C++ Runtime | 分别适用 NVIDIA 和 Microsoft 的再分发条款；安装包保留原文。 |
| LiveKit Server、FFmpeg 与 ffprobe | 固定发行及摘要由构建脚本声明；保留随包许可、来源与相应源码获取信息。 |
| Noto Sans SC 字体 | [Google Fonts / Noto CJK](https://github.com/notofonts/noto-cjk)，适用 SIL Open Font License 1.1。 |
| 角色参考素材与用户导入内容 | 不包含在 NearMate 自有代码许可授权中；角色形象、录音和声音权利分别归原权利人。公开素材保留其来源和独立许可，使用者须遵守相应范围。 |

语音模型权重在首次配置时单独下载，不纳入应用源码的 Apache-2.0 授权。S2-Pro 等模型有独立使用条件，详见 StreamVox 的 [模型许可表](https://github.com/RoversCode/StreamVox/blob/v2.0.0/docs/model-licenses.md)。
