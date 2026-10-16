# coding=utf-8
import os
import time
from streamvox_runtime import ensure_single_onnxruntime_distribution, initialize_nvidia_runtime
from dataclasses import dataclass
from typing import Sequence
import numpy as np

from . import logger




ensure_single_onnxruntime_distribution()
import onnxruntime as ort






@dataclass(frozen=True)
class OnnxProviderSelection:
    """ASR ONNX provider 选择结果。"""

    requested_provider: str
    providers: list[str]
    active_provider: str
    active_dml: bool
    reason: str
    available_providers: tuple[str, ...]


def resolve_onnx_provider_policy(
    requested_provider: str,
    *,
    available_providers: Sequence[str],
) -> OnnxProviderSelection:
    """根据请求策略和 ORT 可用 provider 选择 ASR ONNX 执行 provider。"""

    available = tuple(available_providers)
    normalized = requested_provider.strip().lower()
    provider_map = {
        "cpu": "CPUExecutionProvider",
        "cuda": "CUDAExecutionProvider",
        "dml": "DmlExecutionProvider",
    }

    if normalized == "auto":
        for provider in ("CUDAExecutionProvider", "DmlExecutionProvider", "CPUExecutionProvider"):
            if provider in available:
                return _build_onnx_selection(
                    requested_provider=normalized,
                    active_provider=provider,
                    available_providers=available,
                    reason=f"auto 命中 {provider}",
                )

        return _build_onnx_selection(
            requested_provider=normalized,
            active_provider="CPUExecutionProvider",
            available_providers=available,
            reason="ORT 未报告可用 provider，强制回落 CPUExecutionProvider",
        )

    if normalized not in provider_map:
        raise ValueError("onnx_provider 只能是 auto、cpu、cuda 或 dml")

    requested_ort_provider = provider_map[normalized]
    if requested_ort_provider in available:
        return _build_onnx_selection(
            requested_provider=normalized,
            active_provider=requested_ort_provider,
            available_providers=available,
            reason=f"显式请求 {requested_ort_provider}",
        )

    return _build_onnx_selection(
        requested_provider=normalized,
        active_provider="CPUExecutionProvider",
        available_providers=available,
        reason=f"{requested_ort_provider} 不可用，回落 CPUExecutionProvider",
    )


def _build_onnx_selection(
    *,
    requested_provider: str,
    active_provider: str,
    available_providers: tuple[str, ...],
    reason: str,
) -> OnnxProviderSelection:
    """构造 ONNX provider 选择结果。"""

    providers = [active_provider]
    if active_provider != "CPUExecutionProvider":
        providers.append("CPUExecutionProvider")

    return OnnxProviderSelection(
        requested_provider=requested_provider,
        providers=providers,
        active_provider=active_provider,
        active_dml=active_provider == "DmlExecutionProvider",
        reason=reason,
        available_providers=available_providers,
    )


