# coding=utf-8
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Dict, List, Literal, Optional
import numpy as np

class MsgType(Enum):
    CMD_ENCODE = auto()   # 主进程 -> Encoder: 编码请求
    CMD_ALIGN = auto()    # 主进程 -> Aligner: 对齐请求
    CMD_STOP = auto()     # 主进程 -> Worker: 停止请求
    MSG_EMBD = auto()     # Worker -> 主进程: 返回特征 (Encoder)
    MSG_ALIGN = auto()    # Worker -> 主进程: 返回对齐结果 (Aligner)
    MSG_READY = auto()    # Worker -> 主进程: 就绪信号
    MSG_DONE = auto()     # Worker -> 主进程: 已退出信号
    MSG_ERROR = auto()    # Worker -> 主进程: 错误信号

@dataclass
class StreamingMessage:
    """音频编码/对齐进程通用通信协议"""
    msg_type: MsgType
    data: Any = None         # 存放音频 chunk 或 embedding/align 结果
    text: Optional[str] = None # 用于对齐的文本
    offset_sec: float = 0.0  # 对齐的时间轴偏移
    language: Optional[str] = None # 语言
    is_last: bool = False    # 标记是否为最后一段音频
    encode_time: float = 0.0 # 耗时统计

@dataclass
class DecodeResult:
    """LLM 解码内核输出标准化"""
    text: str = ""           # 包含前缀的完整文本
    new_text: str = ""       # 本次增量生成的文本
    stable_tokens: List[int] = field(default_factory=list)
    # 每个稳定输出位置对应的 Top-K token id 列表。
    # 这里按“稳定输出 token”的顺序保存，而不是按原始生成步保存，
    # 目的是让热词雷达替换时可以直接用 token 下标定位文本片段。
    topk_token_ids: List[List[int]] = field(default_factory=list)
    # 每个稳定输出位置对应的 Top-K 相对概率列表。
    # 概率只用于命中候选之间排序，不参与采样决策，因此不会改变模型生成路径。
    topk_token_probs: List[List[float]] = field(default_factory=list)
    # 当前 chunk 内检测到的热词命中信息。
    # 使用 dict 保持返回结构轻量，便于上层按需透传或调试打印。
    hotword_hits: List[Dict[str, Any]] = field(default_factory=list)
    t_prefill: float = 0.0   # 预填充耗时 (ms)
    t_generate: float = 0.0  # 生成耗时 (ms)
    n_prefill: int = 0       # 预填充 token 数
    n_generate: int = 0      # 生成 token 数
    is_aborted: bool = False # 是否因重复或其他原因熔断中断

@dataclass(frozen=True)
class ForcedAlignItem:
    """单个词/字符的对齐结果"""
    text: str
    start_time: float        # 单位：秒
    end_time: float          # 单位：秒

@dataclass
class ForcedAlignResult:
    """对齐结果标准化集合 (官方结构化输出格式)"""
    items: List[ForcedAlignItem]
    performance: Optional[dict] = None

    def __iter__(self):
        return iter(self.items)

    def __len__(self):
        return len(self.items)

    def __getitem__(self, idx: int) -> ForcedAlignItem:
        return self.items[idx]

@dataclass
class AlignerConfig:
    """对齐引擎配置"""
    model_dir: str
    # 拆分为 Frontend 和 Backend
    encoder_frontend_fn: str = "qwen3_aligner_encoder_frontend.int4.onnx"
    encoder_backend_fn: str = "qwen3_aligner_encoder_backend.int4.onnx"

    llm_fn: str = "qwen3_aligner_llm.q4_k.gguf" 
    onnx_provider: str = 'auto'  # auto, CPU, CUDA, DML
    llm_use_gpu: bool = True
    llm_backend: Literal["vulkan", "cuda", "cpu", "auto"] = "auto"
    n_ctx: int = 2048  # 对于 Aligner Decoder，每秒音频+文字，约占 30 个 token
    dml_pad_to: int = 40  # Encoder 填充时长


@dataclass
class ASREngineConfig:
    """ASR 识别引擎配置"""
    model_dir: str
    encoder_frontend_fn: str = "qwen3_asr_encoder_frontend.fp16.onnx"
    encoder_backend_fn: str = "qwen3_asr_encoder_backend.fp16.onnx"   # qwen3_asr_encoder_backend.int4.onnx
    llm_fn: str = "qwen3_asr_llm.f16.gguf"  # qwen3_asr_llm.q4_k.gguf

    onnx_provider: str = 'auto'  # auto, CPU, CUDA, DML
    llm_use_gpu: bool = True
    llm_backend: Literal["vulkan", "cuda", "cpu", "auto"] = "auto"
    dml_pad_to: int = 40        # 使用 DirectML 加速 onnx 时，Encoder 填充时长
    # 线程参数先统一收敛到引擎配置层，避免后续在 encoder / llama.cpp 中各自读取外部配置，
    # 这样可以把“业务层给出的 CPU 调优意图”稳定地下发到具体执行模块。
    encoder_intra_threads: Optional[int] = None
    encoder_inter_threads: Optional[int] = None
    llm_threads: Optional[int] = None
    llm_threads_batch: Optional[int] = None
    # 这里按当前工程里的 Split Encoder 实际输出长度估算：
    # 16kHz 音频先转成 10ms 一帧的 mel，再经过前端下采样后约为 13 帧/秒。
    # 以 40s chunk 为例，当前 get_feat_extract_output_lengths(4000) = 520，
    # 再叠加提示词、特殊 token 与识别文本后，n_ctx=2048 仍然是按“音频 token + 文本 token”共同预算出来的。
    n_ctx: int = 2048
    chunk_size: float = 40.0    # 每个片段 40s，当前编码器输出约 520 个音频 token
    memory_num: int = 1         # 记忆一个片段时，下一片解码阶段会额外带入约 520 个历史音频 token
    # 热词来源可以是路径、字符串列表或多行文本。
    # None 表示底层 ASR 引擎完全不构建热词雷达，默认保持旧行为。
    hotwords: Optional[Any] = None
    # 每个生成位置观察的候选 token 数。
    hotword_top_k: int = 8
    # 多 token 热词匹配时，从当前命中 token 往后最多看的生成位置数。
    hotword_max_lookahead: int = 12
    # 多 token 热词匹配时允许跳过的非热词生成位置数量。
    hotword_max_gap: int = 1
    verbose: bool = True
    enable_aligner: bool = False
    align_config: Optional[AlignerConfig] = None

    def __post_init__(self):
        # 如果没有显式设置 Encoder 填充时长，则默认与 LLM 分段识别时长对齐
        if self.dml_pad_to is None:
            object.__setattr__(self, 'pad_to', int(self.chunk_size))
            
        if self.align_config is None:
            object.__setattr__(self, 'align_config', AlignerConfig(
                model_dir=self.model_dir,
                onnx_provider=self.onnx_provider,
                llm_use_gpu=self.llm_use_gpu,
                llm_backend=self.llm_backend,
                dml_pad_to=self.dml_pad_to # Aligner 默认也跟随主 pad_to
            ))
        elif self.align_config.dml_pad_to is None:
             object.__setattr__(self.align_config, 'dml_pad_to', int(self.chunk_size))


@dataclass
class TranscribeResult:
    """ASR 转录结果 (含可选的对齐信息)"""
    text: str
    alignment: Optional[ForcedAlignResult] = None
    performance: Optional[dict] = None
    # 最终文本中应用过的热词命中列表。
    # 当前 Web Demo 不展示该字段，但保留它可以支持后续调试和指标统计。
    hotword_hits: List[Dict[str, Any]] = field(default_factory=list)
