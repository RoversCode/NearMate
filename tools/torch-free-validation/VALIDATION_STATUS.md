# 无 Torch 迁移验收状态（2026-09-09）

**状态：代码与独立环境已实现，完整数值门槛尚未通过，不能作为正式发行验收完成。**

## 已验证范围

- Windows x64、Python 3.12.13、RTX 4090、驱动 591.86；模型目录及原始环境保留。
- StreamVox 1.2.5 开发 wheel 含 `streamvox_runtime`，不含参考工具与 Torch 系列依赖。
- NearMate `uv sync --locked --extra dev --extra windows-cuda` 在新独立环境成功；只安装 CUDA ORT。
- 两环境 16 个同名 NVIDIA DLL 的 SHA-256 一致，见 `.scratch/torch-free/dll-hashes-corrected.json`。
- S2 最终 wheel 在 PATH 仅含 Windows 系统目录及候选 Python 目录时成功合成；主进程与解码子进程
  元数据均启用确定性计算，记录的 NVIDIA 用户态 DLL 全部来自候选环境的 NVIDIA wheels。
- StreamVox 43 项测试、NearMate 9 项相关测试、验证工具 7 项测试通过。

## 数值证据

| 对象 | 结果 | 限制 |
| --- | --- | --- |
| Qwen3 0.6B / 1.7B | 已保存的 CUDA 固定输入 ORT 回放精确一致 | 早期记录为抽样调用，不能代表所有 token、输入边界或完整 LLM logits |
| VoxCPM2 | 已保存的 CUDA 固定输入 ORT 回放精确一致 | 同上；模型切换与所有参考音频边界尚未完整覆盖 |
| MOSS | 新基准 144 次 CUDA 固定输入回放精确一致，图及外部权重摘要、环境门禁均通过 | 一次参考音频与短文本运行；尚未覆盖全部输入矩阵 |
| S2-Pro 原默认配置 | 失败，旧环境自回放也失败 | 原始失败样本和报告保留，不放宽阈值 |
| S2-Pro 同确定性配置 | 主进程及子进程共 12 次完整记录回放精确一致 | 不能代替旧默认到确定性计算的迁移验收 |
| Qwen ASR | 新基准 13 次 CUDA encoder 回放通过数值、模型身份与环境门禁；固定文本 token 历史的 8 组完整 logits 及缓存清空重放精确一致 | logits 探针尚未覆盖完整音频 embedding 历史；真实分块转写文本相同 |
| Smart Turn / Whisper | 真实输入最大特征绝对差约 1.68e-5；五个非静音公共 PCM 样例判定一致，概率最大差 8.94e-7 | FFT、窗口及 log10 的严格预算未证明，数值验收未通过；静音／量化后全零的极小输入旧概率为 NaN，候选修复后有限，单独记录 |
| VAD、Speaker Verification、Text EOU | 同参考音频的 8 帧 VAD 概率、192 维声纹向量及单条文本 EOU 概率精确一致；CPU 环境门禁通过 | 首轮样例对照，不代表全部边界或完整链路验收 |
| Wakeword | 旧新均可运行，未命中结果相同 | 参考音频不含指定唤醒词，未验证命中 |

S2 根因已通过单变量实验与 ORT 1.21.1 源码交叉核对：TransposeOptimizer 把归约移到转置前，
归约从最后一轴变为轴 1，CUDA 快速归约使用多个 block 的浮点原子累加。单独禁用
TransposeOptimizer、关闭图优化或启用 `use_deterministic_compute` 都使重复计算稳定。
SDK 使用确定性设置，不修改模型文件、权重、精度、公式和采样配置。源码依据：
[归约布局](https://github.com/microsoft/onnxruntime/blob/v1.21.1/onnxruntime/core/providers/cuda/reduction/reduction_functions.cc#L141)、
[原子累加](https://github.com/microsoft/onnxruntime/blob/v1.21.1/onnxruntime/core/providers/cuda/reduction/reduction_functions.cu#L430)、
[确定性开关](https://github.com/microsoft/onnxruntime/blob/v1.21.1/onnxruntime/core/providers/cuda/reduction/reduction_ops.cc#L689)。

Whisper 初版区间宽度可达 0.44249，DFT 界没有证明适用于实际 FFT，窗口及对数 ULP 假设未证明。
`whisper-rounding.json` 仅保留历史实验；`whisper-stages.json` 与 `whisper_stages.py` 是新的逐阶段
观察证据。不能把“落入初版估计区间”写成等价验收通过。

回放工具现将 `numeric_exact`、`environment_match` 与 `model_identity_match` 分开报告，
三者都成立才标记 `recorded_calls_pass`。这只表示保存的调用通过，不表示整个模型验收完成。
ONNX 身份包含图和所有 external-data 文件；历史 MOSS 记录没有外部权重摘要，保留为观察，
不补写当前摘要。旧按需加载记录缺少部分 DLL，新增 `baseline_runtime.py` 在独立基准进程加载
与候选对应的旧库，不改动基准安装文件。新证据分别保存在 `moss-complete-replay.json`、
`asr-complete-replay.json`、`s2-det-evidence-gated.json`、`asr-logits-comparison.json` 和
`nearmate-component-comparison.json`。早期因缺证据被阻断的报告继续保留。

## 耗时、内存与体积

一次短文本的合成耗时（旧 / 候选，秒）：Qwen3 0.6B 0.655 / 0.622，Qwen3 1.7B 0.636 / 0.625，
VoxCPM2 1.264 / 1.342，MOSS 1.362 / 1.066。S2 最终确定性候选约 1.252 秒。
这些是单次运行，包含不同的记录开销及自由生成长度，不构成性能优劣结论。

对应主进程结束 RSS（旧 / 候选，GiB）：Qwen3 0.6B 1.685 / 1.735，Qwen3 1.7B 2.754 / 2.786，
VoxCPM2 3.595 / 3.422，MOSS 1.178 / 1.158。不是峰值，不包含完整子进程和 GPU 内存。

当前目录逻辑大小：基线验证环境 6,172,341,725 字节，候选验证环境 3,946,330,230 字节，
NearMate 独立开发环境 4,302,596,885 字节。它们的开发依赖不同，且可能使用硬链接，不能直接
当成安装器压缩包大小或磁盘实际节省量；正式发行包大小仍需独立测量。

## 试听与剩余工作

试听清单见 `.scratch/torch-free/listening/index.html`。统一文本为“你好，我是你的语音伙伴。今天过得怎么样？”，
参考音频为 `Condition3.wav`，配置详情随每份 `run.json` 保留。最终自由生成音频不逐采样验收，
听感由用户确认。

仍需完成：Whisper 严格误差预算与 Smart Turn 更完整的判定边界；S2 默认到确定性计算的迁移验收；
全五模型真实音频、静音、极小幅度、长短输入、分块边界、缓存和切换矩阵；固定 LLM token 历史
数值捕获（ASR 固定文本历史已有初步证据）；各输入组件边界对拍；CPU/DirectML 各自基线；
完整内存峰值及发行体积；用户试听。
AMD/Intel 硬件、安装程序、下载向导和商业授权端到端未验证。所有基线与失败样本保留。