class FastWhisperMel:
    """基于 NumPy 的纯净版 Mel 提取器 (彻底干掉 librosa 的 numba JIT 启动延时)"""
    def __init__(self, filter_path: str = None, n_mels=128, sr=16000, n_fft=400, f_min=0, f_max=8000, norm="slaney", mel_scale="slaney"):
        self.n_fft = n_fft
        self.hop_length = 160
        self.n_mels = n_mels
        
        if filter_path and os.path.exists(filter_path):
            self.filters = np.load(filter_path)
        else:
            self.filters = self._generate_filters(sr, n_fft, n_mels, f_min, f_max, norm, mel_scale)
            
        # 提前计算并缓存好汉明窗 (Qwen3/Whisper/Librosa 使用 Hann 窗)
        self.window = 0.5 - 0.5 * np.cos(2 * np.pi * np.arange(self.n_fft) / self.n_fft)
        
    def _generate_filters(self, sr, n_fft, n_mels, f_min, f_max, norm, mel_scale):
        """
        生成组件化的梅尔滤波器组 (兼容 torchaudio 行为)
        norm: "slaney" (面积归一化) 或 None
        mel_scale: "slaney" (分段线性+对数) 或 "htk" (纯对数)
        """
        def hz_to_mel(freq, scale):
            if scale == "htk":
                return 2595.0 * np.log10(1.0 + (freq / 700.0))
            # Slaney Scale (Linear + Log)
            f_min_sl, f_sp_sl = 0.0, 200.0 / 3
            mels = (freq - f_min_sl) / f_sp_sl
            min_log_hz, logstep = 1000.0, np.log(6.4) / 27.0
            min_log_mel = (min_log_hz - f_min_sl) / f_sp_sl
            if isinstance(freq, np.ndarray):
                mask = freq >= min_log_hz
                mels[mask] = min_log_mel + np.log(freq[mask] / min_log_hz) / logstep
            elif freq >= min_log_hz:
                mels = min_log_mel + np.log(freq / min_log_hz) / logstep
            return mels

        def mel_to_hz(mels, scale):
            if scale == "htk":
                return 700.0 * (10.0 ** (mels / 2595.0) - 1.0)
            # Slaney Scale (Linear + Log)
            f_min_sl, f_sp_sl = 0.0, 200.0 / 3
            freqs = f_min_sl + f_sp_sl * mels
            min_log_hz, logstep = 1000.0, np.log(6.4) / 27.0
            min_log_mel = (min_log_hz - f_min_sl) / f_sp_sl
            if isinstance(mels, np.ndarray):
                mask = mels >= min_log_mel
                freqs[mask] = min_log_hz * np.exp(logstep * (mels[mask] - min_log_mel))
            elif mels >= min_log_mel:
                freqs = min_log_hz * np.exp(logstep * (mels - min_log_mel))
            return freqs

        n_freqs = n_fft // 2 + 1
        all_freqs = np.linspace(0, sr // 2, n_freqs)
        m_pts = np.linspace(hz_to_mel(f_min, mel_scale), hz_to_mel(f_max, mel_scale), n_mels + 2)
        f_pts = mel_to_hz(m_pts, mel_scale)
        f_diff = f_pts[1:] - f_pts[:-1]
        slopes = f_pts[np.newaxis, :] - all_freqs[:, np.newaxis]
        down_slopes = (-1.0 * slopes[:, :-2]) / f_diff[:-1]
        up_slopes = slopes[:, 2:] / f_diff[1:]
        fb = np.maximum(0, np.minimum(down_slopes, up_slopes))
        
        # Area Normalization
        if norm == "slaney":
            enorm = 2.0 / (f_pts[2 : n_mels + 2] - f_pts[:n_mels])
            fb *= enorm[np.newaxis, :]
            
        return fb.astype(np.float32)
        
    def __call__(self, audio: np.ndarray, dtype=np.float32) -> np.ndarray:
        # 1. Padding (与 librosa 的 center=True 行为保持一致)
        pad_len = int(self.n_fft // 2)
        y = np.pad(audio, pad_len, mode='reflect')
        
        # 2. 高效分帧 (利用 numpy 内存视图，耗时几乎为0)
        num_frames = 1 + (len(y) - self.n_fft) // self.hop_length
        shape = (self.n_fft, num_frames)
        strides = (y.itemsize, self.hop_length * y.itemsize)
        frames = np.lib.stride_tricks.as_strided(y, shape=shape, strides=strides)
        
        # 3. 加窗并执行实数 FFT
        stft_res = np.fft.rfft(frames * self.window[:, np.newaxis], axis=0)
        
        # 4. 能量谱
        magnitudes = np.abs(stft_res) ** 2
        
        # 5. Mel 映射
        mel_spec = np.dot(self.filters.T, magnitudes)
        
        # 6. 取对数
        log_spec = np.log10(np.maximum(mel_spec, 1e-10))
        
        # 7. 归一化
        log_spec = np.maximum(log_spec, log_spec.max() - 8.0)
        log_spec = (log_spec + 4.0) / 4.0
        
        # 8. 帧对齐：丢弃多余帧
        n_frames_out = audio.shape[-1] // self.hop_length
        log_spec = log_spec[:, :n_frames_out]
        
        return log_spec.astype(dtype)

def get_feat_extract_output_lengths(input_lengths):
    """
    完全复刻官方 Qwen3 前端逻辑，计算最终有效的输出帧数。
    用于从拼接好的 (N*13) 结果中切出有效部分。
    """
    input_lengths_leave = input_lengths % 100
    feat_lengths = (input_lengths_leave - 1) // 2 + 1
    output_lengths = ((feat_lengths - 1) // 2 + 1 - 1) // 2 + 1 + (input_lengths // 100) * 13
    return int(output_lengths)

class QwenAudioEncoder:
    """Qwen3 音频编码器 (Split Frontend + Backend)"""
    def __init__(
        self,
        frontend_path: str,
        backend_path: str,
        onnx_provider: str = 'auto',
        dml_pad_to: int = 30,
        intra_threads: int | None = None,
        inter_threads: int | None = None,
    ):
        """初始化拆分式 ONNX 音频编码器。"""
        self.onnx_provider = onnx_provider
        self.active_dml = False
        self.dml_pad_to = dml_pad_to
        # 预计算目标长度：每 1 秒对应 13 帧 hidden_states
        self.h_target_len = self.dml_pad_to * 13
        
        # 初始化 ONNX Session Options
        sess_opts = ort.SessionOptions()
        sess_opts.log_severity_level = 3
        sess_opts.add_session_config_entry("session.intra_op.allow_spinning", "0")
        sess_opts.add_session_config_entry("session.inter_op.allow_spinning", "0")
        sess_opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL

        # 显式线程配置只在调用方传入时生效。
        # 这样默认路径保持 ONNX Runtime 原有策略，同时允许 WebApp CPU 场景做可复现实验。
        if intra_threads is not None:
            if intra_threads < 1:
                raise ValueError(f"intra_threads 必须大于等于 1，当前收到 {intra_threads}")
            sess_opts.intra_op_num_threads = int(intra_threads)

        # inter_threads 控制算子图层面的并行调度。
        # 实时短音频通常更关注稳定 p95，因此后续 benchmark 可以优先测试 inter_threads=1。
        if inter_threads is not None:
            if inter_threads < 1:
                raise ValueError(f"inter_threads 必须大于等于 1，当前收到 {inter_threads}")
            sess_opts.inter_op_num_threads = int(inter_threads)
        
        available_providers = ort.get_available_providers()
        provider_selection = resolve_onnx_provider_policy(
            self.onnx_provider,
            available_providers=available_providers,
        )
        providers = provider_selection.providers
        if "CUDAExecutionProvider" in providers:
            initialize_nvidia_runtime(consumer="onnx_cuda", required=True)
        self.active_dml = provider_selection.active_dml
        if self.active_dml:
            # DirectML 对动态 shape 和实时短音频更适合顺序执行；关闭 memory pattern 可避免 DML EP 初始化报错。
            sess_opts.enable_mem_pattern = False
            sess_opts.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        if provider_selection.active_provider == "CPUExecutionProvider" and provider_selection.requested_provider not in {"auto", "cpu"}:
            logger.warning("[Encoder] 请求的 ONNX provider 不可用，已回落 CPU：%s", provider_selection.reason)
            
        logger.info(
            "[Encoder] 加载 Split ONNX 模型，Request=%s，Available=%s，Selected=%s，Reason=%s，Pad=%ss，Threads intra=%s inter=%s，Frontend=%s，Backend=%s",
            provider_selection.requested_provider,
            provider_selection.available_providers,
            providers,
            provider_selection.reason,
            dml_pad_to,
            intra_threads or "ORT默认",
            inter_threads or "ORT默认",
            os.path.basename(frontend_path),
            os.path.basename(backend_path),
        )

        # 加载两个 Session
        self.sess_fe = ort.InferenceSession(frontend_path, sess_options=sess_opts, providers=providers)
        self.sess_be = ort.InferenceSession(backend_path, sess_options=sess_opts, providers=providers)
        logger.info(
            "[Encoder] ONNX Session 实际 providers：Frontend=%s，Backend=%s",
            self.sess_fe.get_providers(),
            self.sess_be.get_providers(),
        )
        
        self.mel_extractor = FastWhisperMel()
        
        # 检测精度 (以前端为准)
        try:
            fe_input_type = self.sess_fe.get_inputs()[0].type
            self.input_dtype = np.float16 if 'float16' in fe_input_type else np.float32
        except:
            self.input_dtype = np.float32

        # 预热处理
        if self.dml_pad_to > 0 and self.active_dml:
            logger.info("[Encoder] 正在预热，固定形状=%ss", self.dml_pad_to)
            dummy_wav = np.zeros(int(16000 * self.dml_pad_to)).astype(np.float32)
            _ = self.encode(dummy_wav)
        else:
            # 非 DML 模式下，预热一个短音频即可，无需 Padding
            logger.info("[Encoder] 正在预热，非 DML 模式")
            dummy_wav = np.zeros(int(16000 * 2.0)).astype(np.float32)
            _ = self.encode(dummy_wav)
   
        logger.info("[Encoder] 预热完成")

    def _run_frontend(self, mel: np.ndarray) -> np.ndarray:
        """前端推理流水线：Pad -> Chunk Loop -> Concat -> Slice"""
        T = mel.shape[1]
        
        # 1. 必须 Pad 到 100 的倍数
        pad_len = (100 - (T % 100)) % 100
        if pad_len > 0:
            mel = np.pad(mel, ((0,0), (0, pad_len)), mode='constant')
        
        # 增加 batch 维 -> (1, 128, T_padded)
        mel_input = mel[np.newaxis, ...]
        
        num_chunks = mel_input.shape[2] // 100
        fe_outputs = []
        chunk_size = 100
        
        # 2. 循环推理 (Atomic Inference)
        for i in range(num_chunks):
            start = i * chunk_size
            chunk = mel_input[:, :, start : start + chunk_size]
            out = self.sess_fe.run(None, {"chunk_mel": chunk})[0] # (1, 13, 896/1024)
            fe_outputs.append(out)
            
        # 3. 拼接结果 -> (1, N_frames, D)
        hidden_states = np.concatenate(fe_outputs, axis=1)
        
        # 4. 有效长度切片 (关键: 去除 Padding 带来的尾部垃圾帧)
        t_out = get_feat_extract_output_lengths(T)
        hidden_states = hidden_states[:, :t_out, :]
        
        return hidden_states

    def _run_backend(self, hidden_states: np.ndarray) -> np.ndarray:
        """后端推理流水线：Mask -> Transformer (支持固定形状 Padding)"""
        batch, seq_len, dim = hidden_states.shape
        
        # 1. 形状检查与 Padding (仅在 DML 开启时执行)
        if self.active_dml and seq_len < self.h_target_len:
            pad_width = self.h_target_len - seq_len
            # 对 hidden_states 进行零填充 -> (Batch, T_fixed, D)
            hidden_input = np.pad(hidden_states, ((0,0), (0, pad_width), (0,0)), mode='constant')
            
            # 构造 Mask：前 seq_len 为 0 (关注)，后 pad_width 为 -10000.0 (屏蔽)
            # 维度需要广播到 (Batch, 1, T_fixed, T_fixed)
            mask = np.zeros((batch, 1, self.h_target_len, self.h_target_len), dtype=self.input_dtype)
            mask[:, :, :, seq_len:] = -10000.0
        else:
            hidden_input = hidden_states
            mask = np.zeros((batch, 1, seq_len, seq_len), dtype=self.input_dtype)
        
        # 2. 执行推理
        audio_embd = self.sess_be.run(None, {
            "hidden_states": hidden_input,
            "attention_mask": mask
        })[0]
        
        # 3. 截断输出 -> (Batch, seq_len, D)
        if audio_embd.shape[1] > seq_len:
            audio_embd = audio_embd[:, :seq_len, :]
            
        return audio_embd

    def encode(self, audio: np.ndarray) -> tuple:
        """执行编码 (Mel -> Frontend -> Backend)，返回 (embedding, 耗时)"""
        t0 = time.time()
        
        # 1. 提取 Mel 特征
        # audio: (N_samples,) -> mel: (128, T)
        mel = self.mel_extractor(audio, dtype=self.input_dtype) 
        
        # 2. Frontend (Loop)
        hidden_states = self._run_frontend(mel)
        
        # 3. Backend (Transformer)
        audio_embd = self._run_backend(hidden_states)
        
        # 4. 去除 Batch 维 -> (T, D)
        if audio_embd.ndim == 3: 
            audio_embd = audio_embd[0]
            
        elapsed = time.time() - t0
        return audio_embd, elapsed
