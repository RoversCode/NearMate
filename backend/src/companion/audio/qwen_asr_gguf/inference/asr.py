# coding=utf-8
import gc
import os
import time
import codecs
import dataclasses
import numpy as np
import multiprocessing as mp
from pathlib import Path
from collections import deque
from typing import Optional, List

from .schema import (
    MsgType,
    StreamingMessage,
    DecodeResult,
    ASREngineConfig,
    TranscribeResult,
    ForcedAlignItem,
    ForcedAlignResult,
)
from .utils import normalize_language_name, validate_language
from .encoder import QwenAudioEncoder
from .hotwords import HotwordRadar, load_hotwords
from . import llama
from .llama_backend import resolve_llama_backend_policy
from . import logger


@dataclasses.dataclass
class ASRS_Segment:
    """管理分片记忆及其物理时间坐标"""

    idx: int
    audio_start: float
    audio_end: float
    text: str = ""
    items: List[ForcedAlignItem] = None


class QwenASREngine:
    """Qwen3-ASR 流式转录引擎 (GGUF 后端) - 统一辅助进程架构"""

    def __init__(self, config: ASREngineConfig):
        self.config = config
        self.verbose = config.verbose
        logger.info("[QwenASR] 初始化引擎，Provider=%s", config.onnx_provider)

        # 路径解析
        llm_gguf = os.path.join(config.model_dir, config.llm_fn)
        frontend_path = os.path.join(config.model_dir, config.encoder_frontend_fn)
        backend_path = os.path.join(config.model_dir, config.encoder_backend_fn)

        # 1. 初始化 Encoder
        self.encoder = QwenAudioEncoder(
            frontend_path=frontend_path,
            backend_path=backend_path,
            onnx_provider=config.onnx_provider,
            dml_pad_to=config.dml_pad_to,
            intra_threads=config.encoder_intra_threads,
            inter_threads=config.encoder_inter_threads,
        )

        # 2. 初始化 Aligner (可选)
        self.aligner = None
        if config.enable_aligner and config.align_config:
            # 时间戳对齐链路完全独立于主识别链路，因此只在用户显式启用时加载，
            # 避免无时间戳需求时额外占用显存和初始化时间。
            from .aligner import QwenForcedAligner

            self.aligner = QwenForcedAligner(config.align_config)

        # 3. 加载识别 LLM
        # GGUF 模型负责“文本自回归解码”。
        # 这里同时读取 embedding_table，是因为本项目采用“手工拼 embedding 序列”的方式把音频和文本送进 LLM。
        llama_backend_resolution = resolve_llama_backend_policy(
            llama.resolve_backend_bin_dir(),
            requested_backend=config.llm_backend,
        )
        logger.info(
            "[QwenASR] LLM backend 选择：requested=%s selected=%s use_gpu=%s candidates=%s nvidia=%s reason=%s",
            llama_backend_resolution.requested_backend,
            llama_backend_resolution.backend,
            llama_backend_resolution.use_gpu,
            llama_backend_resolution.candidate_backends,
            llama_backend_resolution.nvidia_gpu_names,
            llama_backend_resolution.reason,
        )
        for skipped_reason in llama_backend_resolution.skipped_reasons:
            logger.info("[QwenASR] LLM backend 跳过候选：%s", skipped_reason)
        llama_model_backend = (
            llama_backend_resolution.backend
            if llama_backend_resolution.use_gpu
            else config.llm_backend
        )
        self.model = llama.LlamaModel(
            llm_gguf,
            use_gpu=llama_backend_resolution.use_gpu,
            backend=llama_model_backend,
        )
        self.embedding_table = llama.get_token_embeddings_gguf(llm_gguf)
        self.ctx = llama.LlamaContext(
            self.model, n_ctx=config.n_ctx, n_batch=4096, embeddings=False
        )
        if getattr(self.model, "actual_backend", None):
            logger.info("[QwenASR] LLM backend 实际命中：%s", self.model.actual_backend)

        # 4. 初始化热词雷达 (可选)
        # 热词雷达依赖真实 GGUF tokenizer，因此必须在 self.model 加载完成后构建。
        # 若配置为空或热词文件不存在，则保持 None，主识别链路完全沿用旧行为。
        self.hotword_radar = None
        loaded_hotwords = load_hotwords(config.hotwords)
        if loaded_hotwords:
            self.hotword_radar = HotwordRadar(loaded_hotwords, tokenizer=self.model)

            logger.info("[QwenASR] 热词雷达已加载 %s 个热词", len(loaded_hotwords))

        # 缓存 Token ID
        # 这些特殊 token 会在 _build_prompt_embd 里反复使用，初始化时缓存下来可以减少重复查表。
        self.ID_IM_START = self.model.token_to_id("<|im_start|>")
        self.ID_IM_END = self.model.token_to_id("<|im_end|>")
        self.ID_AUDIO_START = self.model.token_to_id("<|audio_start|>")
        self.ID_AUDIO_END = self.model.token_to_id("<|audio_end|>")
        self.ID_ASR_TEXT = self.model.token_to_id("<asr_text>")

    def shutdown(self):
        """关闭 Qwen ASR 引擎持有的本地推理资源。"""

        # shutdown 可能被 provider、RealtimeInputRuntime 或进程退出兜底路径重复调用；已关闭时直接返回保持幂等。
        if getattr(self, "_shutdown", False):
            return
        self._shutdown = True

        # 先记录需要显式关闭的对象；部分对象没有 shutdown 方法，只依赖引用释放触发自身析构。
        resources_to_shutdown = (
            getattr(self, "encoder", None),
            getattr(self, "aligner", None),
        )
        shutdown_errors = []
        for resource in resources_to_shutdown:
            # 测试替身或未来实现可能不暴露 shutdown，缺失时交给后续引用清理路径处理。
            shutdown_resource = getattr(resource, "shutdown", None)
            if not callable(shutdown_resource):
                continue

            try:
                shutdown_resource()
            except Exception as exception:
                # 关闭阶段要尽量释放所有资源；先收集失败，避免一个子资源阻断后续 native 指针和 memmap 清理。
                shutdown_errors.append(exception)

        # 按依赖反向断开引用：热词雷达和 context 可能间接持有 model，必须在 model 前释放。
        self.hotword_radar = None
        self.aligner = None
        self.ctx = None
        self.model = None
        self.embedding_table = None
        self.encoder = None

        # llama.cpp wrapper 和 ONNX session 都主要依赖析构释放 native 资源；主动 GC 可以缩短异常退出时的资源占用窗口。
        gc.collect()

        logger.info("[QwenASR] 引擎已关闭")

        if shutdown_errors:
            raise ExceptionGroup("QwenASREngine resource shutdown failed", shutdown_errors)

    def _build_prompt_embd(
        self,
        audio_embd: np.ndarray,
        prefix_text: str,
        context: Optional[str],
        language: Optional[str],
    ):
        """构造用于 LLM 输入的 Embedding 序列 (区块化打包模式)"""

        def tk(t):
            return self.model.tokenize(t)

        # 1. 区块 A: 音频之前的所有内容 (System + User Header)
        # 这一段完全由文本 token 组成，作用是把“系统提示 + user 起始 + 音频开始标记”组织成多模态 prompt 前缀。
        prefix_str = f"system\n{context or 'You are a helpful assistant.'}"
        prefix_tokens = (
            [self.ID_IM_START]
            + tk(prefix_str)
            + [self.ID_IM_END]
            + [self.ID_IM_START]
            + tk("user\n")
            + [self.ID_AUDIO_START]
        )

        # 2. 区块 B: 音频之后的所有内容 (Instruction + Assistant Header + History)
        # language 会以文本控制指令的形式追加在 assistant header 后面。
        # prefix_text 则承载历史 chunk 的稳定文本，让当前 chunk 解码时拥有跨段上下文。
        suffix_head = f"assistant\n"
        if language:
            suffix_head += f"language {language}"

        suffix_tokens = (
            [self.ID_AUDIO_END]
            + [self.ID_IM_END]
            + [self.ID_IM_START]
            + tk(suffix_head)
            + [self.ID_ASR_TEXT]
            + tk(prefix_text)
        )

        # 3. 统计并拼接
        # 这里的关键点是：文本部分来自 embedding_table，音频部分直接使用 encoder 产出的向量。
        # 最终 LLM 看到的是一条已经在 embedding 级别拼好的“统一序列”。
        n_pre, n_aud, n_suf = (
            len(prefix_tokens),
            audio_embd.shape[0],
            len(suffix_tokens),
        )
        total_embd = np.zeros(
            (n_pre + n_aud + n_suf, self.model.n_embd), dtype=np.float32
        )

        total_embd[:n_pre] = self.embedding_table[prefix_tokens]
        total_embd[n_pre : n_pre + n_aud] = audio_embd
        total_embd[n_pre + n_aud :] = self.embedding_table[suffix_tokens]

        return total_embd

    def _decode(
        self,
        full_embd: np.ndarray,
        prefix_text: str,
        rollback_num: int,
        is_last_chunk: bool = False,
        collect_top_k: int = 0,
    ) -> DecodeResult:
        """使用贪心策略执行一次 LLM 解码。"""
        result = DecodeResult()

        # full_embd 是已经拼接好的“文本 embedding + 音频 embedding + 文本 embedding”。
        # llama.cpp 这边会把它当成一次完整 prefill 输入。
        total_len = full_embd.shape[0]
        pos_base = np.arange(0, total_len, dtype=np.int32)
        pos_arr = np.concatenate(
            [pos_base, pos_base, pos_base, np.zeros(total_len, dtype=np.int32)]
        )  # mrope
        
        batch = llama.LlamaBatch(max(total_len * 4, 8192), self.model.n_embd, 1)
        batch.set_embd(full_embd, pos=pos_arr)

        # 1. Prefill
        self.ctx.clear_kv_cache()
        t_pre_start = time.time()
        self.ctx.decode(batch) # prefill
        prefill_time = time.time() - t_pre_start

        # 2. Generation Loop
        # 这里开始进入逐 token 生成阶段。
        # prefill 负责建立上下文，generation 负责一边采样一边把新 token 写入 KV Cache。
        t_gen_start = time.time()
        n_gen_tokens = 0
        display_queue = deque()
        display_topk_queue = deque()
        stable_tokens = []
        stable_topk_ids = []
        stable_topk_probs = []
        stable_text_acc = ""
        text_decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

        # ASR 只走贪心采样，保证同一段音频在同一模型版本下尽量稳定可复现。
        sampler = llama.LlamaSampler()

        # 采样前先读取当前 logits 的 Top-K 轨迹。
        # 这一步只是观察模型“还认为哪些 token 有可能”，不会修改采样器或 logits。
        last_topk_ids, last_topk_probs = (
            self.ctx.get_top_k_logits(collect_top_k) if collect_top_k > 0 else ([], [])
        )
        last_sampled_token = sampler.sample(self.ctx.ptr)
        for _ in range(512):  # Max new tokens per chunk
            # 命中结束 token 说明该 chunk 的当前轮输出已经自然终止。
            if last_sampled_token in [self.model.eos_token, self.ID_IM_END]:
                break

            if self.ctx.decode_token(last_sampled_token) != 0:
                break

            display_queue.append(last_sampled_token)
            display_topk_queue.append((last_topk_ids, last_topk_probs))
            if len(display_queue) > rollback_num:
                # 这里实现“回滚稳定输出”：
                # 最新生成的若干 token 先留在 display_queue 中观察，
                # 只有超过 rollback_num 后，最老的 token 才被认定为稳定输出。
                # 这样可以减轻 chunk 末尾局部震荡导致的错误展示和错误记忆。
                ready_token = display_queue.popleft()
                ready_topk_ids, ready_topk_probs = display_topk_queue.popleft()
                stable_tokens.append(ready_token)
                if collect_top_k > 0:
                    # Top-K 轨迹必须和 stable_tokens 同步出队，后续热词替换才能用同一套 token 下标。
                    stable_topk_ids.append(ready_topk_ids)
                    stable_topk_probs.append(ready_topk_probs)
                piece = text_decoder.decode(self.model.token_to_bytes(ready_token))
                if piece:
                    stable_text_acc += piece

            # 当前 token 已经写入 KV Cache，下一步采样前再次读取“下一个位置”的 Top-K。
            # 这里记录的是即将采样的 token 位置，而不是刚刚解码进去的 token 位置。
            last_topk_ids, last_topk_probs = (
                self.ctx.get_top_k_logits(collect_top_k)
                if collect_top_k > 0
                else ([], [])
            )
            last_sampled_token = sampler.sample(self.ctx.ptr)
            n_gen_tokens += 1

        gen_time = time.time() - t_gen_start
        del sampler  # 释放采样器资源
        del batch

        if is_last_chunk and not result.is_aborted:
            # 只有最后一个 chunk 才把 display_queue 全部冲刷出来。
            # 非最后块保留尾部 token 的目的，是避免把边界附近尚不稳定的 token 过早并入历史记忆。
            while display_queue:
                t = display_queue.popleft()
                t_topk_ids, t_topk_probs = display_topk_queue.popleft()
                stable_tokens.append(t)
                if collect_top_k > 0:
                    # 最后一块会冲刷尾部 token，因此尾部对应的 Top-K 轨迹也必须一起进入稳定序列。
                    stable_topk_ids.append(t_topk_ids)
                    stable_topk_probs.append(t_topk_probs)
                piece = text_decoder.decode(self.model.token_to_bytes(t))
                if piece:
                    stable_text_acc += piece
            final_p = text_decoder.decode(b"", final=True)
            if final_p:
                stable_text_acc += final_p

        # 填充结果（内核输出标准化）
        result.text = stable_text_acc
        result.stable_tokens = stable_tokens
        result.topk_token_ids = stable_topk_ids
        result.topk_token_probs = stable_topk_probs
        result.t_prefill = prefill_time
        result.t_generate = gen_time
        result.n_prefill = total_len
        result.n_generate = n_gen_tokens
        return result

    def _print_stats(self, stats: dict, audio_duration: float, t_total: float):
        """打印转录过程的性能统计指标"""
        rtf = t_total / audio_duration if audio_duration > 0 else 0
        pre_speed = (
            stats["prefill_tokens"] / stats["prefill_time"]
            if stats["prefill_time"] > 0
            else 0
        )
        gen_speed = (
            stats["decode_tokens"] / stats["decode_time"]
            if stats["decode_time"] > 0
            else 0
        )

        logger.info("[QwenASR] 性能统计：RTF=%.3f，音频时长=%.2fs，总处理耗时=%.2fs", rtf, audio_duration, t_total)
        if stats.get("align_time"):
            logger.info("[QwenASR] 对齐耗时：%.3fs", stats["align_time"])
        logger.info("[QwenASR] 编码耗时：%.3fs", stats["encode_time"])
        logger.info(
            "[QwenASR] LLM 预填充：%.3fs，%s tokens，%.1f tokens/s",
            stats["prefill_time"],
            stats["prefill_tokens"],
            pre_speed,
        )
        logger.info(
            "[QwenASR] LLM 生成：%.3fs，%s tokens，%.1f tokens/s",
            stats["decode_time"],
            stats["decode_tokens"],
            gen_speed,
        )

    def transcribe(
        self,
        audio_file: str,
        language: Optional[str] = None,
        context: Optional[str] = None,
        start_second: float = 0.0,
        duration: float = 0.0,
        rollback_num: int = 5,
    ) -> TranscribeResult:
        """运行完整转录流水线 (从文件加载音频)"""
        from .audio import load_audio

        # transcribe 只是文件入口包装层，真正的分块识别逻辑全部在 asr() 中。
        audio = load_audio(audio_file, start_second=start_second, duration=duration)

        return self.asr(
            audio=audio,
            context=context or "",
            language=language,
            chunk_size_sec=self.config.chunk_size,
            memory_chunks=self.config.memory_num,
            rollback_num=rollback_num,
        )

    def asr(
        self,
        audio: np.ndarray,
        context: Optional[str],
        language: Optional[str],
        chunk_size_sec: float = 40.0,  # 可以调，官方最小0.5秒，最大1200秒 对ASR来说，有Aligner， 200秒
        memory_chunks: int = 2,
        rollback_num: int = 5,
        apply_hotwords: bool = False,
    ) -> TranscribeResult:
        """运行完整转录流水线 (三级流水线：i+1 预取, i 识别, i-1 对齐)"""
        # 语言归一化与校验
        if language:
            # 这里先把用户输入的语言别名规整为内部标准名，避免后续 prompt 和 aligner 使用不同拼写。
            language = normalize_language_name(language)
            validate_language(language)

        sr = 16000
        samples_per_chunk = int(chunk_size_sec * sr)
        total_len = len(audio)
        num_chunks = int(np.ceil(total_len / samples_per_chunk))
        total_duration = total_len / sr

        # 记忆管理 (预定义所有分片的物理边界)
        # all_segments 用来保存“每个 chunk 在原音频中的真实时间范围”和后续识别/对齐结果，
        # 这样最终整理时间戳时不会丢失原始物理坐标。
        all_segments: List[ASRS_Segment] = [
            ASRS_Segment(
                idx=i,
                audio_start=i * chunk_size_sec,
                audio_end=min((i + 1) * chunk_size_sec, total_duration),
            )
            for i in range(num_chunks)
        ]
        # asr_memory 中每个元素都是 (audio_embedding, recognized_text)。
        # 当前 chunk 解码时会把历史 embedding 与历史文本一起拼进 prompt，
        # 这就是项目里跨 chunk 上下文连续性的来源。
        asr_memory = deque(maxlen=memory_chunks)  # 存储 (embd, text)
        total_full_text = ""
        all_aligned_items: List[ForcedAlignItem] = []

        # 统计指标
        stats = {
            "prefill_time": 0.0,
            "decode_time": 0.0,
            "prefill_tokens": 0,
            "decode_tokens": 0,
            "encode_time": 0.0,
            "align_time": 0.0,
            "hotword_time": 0.0,
        }

        # all_hotword_hits 保存整段音频所有 chunk 的热词命中。
        # 当前前端不展示它，但返回给 Python 调用方可用于调试召回和误报。
        all_hotword_hits = []
        t_main_start = time.time()

        # --- 顺序同步处理循环 ---
        for i in range(num_chunks):
            # 1. 编码第 i 片段
            s, e = i * samples_per_chunk, min((i + 1) * samples_per_chunk, total_len)
            chunk_data = audio[s:e]
            if len(chunk_data) < samples_per_chunk:
                # 最后一个 chunk 常常不足 chunk_size，这里补零是为了保持编码器输入长度规则化。
                # 后续通过真实 audio_end 仍然能保留正确的物理时间范围。
                chunk_data = np.pad(
                    chunk_data, (0, samples_per_chunk - len(chunk_data))
                )

            audio_feature, enc_time = self.encoder.encode(chunk_data)
            stats["encode_time"] += enc_time
            was_last = i == num_chunks - 1

            # 2. 识别第 i 片段文字
            # prefix_text 只拼接历史“稳定文本”，不包含当前 chunk 未确认的尾部 token。
            # 这样可以避免不稳定文本被再次喂回模型，形成错误自增强。
            prefix_text = "".join([m[1] for m in asr_memory])  # 之前识别的文本
            combined_audio = np.concatenate(
                [m[0] for m in asr_memory] + [audio_feature], axis=0
            )  # 之前音频的embedding
    
            # 当前 chunk 的 prompt 里不仅有当前音频，还会串上若干历史 chunk 的音频 embedding。
            # 这是该工程减少切块断裂感的关键技巧。
            full_embd = self._build_prompt_embd(
                combined_audio, prefix_text, context, language
            )

            should_apply_hotwords = (
                apply_hotwords
                and self.hotword_radar is not None
                and self.hotword_radar.enabled
            )
            res = self._decode(
                full_embd,
                prefix_text,
                rollback_num,
                was_last,
                collect_top_k=self.config.hotword_top_k if should_apply_hotwords else 0,
            )

            if should_apply_hotwords and res.text:
                # 热词雷达只在完整 chunk 文本生成后做后处理。
                t_hotword_start = time.time()
                enhanced_text, hotword_hits = self.hotword_radar.apply(
                    text=res.text,
                    stable_tokens=res.stable_tokens,
                    topk_token_ids=res.topk_token_ids,
                    topk_token_probs=res.topk_token_probs,
                    max_lookahead=self.config.hotword_max_lookahead,
                    max_gap=self.config.hotword_max_gap,
                )
                res.text = enhanced_text
                res.hotword_hits = hotword_hits
                stats["hotword_time"] += time.time() - t_hotword_start

                # 打印热词命中详情，便于实时调试当前 chunk 的后处理结果。
                if hotword_hits:
                    logger.info("[Hotword] chunk=%s hits=%s", i, len(hotword_hits))
                    for hit in hotword_hits:
                        logger.info(
                            "[Hotword] "
                            "text=%s "
                            "span=%s->%s "
                            "score=%s "
                            "tokens=%s",
                            hit.get("text"),
                            hit.get("start_index"),
                            hit.get("end_index"),
                            hit.get("score"),
                            hit.get("token_indices"),
                        )

                for hit in hotword_hits:
                    # chunk_index 让调用方可以定位命中来自哪一个音频分片。
                    hit_with_chunk = dict(hit)
                    hit_with_chunk["chunk_index"] = i
                    all_hotword_hits.append(hit_with_chunk)

            # 更新记忆与统计
            # 这里把当前 chunk 的“音频 embedding + 最终稳定文本”一起写入记忆，
            # 供下一个 chunk 构造 prefix 时复用。
            all_segments[i].text = res.text
            asr_memory.append((audio_feature, res.text))

            total_full_text += res.text
            stats["prefill_tokens"] += res.n_prefill
            stats["prefill_time"] += res.t_prefill
            stats["decode_tokens"] += res.n_generate
            stats["decode_time"] += res.t_generate

            # 3. 对齐第 i 片段 (同步)
            if self.aligner and res.text.strip():
                t_align_start = time.time()
                # 这里的对齐输入不是整段音频，而是当前 chunk 对应的物理音频切片。
                # offset_sec 用于把 aligner 产出的相对时间还原回整段音频时间轴。
                # 当前实现采用“chunk 起点作为基准偏移”的简化策略，不做跨 chunk 动态边界修正。
                offset_sec = all_segments[i].audio_start
                s_smpl, e_smpl = (
                    int(offset_sec * sr),
                    int(all_segments[i].audio_end * sr),
                )
                audio_slice = audio[s_smpl:e_smpl]

                align_res = self.aligner.align(
                    audio_slice,
                    res.text,
                    language=language,
                    offset_sec=float(offset_sec),
                )
                all_segments[i].items = align_res.items
                all_aligned_items.extend(align_res.items)
                stats["align_time"] += time.time() - t_align_start

        # 4. 结果整理
        # 对齐项来自每个 chunk 的独立对齐结果，统一按时间排序后再打包成最终返回对象。
        all_aligned_items.sort(key=lambda x: x.start_time)
        t_total = time.time() - t_main_start
        if self.verbose:
            self._print_stats(stats, total_duration, t_total)

        return TranscribeResult(
            text=total_full_text,
            alignment=ForcedAlignResult(items=all_aligned_items)
            if all_aligned_items
            else None,
            performance=stats,
            hotword_hits=all_hotword_hits,
        )
