import sys
import os
import ctypes
from streamvox_runtime import initialize_nvidia_runtime
import struct
import time
import numpy as np
from gguf.constants import GGML_QUANT_SIZES, GGMLQuantizationType
from typing import List, Union, Optional, Tuple
from pathlib import Path
from . import logger
from .hotwords import topk_from_logits
from .llama_backend import (
    BackendSelectionSummary,
    LlmBackend,
    build_runtime_backend_load_paths,
    summarize_backend_selection,
    validate_backend_policy,
)
from companion.core.runtime_paths import NATIVE_ROOT


# =========================================================================
# Configuration
# =========================================================================
LOGS = False  # 是否在 logger 文件中记录 llama.cpp 的日志
_forward_native_logs = LOGS


def resolve_backend_bin_dir() -> Path:
    """解析当前运行时统一的 llama.cpp native DLL 目录。

    Returns:
        安装态的 ``runtime/native``；开发态仓库中的 ``backend/bin``。

    Raises:
        FileNotFoundError: native DLL 目录不存在时抛出。
    """

    if not NATIVE_ROOT.is_dir():
        raise FileNotFoundError(f"llama.cpp DLL 目录缺失：{NATIVE_ROOT}")
    return NATIVE_ROOT

# =========================================================================
# Type Definitions
# =========================================================================

llama_token = ctypes.c_int32
llama_pos = ctypes.c_int32
llama_seq_id = ctypes.c_int32


class llama_model_params(ctypes.Structure):
    _fields_ = [
        ("devices", ctypes.POINTER(ctypes.c_void_p)),
        ("tensor_buft_overrides", ctypes.POINTER(ctypes.c_void_p)),
        ("n_gpu_layers", ctypes.c_int32),
        ("split_mode", ctypes.c_int32),
        ("main_gpu", ctypes.c_int32),
        ("tensor_split", ctypes.POINTER(ctypes.c_float)),
        (
            "progress_callback",
            ctypes.CFUNCTYPE(ctypes.c_bool, ctypes.c_float, ctypes.c_void_p),
        ),
        ("progress_callback_user_data", ctypes.c_void_p),
        ("kv_overrides", ctypes.POINTER(ctypes.c_void_p)),
        ("vocab_only", ctypes.c_bool),
        ("use_mmap", ctypes.c_bool),
        ("use_direct_io", ctypes.c_bool),
        ("use_mlock", ctypes.c_bool),
        ("check_tensors", ctypes.c_bool),
        ("use_extra_bufts", ctypes.c_bool),
        ("no_host", ctypes.c_bool),
        ("no_alloc", ctypes.c_bool),
    ]


class llama_context_params(ctypes.Structure):
    _fields_ = [
        ("n_ctx", ctypes.c_uint32),
        ("n_batch", ctypes.c_uint32),
        ("n_ubatch", ctypes.c_uint32),
        ("n_seq_max", ctypes.c_uint32),
        ("n_threads", ctypes.c_int32),
        ("n_threads_batch", ctypes.c_int32),
        ("rope_scaling_type", ctypes.c_int32),
        ("pooling_type", ctypes.c_int32),
        ("attention_type", ctypes.c_int32),
        ("flash_attn_type", ctypes.c_int32),
        ("rope_freq_base", ctypes.c_float),
        ("rope_freq_scale", ctypes.c_float),
        ("yarn_ext_factor", ctypes.c_float),
        ("yarn_attn_factor", ctypes.c_float),
        ("yarn_beta_fast", ctypes.c_float),
        ("yarn_beta_slow", ctypes.c_float),
        ("yarn_orig_ctx", ctypes.c_uint32),
        ("defrag_thold", ctypes.c_float),
        ("cb_eval", ctypes.c_void_p),
        ("cb_eval_user_data", ctypes.c_void_p),
        ("type_k", ctypes.c_int32),
        ("type_v", ctypes.c_int32),
        ("abort_callback", ctypes.c_void_p),
        ("abort_callback_data", ctypes.c_void_p),
        ("embeddings", ctypes.c_bool),
        ("offload_kqv", ctypes.c_bool),
        ("no_perf", ctypes.c_bool),
        ("op_offload", ctypes.c_bool),
        ("swa_full", ctypes.c_bool),
        ("kv_unified", ctypes.c_bool),
        ("samplers", ctypes.POINTER(ctypes.c_void_p)),
        ("n_samplers", ctypes.c_size_t),
    ]


class llama_sampler_chain_params(ctypes.Structure):
    _fields_ = [
        ("no_perf", ctypes.c_bool),
    ]


class llama_logit_bias(ctypes.Structure):
    _fields_ = [
        ("token", llama_token),
        ("bias", ctypes.c_float),
    ]


class llama_batch(ctypes.Structure):
    _fields_ = [
        ("n_tokens", ctypes.c_int32),
        ("token", ctypes.POINTER(llama_token)),
        ("embd", ctypes.POINTER(ctypes.c_float)),
        ("pos", ctypes.POINTER(llama_pos)),
        ("n_seq_id", ctypes.POINTER(ctypes.c_int32)),
        ("seq_id", ctypes.POINTER(ctypes.POINTER(llama_seq_id))),
        ("logits", ctypes.POINTER(ctypes.c_int8)),
    ]


# =========================================================================
# Llama.cpp Library Bindings
# =========================================================================

# Global library references
llama = None
ggml = None
ggml_base = None
_dll_directory_handle = None
_extra_dll_directory_handles: dict[str, object] = {}
_loaded_dynamic_backend_paths: set[str] = set()
_backend_runtime_initialized = False

# Global function pointers
llama_log_set = None
llama_backend_init = None
llama_backend_free = None
llama_model_default_params = None
llama_model_load_from_file = None
llama_model_free = None
llama_model_get_vocab = None
llama_context_default_params = None
llama_init_from_model = None
llama_free = None
llama_batch_init = None
llama_batch_free = None
llama_decode = None
llama_get_logits = None
llama_get_logits_ith = None
llama_get_embeddings = None
llama_tokenize = None
llama_vocab_n_tokens = None
llama_vocab_eos = None
llama_token_to_piece = None
llama_get_memory = None
llama_memory_clear = None
llama_model_n_embd = None

# Sampler
llama_sampler_chain_default_params = None
llama_sampler_chain_init = None
llama_sampler_chain_add = None
llama_sampler_init_greedy = None
llama_sampler_init_dist = None
llama_sampler_init_temp = None
llama_sampler_init_top_k = None
llama_sampler_init_top_p = None
llama_sampler_sample = None
llama_sampler_free = None
llama_sampler_init_min_p = None
llama_sampler_init_penalties = None
llama_sampler_accept = None
_native_log_listeners = []


class _NativeLogCapture:
    """临时收集 llama.cpp 原生日志，供 backend 判定使用。"""

    def __init__(self):
        self.lines: list[str] = []
        self._closed = False

    def handle(self, message: str) -> None:
        self.lines.append(message)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.handle in _native_log_listeners:
            _native_log_listeners.remove(self.handle)


def _start_native_log_capture() -> _NativeLogCapture:
    """注册一次性原生日志收集器。"""

    capture = _NativeLogCapture()
    _native_log_listeners.append(capture.handle)
    return capture


def logger_callback(level, message, user_data):
    """
    llama.cpp 日志回调函数
    当前仓库没有直接手动调用它，但 configure_logging() 会把它注册给底层 C 库，
    因此它属于“间接使用”的桥接代码，不建议按普通未引用函数直接删除。
    level:
        2 = ERROR
        3 = WARN
        4 = INFO
        5 = DEBUG
    """
    if not message:
        return
    try:
        msg_str = message.decode("utf-8", errors="replace").strip()
        if not msg_str or msg_str in [".", "\n"]:
            return

        for listener in list(_native_log_listeners):
            listener(msg_str)
        if not _forward_native_logs:
            return
        if level == 2:
            logger.error(f"[llama.cpp] {msg_str}")
        elif level == 3:
            logger.warning(f"[llama.cpp] {msg_str}")
        elif level == 4:
            logger.info(f"[llama.cpp] {msg_str}")
        elif level >= 5:
            logger.debug(f"[llama.cpp] {msg_str}")
        else:
            logger.info(f"[llama.cpp] {msg_str}")
    except Exception:
        logger.exception("llama.cpp 日志回调出错")


def configure_logging(logs=True):
    """配置 llama.cpp 日志回调"""
    # bind_llama_lib() 初始化时会立即调用它，把 Python logger 接到 llama.cpp 日志流上。
    global _dll_directory_handle, _forward_native_logs, _log_callback_ref
    LOG_CALLBACK = ctypes.CFUNCTYPE(
        None, ctypes.c_int, ctypes.c_char_p, ctypes.c_void_p
    )
    _forward_native_logs = logs
    _log_callback_ref = LOG_CALLBACK(logger_callback)
    llama_log_set(_log_callback_ref, None)


def _prepend_dll_search_path(path: Path) -> None:
    """把目录加入当前进程的 DLL 搜索路径，并持有句柄。"""

    normalized = str(path.resolve())
    current_path_parts = [part for part in os.environ.get("PATH", "").split(os.pathsep) if part]
    if normalized not in current_path_parts:
        os.environ["PATH"] = normalized + os.pathsep + os.environ.get("PATH", "")
    if not hasattr(os, "add_dll_directory"):
        return
    if normalized in _extra_dll_directory_handles:
        return
    _extra_dll_directory_handles[normalized] = os.add_dll_directory(normalized)




def _backend_name_from_path(path: Path) -> str | None:
    """根据 backend 动态库文件名推断 backend 名称。"""

    normalized_name = path.name.lower()
    if "cpu" in normalized_name:
        return "cpu"
    if "cuda" in normalized_name:
        return "cuda"
    if "vulkan" in normalized_name:
        return "vulkan"
    return None


def _is_backend_registered(backend_name: str | None) -> bool:
    """检查目标 backend 是否已经在当前进程注册。"""

    if ggml is None or not backend_name:
        return False
    try:
        ggml_backend_reg_by_name = ggml.ggml_backend_reg_by_name
    except AttributeError:
        return False

    ggml_backend_reg_by_name.argtypes = [ctypes.c_char_p]
    ggml_backend_reg_by_name.restype = ctypes.c_void_p
    return bool(ggml_backend_reg_by_name(backend_name.encode("utf-8")))


def _load_backend_file(path: Path) -> bool:
    """按文件精确装载一个 llama.cpp backend。"""

    normalized_path = str(path.resolve())
    backend_name = _backend_name_from_path(path)
    if _is_backend_registered(backend_name):
        return True
    if normalized_path in _loaded_dynamic_backend_paths:
        return True

    try:
        ggml_backend_load = ggml.ggml_backend_load
    except AttributeError:
        logger.warning("[QwenASR] 当前 ggml.dll 不支持按文件加载 backend：%s", path.name)
        return False

    ggml_backend_load.argtypes = [ctypes.c_char_p]
    ggml_backend_load.restype = ctypes.c_void_p
    backend_reg = ggml_backend_load(normalized_path.encode("utf-8"))
    if not backend_reg:
        logger.warning("[QwenASR] llama backend 动态加载失败：%s", normalized_path)
        return False

    _loaded_dynamic_backend_paths.add(normalized_path)
    logger.info("[QwenASR] llama backend 动态加载成功：%s", path.name)
    return True


def _ensure_runtime_backends(
    lib_dir: Path,
    *,
    use_gpu: bool,
    requested_backend: LlmBackend,
) -> None:
    """只装载本次请求真正需要的 backend，避免 native 自行命中错误 GPU backend。"""

    for backend_path in build_runtime_backend_load_paths(
        lib_dir,
        use_gpu=use_gpu,
        backend=requested_backend,
    ):
        _load_backend_file(backend_path)


def bind_llama_lib(
    *,
    use_gpu: bool = False,
    requested_backend: LlmBackend = "auto",
):
    """绑定 llama.cpp 库的 api"""
    # 这是本文件最核心的入口之一：
    # 作用不是“推理”，而是把动态库符号声明成可在 Python 里直接调用的 ctypes 函数。
    # 后面所有 LlamaModel / LlamaContext / LlamaSampler 的高级封装，最终都依赖这里完成绑定。
    global llama, ggml, ggml_base
    global llama_log_set, llama_backend_init, llama_backend_free
    global \
        llama_model_default_params, \
        llama_model_load_from_file, \
        llama_model_free, \
        llama_model_get_vocab
    global llama_context_default_params, llama_init_from_model, llama_free
    global llama_batch_init, llama_batch_free, llama_batch_get_one
    global \
        llama_decode, \
        llama_get_logits, \
        llama_get_logits_ith, \
        llama_get_embeddings, \
        llama_tokenize
    global llama_get_memory, llama_memory_clear, llama_model_n_embd
    global llama_vocab_n_tokens, llama_vocab_eos, llama_token_to_piece
    global \
        llama_sampler_chain_default_params, \
        llama_sampler_chain_init, \
        llama_sampler_chain_add
    global llama_sampler_init_greedy, llama_sampler_init_dist, llama_sampler_init_temp
    global \
        llama_sampler_init_top_k, \
        llama_sampler_init_top_p, \
        llama_sampler_sample, \
        llama_sampler_free
    global llama_sampler_init_min_p, llama_sampler_init_penalties, llama_sampler_accept
    global llama_sampler_init_logit_bias
    global _log_callback_ref, _backend_runtime_initialized

    # rehome 后 native DLL 统一放在 RuntimeLayout 的 native 目录，避免推理包内部夹带大体积二进制。
    lib_dir = resolve_backend_bin_dir()
    if use_gpu and requested_backend in {"cuda", "auto"}:
        initialize_nvidia_runtime(consumer="llama", required=requested_backend == "cuda")

    if llama is not None:
        _ensure_runtime_backends(
            lib_dir,
            use_gpu=use_gpu,
            requested_backend=requested_backend,
        )
        if not _backend_runtime_initialized:
            llama_backend_init()
            _backend_runtime_initialized = True
        return

    # DLL 命名处理
    if sys.platform == "win32":
        GGML_DLL = "ggml.dll"
        GGML_BASE_DLL = "ggml-base.dll"
        LLAMA_DLL = "llama.dll"
    elif sys.platform == "darwin":
        GGML_DLL = "libggml.dylib"
        GGML_BASE_DLL = "libggml-base.dylib"
        LLAMA_DLL = "libllama.dylib"
    else:
        GGML_DLL = "libggml.so"
        GGML_BASE_DLL = "libggml-base.so"
        LLAMA_DLL = "libllama.so"

    # ctypes.CDLL 接收字符串路径最稳定；这里显式转成 str，兼容 Python 版本和 Windows 路径对象差异。
    ggml = ctypes.CDLL(str(lib_dir / GGML_DLL))
    ggml_base = ctypes.CDLL(str(lib_dir / GGML_BASE_DLL))
    llama = ctypes.CDLL(str(lib_dir / LLAMA_DLL))

    # 设置日志回调
    LOG_CALLBACK = ctypes.CFUNCTYPE(
        None, ctypes.c_int, ctypes.c_char_p, ctypes.c_void_p
    )
    llama_log_set = llama.llama_log_set
    llama_log_set.argtypes = [LOG_CALLBACK, ctypes.c_void_p]
    llama_log_set.restype = None
    configure_logging(logs=LOGS)

    # 这里不再无差别 load_all，而是按请求精确装载 backend。
    # 否则当机器同时具备 Vulkan/CUDA 时，native 可能静默选错 backend。
    _ensure_runtime_backends(
        lib_dir,
        use_gpu=use_gpu,
        requested_backend=requested_backend,
    )

    llama_backend_init = llama.llama_backend_init
    llama_backend_init.argtypes = []
    llama_backend_init.restype = None
    llama_backend_init()
    _backend_runtime_initialized = True

    # 绑定其他函数
    # 这里有一部分底层符号当前仓库没有直接走到，
    # 但保留它们的价值在于：后续扩展时不必重新补 ctypes 声明。
    llama_backend_free = llama.llama_backend_free
    llama_backend_free.argtypes = []
    llama_backend_free.restype = None

    llama_model_default_params = llama.llama_model_default_params
    llama_model_default_params.argtypes = []
    llama_model_default_params.restype = llama_model_params

    llama_model_load_from_file = llama.llama_model_load_from_file
    llama_model_load_from_file.argtypes = [ctypes.c_char_p, llama_model_params]
    llama_model_load_from_file.restype = ctypes.c_void_p

    llama_model_free = llama.llama_model_free
    llama_model_free.argtypes = [ctypes.c_void_p]
    llama_model_free.restype = None

    llama_model_get_vocab = llama.llama_model_get_vocab
    llama_model_get_vocab.argtypes = [ctypes.c_void_p]
    llama_model_get_vocab.restype = ctypes.c_void_p

    llama_model_n_embd = llama.llama_model_n_embd
    llama_model_n_embd.argtypes = [ctypes.c_void_p]
    llama_model_n_embd.restype = ctypes.c_int32

    # Context
    llama_context_default_params = llama.llama_context_default_params
    llama_context_default_params.argtypes = []
    llama_context_default_params.restype = llama_context_params

    llama_init_from_model = llama.llama_init_from_model
    llama_init_from_model.argtypes = [ctypes.c_void_p, llama_context_params]
    llama_init_from_model.restype = ctypes.c_void_p

    llama_free = llama.llama_free
    llama_free.argtypes = [ctypes.c_void_p]
    llama_free.restype = None

    # Batch
    llama_batch_init = llama.llama_batch_init
    llama_batch_init.argtypes = [ctypes.c_int32, ctypes.c_int32, ctypes.c_int32]
    llama_batch_init.restype = llama_batch

    llama_batch_free = llama.llama_batch_free
    llama_batch_free.argtypes = [llama_batch]
    llama_batch_free.restype = None

    llama_batch_get_one = llama.llama_batch_get_one
    llama_batch_get_one.argtypes = [ctypes.POINTER(llama_token), ctypes.c_int32]
    llama_batch_get_one.restype = llama_batch

    # Decode
    llama_decode = llama.llama_decode
    llama_decode.argtypes = [ctypes.c_void_p, llama_batch]
    llama_decode.restype = ctypes.c_int32

    # Logits
    llama_get_logits = llama.llama_get_logits
    llama_get_logits.argtypes = [ctypes.c_void_p]
    llama_get_logits.restype = ctypes.POINTER(ctypes.c_float)

    llama_get_logits_ith = llama.llama_get_logits_ith
    llama_get_logits_ith.argtypes = [ctypes.c_void_p, ctypes.c_int32]
    llama_get_logits_ith.restype = ctypes.POINTER(ctypes.c_float)

    llama_get_embeddings = llama.llama_get_embeddings
    llama_get_embeddings.argtypes = [ctypes.c_void_p]
    llama_get_embeddings.restype = ctypes.POINTER(ctypes.c_float)

    # Tokenize
    llama_tokenize = llama.llama_tokenize
    llama_tokenize.argtypes = [
        ctypes.c_void_p,
        ctypes.c_char_p,
        ctypes.c_int32,
        ctypes.POINTER(llama_token),
        ctypes.c_int32,
        ctypes.c_bool,
        ctypes.c_bool,
    ]
    llama_tokenize.restype = ctypes.c_int32

    # Vocab
    llama_vocab_n_tokens = llama.llama_vocab_n_tokens
    llama_vocab_n_tokens.argtypes = [ctypes.c_void_p]
    llama_vocab_n_tokens.restype = ctypes.c_int32

    llama_vocab_eos = llama.llama_vocab_eos
    llama_vocab_eos.argtypes = [ctypes.c_void_p]
    llama_vocab_eos.restype = llama_token

    llama_token_to_piece = llama.llama_token_to_piece
    llama_token_to_piece.argtypes = [
        ctypes.c_void_p,
        llama_token,
        ctypes.c_char_p,
        ctypes.c_int32,
        ctypes.c_int32,
        ctypes.c_bool,
    ]
    llama_token_to_piece.restype = ctypes.c_int

    # Memory (KV Cache)
    llama_get_memory = llama.llama_get_memory
    llama_get_memory.argtypes = [ctypes.c_void_p]
    llama_get_memory.restype = ctypes.c_void_p

    llama_memory_clear = llama.llama_memory_clear
    llama_memory_clear.argtypes = [ctypes.c_void_p, ctypes.c_bool]
    llama_memory_clear.restype = None

    # Sampler
    llama_sampler_chain_default_params = llama.llama_sampler_chain_default_params
    llama_sampler_chain_default_params.argtypes = []
    llama_sampler_chain_default_params.restype = llama_sampler_chain_params

    llama_sampler_chain_init = llama.llama_sampler_chain_init
    llama_sampler_chain_init.argtypes = [llama_sampler_chain_params]
    llama_sampler_chain_init.restype = ctypes.c_void_p

    llama_sampler_chain_add = llama.llama_sampler_chain_add
    llama_sampler_chain_add.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    llama_sampler_chain_add.restype = None

    llama_sampler_init_greedy = llama.llama_sampler_init_greedy
    llama_sampler_init_greedy.argtypes = []
    llama_sampler_init_greedy.restype = ctypes.c_void_p

    llama_sampler_init_dist = llama.llama_sampler_init_dist
    llama_sampler_init_dist.argtypes = [ctypes.c_uint32]
    llama_sampler_init_dist.restype = ctypes.c_void_p

    llama_sampler_init_temp = llama.llama_sampler_init_temp
    llama_sampler_init_temp.argtypes = [ctypes.c_float]
    llama_sampler_init_temp.restype = ctypes.c_void_p

    llama_sampler_init_top_k = llama.llama_sampler_init_top_k
    llama_sampler_init_top_k.argtypes = [ctypes.c_int32]
    llama_sampler_init_top_k.restype = ctypes.c_void_p

    llama_sampler_init_top_p = llama.llama_sampler_init_top_p
    llama_sampler_init_top_p.argtypes = [ctypes.c_float, ctypes.c_size_t]
    llama_sampler_init_top_p.restype = ctypes.c_void_p

    llama_sampler_sample = llama.llama_sampler_sample
    llama_sampler_sample.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int32]
    llama_sampler_sample.restype = llama_token

    llama_sampler_free = llama.llama_sampler_free
    llama_sampler_free.argtypes = [ctypes.c_void_p]
    llama_sampler_free.restype = None

    llama_sampler_init_logit_bias = llama.llama_sampler_init_logit_bias
    llama_sampler_init_logit_bias.argtypes = [
        ctypes.c_int32,
        ctypes.c_int32,
        ctypes.POINTER(llama_logit_bias),
    ]
    llama_sampler_init_logit_bias.restype = ctypes.c_void_p

    llama_sampler_init_min_p = llama.llama_sampler_init_min_p
    llama_sampler_init_min_p.argtypes = [ctypes.c_float, ctypes.c_size_t]
    llama_sampler_init_min_p.restype = ctypes.c_void_p

    llama_sampler_init_penalties = llama.llama_sampler_init_penalties
    llama_sampler_init_penalties.argtypes = [
        ctypes.c_int32,
        ctypes.c_float,
        ctypes.c_float,
        ctypes.c_float,
    ]
    llama_sampler_init_penalties.restype = ctypes.c_void_p

    llama_sampler_accept = llama.llama_sampler_accept
    llama_sampler_accept.argtypes = [ctypes.c_void_p, llama_token]
    llama_sampler_accept.restype = None


def init(
    *,
    use_gpu: bool = False,
    requested_backend: LlmBackend = "auto",
):
    """
    切换目录，初始化 llama.cpp lib
    """
    # 这个初始化在 import 阶段就会自动执行。
    # 当前 ASR / Aligner 都依赖这种“模块导入即完成动态库绑定”的行为。
    global _dll_directory_handle

    original_cwd = Path.cwd()
    # 所有 llama.cpp DLL 已复制到 RuntimeLayout 的 native 目录，初始化时只临时切到该目录，方便底层依赖互相解析。
    lib_dir = resolve_backend_bin_dir()

    # 跳转到 dll 所在目录，并将其加到 DLL 搜索路径；finally 只负责恢复 cwd，DLL 搜索状态需保持到进程结束。
    try:
        os.chdir(lib_dir)
        lib_dir_str = str(lib_dir)
        _prepend_dll_search_path(lib_dir)
        if hasattr(os, "add_dll_directory") and _dll_directory_handle is None:
            # handle 必须保留引用，否则 Windows 可能提前移除该 DLL 搜索目录。
            _dll_directory_handle = os.add_dll_directory(lib_dir_str)
        logger.info(f"初始化 llama.cpp，跳转至：{Path.cwd()}")

        # 绑定 llama api
        bind_llama_lib(
            use_gpu=use_gpu,
            requested_backend=requested_backend,
        )
    finally:
        # 跳回到原来目录，避免一次 ASR 初始化污染后续 FastAPI 或测试进程的相对路径解析。
        os.chdir(original_cwd)
        logger.info(f"恢复至目录：{Path.cwd()}")

    return True


init(use_gpu=False, requested_backend="auto")


# =========================================================================
# Llama.cpp 高级 API
# =========================================================================


class LlamaModel:
    """
    作用： 负责从硬盘加载 .gguf 模型文件，并掌管词表（Vocabulary）。它本身不参与对话生成，只提供静态的“知识”。
    交互方法： 传入路径加载模型；使用 .tokenize() 把文本变成 Token ID，用 .detokenize() 把 Token ID 变回文本。
    """

    def __init__(
        self,
        path,
        n_gpu_layers=-1,
        use_gpu=1,
        backend: LlmBackend = "auto",
    ):
        # 当前仓库的 asr.py / aligner.py 都通过这个类加载解码器 GGUF，
        self.use_gpu = bool(use_gpu)
        self.requested_backend = validate_backend_policy(backend)
        self.actual_backend: str | None = None
        self.backend_summary: BackendSelectionSummary | None = None
        self._backend_log_capture = _start_native_log_capture()
        self.ptr = self.load_model(path, n_gpu_layers=n_gpu_layers, use_gpu=use_gpu)

        self.vocab = llama_model_get_vocab(self.ptr)
        self.n_embd = llama_model_n_embd(self.ptr)
        self.eos_token = llama_vocab_eos(self.vocab)

    def load_model(self, model_path: str, n_gpu_layers: int = -1, use_gpu: bool = 0):
        """
        加载 GGUF 模型（自动处理初始化和路径编码）

        Args:
            model_path: GGUF 模型文件路径
            n_gpu_layers: 卸载到 GPU 的层数 (-1 表示全部)
            use_gpu: 是否启用 GPU (如果为 False，则强制使用 CPU)

        Returns:
            model: llama_model 指针
        """
        # 这里故意把 Path 统一转成 utf-8 字节路径，
        # 是为了兼容中文路径/跨平台路径，不把路径处理细节泄露给上层调用方。
        model_path = Path(model_path)
        init(
            use_gpu=bool(use_gpu),
            requested_backend=self.requested_backend,
        )

        model_params = llama_model_default_params()
        model_params.n_gpu_layers = n_gpu_layers

        if not use_gpu:
            model_params.devices = (ctypes.c_void_p * 1)(None)

        model = llama_model_load_from_file(
            model_path.as_posix().encode("utf-8"), model_params
        )

        if model:
            return model
        else:
            logger.error(f"模型加载失败: {model_path}")
            return None

    def finalize_backend_runtime_check(self) -> BackendSelectionSummary:
        """在上下文初始化后核对实际 backend 是否符合请求。"""

        if self.backend_summary is not None:
            return self.backend_summary

        capture = getattr(self, "_backend_log_capture", None)
        log_lines = tuple(capture.lines) if capture is not None else ()
        if capture is not None:
            capture.close()
            self._backend_log_capture = None

        summary = summarize_backend_selection(
            requested_backend=self.requested_backend,
            use_gpu=self.use_gpu,
            log_lines=log_lines,
        )
        self.backend_summary = summary
        self.actual_backend = summary.actual_backend
        logger.info(
            "[QwenASR] llama backend 判定：requested=%s use_gpu=%s actual=%s",
            summary.requested_backend,
            summary.use_gpu,
            summary.actual_backend,
        )
        # for line in summary.evidence:
        #     logger.info("[QwenASR] llama backend 证据：%s", line)
        if not summary.is_success:
            logger.error("[QwenASR] llama backend 判定失败：%s", summary.failure_reason)
            raise RuntimeError(
                "Qwen ASR LLM backend 初始化失败："
                f"{summary.failure_reason}"
            )
        return summary

    def tokenize(
        self, text: str, add_special: bool = False, parse_special: bool = True
    ) -> List[int]:
        """(Native) 文本转 Token ID 列表"""
        # 当前 asr.py / aligner.py 都会用它把特殊 token 和普通文本转成 token ids。
        return text_to_tokens(self.vocab, text, add_special, parse_special)

    def detokenize(self, tokens: List[int]) -> str:
        """(Native) Token ID 列表转文本"""
        # 它主要是 tokenize 的反向辅助接口，便于调试或未来扩展。
        if tokens is None or len(tokens) == 0:
            return ""
        all_bytes = b"".join([self.token_to_bytes(tid) for tid in tokens])
        return all_bytes.decode("utf-8", errors="replace")

    def token_to_bytes(self, token_id: int) -> bytes:
        """(Native) 单个 Token 转字节"""
        # 当前 asr.py 的流式解码环节会用它做增量 utf-8 解码，
        # 所以它不是“工具函数边角料”，而是生成阶段文本拼接的一环。
        return token_to_bytes(self.vocab, token_id)

    def token_to_piece(self, token_id: int) -> str:
        """(Native) 单个 Token 转字符串 Piece"""
        # 它和 token_to_bytes 的区别是这里直接返回字符串，适合调试单 token 可读形式。
        return self.token_to_bytes(token_id).decode("utf-8", errors="replace")

    def token_eos(self) -> int:
        # 当前仓库未检索到直接调用 token_eos() 方法，
        # 但 __init__ 已经直接读取 eos_token 并在生成循环里使用，所以 eos 相关逻辑本身是有用的。
        return llama_vocab_eos(self.vocab)

    def token_to_id(self, text: str) -> int:
        """(Native) 单个 Token 字符串转 ID (仅限 Exact Match)"""
        # 当前 asr.py / aligner.py 会在初始化阶段大量调用它，
        # 用来缓存 <|audio_start|>、<|im_end|> 等特殊 token 的 id。
        # 这里复用 tokenize 而不单独查词表，是为了让分词规则完全以 llama.cpp 为准。
        res = self.tokenize(text, add_special=False, parse_special=True)
        return res[0] if res else -1

    def __del__(self):
        capture = getattr(self, "_backend_log_capture", None)
        if capture is not None:
            capture.close()
        if hasattr(self, "ptr") and self.ptr:
            llama_model_free(self.ptr)
            self.ptr = None


class LlamaContext:
    """
    作用： 负责管理运行时的状态，比如我们常说的 KV Cache（记住之前的对话上下文）。所有的计算（推理）都在这里发生。
    交互方法： 调用 .decode(batch) 将输入送入模型计算，调用 .get_logits() 获取模型预测下一个词的概率。
    """

    def __init__(
        self,
        model,
        n_ctx=2048,
        n_batch=2048,
        n_ubatch=512,
        n_seq_max=1,
        embeddings=False,
        pooling_type=0,
        flash_attn=True,
        offload_kqv=True,
        no_perf=True,
        n_threads=None,
        n_threads_batch=None,
    ):
        """
        初始化单个 llama 推理上下文，并写入上下文窗口、批大小、线程数等运行参数。

        Args:
            model (LlamaModel): 已加载的模型对象，内部会读取其原生指针并与当前上下文绑定。
            n_ctx (int, optional): 上下文窗口大小，决定 KV Cache 可保留的最大 token 数。默认值为 2048。
            n_batch (int, optional): 单次送入解码器的最大批大小，影响吞吐与显存占用。默认值为 2048。
            n_ubatch (int, optional): 内部分块使用的微批大小，用于控制一次底层计算切分的粒度。默认值为 512。
            n_seq_max (int, optional): 上下文中允许同时维护的序列数量，适用于多序列并行场景。默认值为 1。
            embeddings (bool, optional): 是否启用向量输出模式；开启后可从上下文中提取 embedding。默认值为 False。
            pooling_type (int, optional): embedding 池化策略的原生枚举值，仅在启用 embeddings 时生效。默认值为 0。
            flash_attn (bool, optional): 是否启用 Flash Attention 加速，以换取更高的注意力计算效率。默认值为 True。
            offload_kqv (bool, optional): 是否将注意力中的 K/Q/V 相关计算尽量卸载到加速设备。默认值为 True。
            no_perf (bool, optional): 是否关闭性能统计采样，适合减少额外的运行时开销。默认值为 True。
            n_threads (int | None, optional): 解码阶段使用的 CPU 线程数；为 None 时自动按 CPU 核心数推导。默认值为 None。
            n_threads_batch (int | None, optional): 批处理路径使用的 CPU 线程数；为 None 时优先跟随 `n_threads`，否则回退到 CPU 核心数。默认值为 None。

        Raises:
            RuntimeError: 当底层 `llama_init_from_model` 返回空指针时，说明原生上下文初始化失败。
        """
        # 保持模型引用，避免 Python 提前回收 model，导致底层 context 持有悬空指针。
        self.model = model
        params = llama_context_default_params()
        params.n_ctx = n_ctx
        params.n_batch = n_batch
        params.n_ubatch = n_ubatch
        params.n_seq_max = n_seq_max
        params.embeddings = embeddings
        params.pooling_type = pooling_type
        params.flash_attn_type = 1 if flash_attn else 0
        params.offload_kqv = offload_kqv
        params.no_perf = no_perf

        # 线程配置
        # 这里把线程数控制在一个相对保守的范围，
        # 目的是避免默认把全部 CPU 打满，影响多进程/多模块同时运行时的整体吞吐。
        cpu_count = min(os.cpu_count(), 25) or 4
        if n_threads:
            params.n_threads = n_threads
        else:
            params.n_threads = cpu_count // 2

        if n_threads_batch:
            params.n_threads_batch = n_threads_batch
        else:
            params.n_threads_batch = n_threads if n_threads else cpu_count

        self.ptr = llama_init_from_model(model.ptr, params)
        if hasattr(model, "finalize_backend_runtime_check"):
            model.finalize_backend_runtime_check()
        if not self.ptr:
            raise RuntimeError("上下文初始化失败")

        # 预分配单 token decode 所需的持久缓冲。
        # 其中 token_id 会先写入一个局部 ctypes 数组，再把指针交给 llama_batch_get_one()。
        # 这种写法在直接运行时通常“看起来可用”，但在 debugger 单步、对象生命周期变化或
        # ctypes 临时对象更早释放时，底层可能读到失效指针，进而让 llama_decode 返回非零错误码。
        # 新逻辑把单 token 缓冲挂到 Context 实例上，保证指针在整个 Context 生命周期内稳定有效。
        self._single_token_arr = (llama_token * 1)()
        self._single_token_batch = llama_batch_get_one(self._single_token_arr, 1)

    def decode(self, batch):
        # 这是 prefill 和 embedding 注入的统一入口。
        # asr.py / aligner.py 会把一整段 embedding 序列打进来，完成首轮上下文建立。
        struct = batch.struct if hasattr(batch, "struct") else batch
        return llama_decode(self.ptr, struct)

    def decode_token(self, token_id):
        """
        原子操作：设置单 Token Batch 并执行解码
        """
        # 当前 asr.py 的逐 token 生成循环会直接调用它，
        # 目的是复用 get_one_batch() 的轻量路径，减少每步生成时的 Python 侧分配开销。
        self._single_token_arr[0] = token_id
        return self.decode(self._single_token_batch)

    def get_logits(self):
        """获取 Batch 中最后一个启用 Logits 的 Token 的输出"""
        # 如果未来只关心“最后一个 token 的 logits”，这个接口比 get_logits_ith() 更直接。
        return llama_get_logits(self.ptr)

    def get_logits_ith(self, i: int):
        """获取 Batch 中第 i 个 Token 的 Logits 输出 (前提是该 Token 启用了 Logits 标志)"""
        # 但 LlamaSampler.sample() 在做 logits 范围裁剪时会通过它拿到指定位置的输出。
        return llama_get_logits_ith(self.ptr, i)

    def get_top_k_logits(self, top_k: int, idx: int = -1) -> Tuple[List[int], List[float]]:
        """只读提取指定输出位置的 Top-K token 和相对概率。

        top_k 控制观察候选数量，idx 与 llama.cpp 的 logits 位置语义一致，默认读取最后一个可用输出。
        返回值是两个等长列表，分别是 token id 和 Top-K 内部归一化概率。
        该方法只读取底层 logits 指针，不修改 logits，也不调用采样器，因此不会改变解码结果。
        """
        if top_k <= 0:
            return [], []

        # n_vocab 是当前 GGUF 模型词表大小，用于把 ctypes 指针安全映射成 numpy 视图。
        n_vocab = llama_vocab_n_tokens(self.model.vocab)
        logits_ptr = self.get_logits_ith(idx)
        logits = np.ctypeslib.as_array(logits_ptr, shape=(n_vocab,))
        return topk_from_logits(logits, top_k)

    def get_embeddings(self):
        # 它更偏向 embedding 模式/调试模式的预留接口。
        return llama_get_embeddings(self.ptr)

    def clear_kv_cache(self):
        # 当前 asr.py / aligner.py 的每轮 prefill 前都会先清空 KV Cache，
        # 这样可以确保每个 chunk 从“干净上下文”开始，避免旧缓存污染当前结果。
        mem = llama_get_memory(self.ptr)
        llama_memory_clear(mem, True)

    def __del__(self):
        if hasattr(self, "ptr") and self.ptr:
            llama_free(self.ptr)
            self.ptr = None


class LlamaBatch:
    """Batch 的面向对象封装，支持直接属性访问"""

    def __init__(self, n_tokens, embd_dim=0, n_seq_max=1):
        # 当前仓库会用它承载“已经拼好的 embedding 序列”，
        # 所以这里不是传统 token batch，而是多模态输入的物理容器。
        self.struct = llama_batch_init(n_tokens, embd_dim, n_seq_max)
        self.n_tokens_max = n_tokens

    @property
    def n_tokens(self):
        return self.struct.n_tokens

    @n_tokens.setter
    def n_tokens(self, val):
        self.struct.n_tokens = val

    @property
    def token(self):
        return self.struct.token

    @property
    def embd(self):
        return self.struct.embd

    @property
    def pos(self):
        return self.struct.pos

    @property
    def n_seq_id(self):
        return self.struct.n_seq_id

    @property
    def seq_id(self):
        return self.struct.seq_id

    @property
    def logits(self):
        return self.struct.logits

    def set_embd(
        self, data: np.ndarray, pos: Union[np.ndarray, int] = 0, seq_id: int = 0
    ):
        """
        高阶接口：直接注入 Embedding 数据并初始化位置信息

        Args:
            data: Embedding 数据 [n_tokens, dim]
            pos: 位置信息。
                 - 若为 int，则视为起始偏移量，自动生成 [offset, offset+1, ...]
                 - 若为 np.ndarray，则直接拷贝到 pos buffer (支持 Qwen3 等复杂位置编码)
            seq_id: 序列 ID
        """
        # 当前 ASR 链路的关键点就在这里：
        # 上层先把“文本 token 对应的 embedding”和“音频 encoder 输出的 embedding”拼成一条连续序列，
        # 再通过本函数一次性灌进 llama.cpp，绕过纯 token 输入模式。
        n_tokens = data.shape[0]
        if n_tokens > self.n_tokens_max:
            raise ValueError(f"Batch 空间不足: {n_tokens} > {self.n_tokens_max}")

        # 1. 内存移动 (Embedding)
        if not data.flags["C_CONTIGUOUS"]:
            data = np.ascontiguousarray(data)
        ctypes.memmove(self.embd, data.ctypes.data, data.nbytes)

        # 2. 位置信息处理 (Position)
        if isinstance(pos, int):
            # 自动生成线性位置
            pos_offset = pos
            for i in range(n_tokens):
                self.pos[i] = pos_offset + i
        elif isinstance(pos, np.ndarray):
            # 外部提供的复杂位置 (如 Qwen3 的多平面位置)
            # 注意：不检查 pos 长度是否等于 n_tokens，因为可能有 stride (Qwen3 case)
            # 但必须确保不超过 batch capacity
            if not pos.flags["C_CONTIGUOUS"]:
                pos = np.ascontiguousarray(pos)

            # 使用 memmove 直接拷贝
            # self.pos 是 ctypes 指针，可以直接操作
            ctypes.memmove(self.pos, pos.ctypes.data, pos.nbytes)
        else:
            raise TypeError(f"Unsupported pos type: {type(pos)}")

        # 3. 设置其他元数据
        self.n_tokens = n_tokens
        for i in range(n_tokens):
            self.n_seq_id[i] = 1
            self.seq_id[i][0] = seq_id
            self.logits[i] = 1 if i == n_tokens - 1 else 0

        return self

    def __del__(self):
        if hasattr(self, "struct"):
            llama_batch_free(self.struct)


def get_one_batch(token_id: int):
    """
    底层极限优化：用于单 Token 生成的无分配 Batch 构造。
    相当于 C++ 的 llama_batch_get_one(&token, 1)。
    它不依赖 llama_batch_init 的内存分配，并允许底层自动推断 pos。
    """
    # 这条路径存在的意义，是把生成阶段每一步的 Python 开销压到最低。
    token_arr = (llama_token * 1)(token_id)
    return llama_batch_get_one(token_arr, 1)


class LlamaSampler:
    """采样器的面向对象封装"""

    def __init__(
        self,
        temperature: float = 0.0,
        top_k: int = 50,
        top_p: float = 1.0,
        min_p: float = 0.0,
        repeat_penalty: float = 1.0,
        frequency_penalty: float = 0.0,
        presence_penalty: float = 0.0,
        penalty_last_n: int = 64,
        seed: Optional[int] = None,
        logit_bias: Optional[dict] = None,
        n_vocab: int = 0,
    ):
        import time

        # 当前 asr.py 的生成循环会直接实例化采样器。
        # ASR 默认需要稳定、可复现的文本，因此默认 temperature=0.0 走贪心解码；
        # 调用方只有在明确需要非确定性生成时，才应显式传入非零 temperature。
        if seed is None:
            seed = int(time.time())

        sparams = llama_sampler_chain_default_params()
        self.ptr = llama_sampler_chain_init(sparams)

        # 1. Logit Bias (最高优先级)
        if logit_bias and n_vocab > 0 and isinstance(logit_bias, dict):
            n_bias = len(logit_bias)
            BiasArray = llama_logit_bias * n_bias
            bias_data = BiasArray()
            for i, (token, bias) in enumerate(logit_bias.items()):
                bias_data[i].token = token
                bias_data[i].bias = bias
            llama_sampler_chain_add(
                self.ptr, llama_sampler_init_logit_bias(n_vocab, n_bias, bias_data)
            )

        # 2. Penalties (重复/频率/存在惩罚)
        has_penalty = (
            repeat_penalty != 1.0 or frequency_penalty != 0.0 or presence_penalty != 0.0
        )
        if has_penalty:
            # llama.cpp 会自动管理历史 rings
            llama_sampler_chain_add(
                self.ptr,
                llama_sampler_init_penalties(
                    penalty_last_n, repeat_penalty, frequency_penalty, presence_penalty
                ),
            )

        # 3. 采样过滤器 (顺序很重要)
        if temperature > 0:
            if top_k > 0:
                llama_sampler_chain_add(self.ptr, llama_sampler_init_top_k(top_k))
            if top_p < 1.0:
                llama_sampler_chain_add(self.ptr, llama_sampler_init_top_p(top_p, 1))
            if 0.0 < min_p < 1.0:
                llama_sampler_chain_add(self.ptr, llama_sampler_init_min_p(min_p, 1))

            llama_sampler_chain_add(self.ptr, llama_sampler_init_temp(temperature))
            llama_sampler_chain_add(self.ptr, llama_sampler_init_dist(seed))
        else:
            llama_sampler_chain_add(self.ptr, llama_sampler_init_greedy())

        self._neg_inf = -1e10

    def accept(self, token_id: int):
        """同步 Token 到采样链历史 (native)"""
        # 因为 llama_sampler_sample() 默认已经会自动 accept，只有手工控制采样状态时才需要显式调用。
        if self.ptr:
            llama_sampler_accept(self.ptr, token_id)

    def sample(self, ctx, idx=-1, limit_start=None, limit_end=None, allow_tokens=None):
        """采样一个 Token，支持范围限制和白名单豁免"""
        # 当前 asr.py 的逐 token 生成每一步都会调用这里。
        # 这个接口一方面包装 llama.cpp 的原生采样链，另一方面允许在 Python 侧临时做 logits 掩码控制。
        ctx_ptr = ctx.ptr if hasattr(ctx, "ptr") else ctx

        # 处理 Logits 约束 (Range Limit + Allow-list)
        if (limit_start is not None or limit_end is not None) and hasattr(
            ctx, "get_logits"
        ):
            n_vocab = llama_vocab_n_tokens(ctx.model.vocab)
            logits_ptr = ctx.get_logits_ith(idx)  # 获取指定索引的 logits
            logits = np.ctypeslib.as_array(logits_ptr, shape=(n_vocab,))

            s = max(0, limit_start) if limit_start is not None else 0
            e = min(n_vocab, limit_end) if limit_end is not None else n_vocab

            # 创建掩码：默认全灭，然后开启指定范围和白名单
            mask = np.ones(n_vocab, dtype=bool)
            mask[s:e] = False  # 范围内的不抹除
            if allow_tokens:
                for t in allow_tokens:
                    if 0 <= t < n_vocab:
                        mask[t] = False  # 白名单内地不抹除

            logits[mask] = self._neg_inf

        # 使用原生 Chain 进行采样。
        # 注意：llama_sampler_sample 会自动对 Chain 调用 accept()。
        return llama_sampler_sample(self.ptr, ctx_ptr, idx)

    def free(self):
        """释放采样器资源"""
        if hasattr(self, "ptr") and self.ptr:
            llama_sampler_free(self.ptr)
            self.ptr = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.free()

    def __del__(self):
        self.free()


# =========================================================================
# Embedding Table
# =========================================================================


class LlamaEmbeddingTable:
    """动态反量化 Embedding 表，支持 table[ids] 语法"""

    def __init__(self, raw_data, qtype):
        self.raw_data = raw_data
        self.qtype = qtype

    def __len__(self):
        # 保留它主要是为了让这个对象更像标准 table / ndarray 包装器。
        return self.raw_data.shape[0]

    def __getitem__(self, tokens):
        from gguf.quants import dequantize

        # 当前 asr.py / aligner.py 会频繁用 table[token_ids] 取文本 embedding。
        # 这里做“按需反量化”，是为了避免在启动阶段把整个 embedding 矩阵一次性解压到内存里。
        # 如果是原生 float 类型，直接返回
        if self.raw_data.dtype in (np.float32, np.float16):
            return self.raw_data[tokens].astype(np.float32)

        # 调用官方库进行高性能反量化
        return dequantize(self.raw_data[tokens], self.qtype.value)


def _skip_gguf_value(mm, offs, v_type):
    # 这是 get_token_embeddings_gguf() 的内部辅助函数。
    # 当前仓库没有外部直接引用它，但删除后会直接破坏 GGUF 头部的快速扫描逻辑。
    # UINT8=0, INT8=1, UINT16=2, INT16=3, UINT32=4, INT32=5, FLOAT32=6, BOOL=7, STRING=8, ARRAY=9, UINT64=10, INT64=11, FLOAT64=12
    fixed = [1, 1, 2, 2, 4, 4, 4, 1, -1, -2, 8, 8, 8]
    val_len = fixed[v_type]
    if val_len > 0:
        return offs + val_len
    elif val_len == -1:  # string
        slen = struct.unpack_from("<Q", mm, offs)[0]
        return offs + 8 + slen
    elif val_len == -2:  # array
        itype, alen = struct.unpack_from("<IQ", mm, offs)
        offs += 12
        if itype == 8:  # string array
            for _ in range(alen):
                slen = struct.unpack_from("<Q", mm, offs)[0]
                offs += 8 + slen
        else:
            item_len = fixed[itype]
            if item_len > 0:
                offs += item_len * alen
            else:
                raise ValueError(
                    "Nested arrays or unknown type not supported in fast skip"
                )
        return offs


def get_token_embeddings_gguf(model_path, target_tensor="token_embd.weight"):
    """
    超极速 GGUF Embedding 提取 (直接二进制寻址)
    避免加载整个模型、避免解析包含 15 万词条的 tokenizer 对象。耗时降至 < 50ms。
    """
    # 当前 asr.py / aligner.py 明确依赖它来构造 embedding_table。
    # 这段代码存在的核心价值，是让项目能走“音频 embedding + 文本 embedding 手工拼接”的推理路线，
    # 同时避免为了取词表 embedding 而完整加载第二套高层模型对象。
    t_start = time.time()
    mm = np.memmap(model_path, mode="r")

    # 获取文件头信息
    tensor_count, kv_count = struct.unpack_from("<QQ", mm, 8)
    offs = 24
    alignment = 32

    # 光速跃过/扫描所有 KV 字段
    for _ in range(kv_count):
        key_len = struct.unpack_from("<Q", mm, offs)[0]
        offs += 8
        if key_len == 17 and mm[offs : offs + 17].tobytes() == b"general.alignment":
            offs += 17
            v_type = struct.unpack_from("<I", mm, offs)[0]
            offs += 4
            if v_type == 4:  # UINT32
                alignment = struct.unpack_from("<I", mm, offs)[0]
                offs += 4
                continue
        else:
            offs += key_len

        v_type = struct.unpack_from("<I", mm, offs)[0]
        offs += 4
        offs = _skip_gguf_value(mm, offs, v_type)

    # 扫描 Tensor Infos 搜寻我们想要的张量
    target_rel_offset = None
    target_type = None
    target_shape = None  # GGUF shape 是倒序的 [n_embd, vocab_size]

    target_bytes = target_tensor.encode("utf-8")
    for _ in range(tensor_count):
        name_len = struct.unpack_from("<Q", mm, offs)[0]
        offs += 8
        is_target = False
        if (
            name_len == len(target_bytes)
            and mm[offs : offs + name_len].tobytes() == target_bytes
        ):
            is_target = True
        offs += name_len

        n_dims = struct.unpack_from("<I", mm, offs)[0]
        offs += 4

        shape = struct.unpack_from(f"<{n_dims}Q", mm, offs)  # 返回元组
        offs += 8 * n_dims

        t_type = struct.unpack_from("<I", mm, offs)[0]
        offs += 4

        rel_offset = struct.unpack_from("<Q", mm, offs)[0]
        offs += 8

        if is_target:
            target_shape = shape
            target_type = t_type
            target_rel_offset = rel_offset

    # 计算数据区起始点并加载张量
    padding = offs % alignment
    if padding != 0:
        offs += alignment - padding
    data_offset = offs

    if target_shape is None:
        logger.error(f"无法在 {model_path} 中找到 {target_tensor}")
        return None

    abs_offset = data_offset + target_rel_offset
    n_embd = target_shape[0]  # 特征维度
    vocab_size = target_shape[1]  # 词表大小

    qtype = GGMLQuantizationType(target_type)
    if qtype in GGML_QUANT_SIZES:
        block_size, type_size = GGML_QUANT_SIZES[qtype]
        bytes_per_row = (n_embd // block_size) * type_size
    else:
        # F32 或 F16
        if qtype == GGMLQuantizationType.F32:
            bytes_per_row = n_embd * 4
        elif qtype == GGMLQuantizationType.F16:
            bytes_per_row = n_embd * 2
        else:
            raise ValueError(f"未知的数据格式支持: {qtype.name}")

    total_bytes = vocab_size * bytes_per_row
    raw_data = mm[abs_offset : abs_offset + total_bytes]

    if qtype in (GGMLQuantizationType.F32, GGMLQuantizationType.F16):
        if qtype == GGMLQuantizationType.F32:
            raw_data = raw_data.view(np.float32).reshape(vocab_size, n_embd)
        else:
            raw_data = raw_data.view(np.float16).reshape(vocab_size, n_embd)
    else:
        raw_data = raw_data.reshape(vocab_size, bytes_per_row)

    total_time = time.time() - t_start
    logger.info(
        f"--- [QwenASR] 已极速载入 Embedding 视图 ({total_time * 1000:.1f}ms) ---"
    )
    logger.info(f"    - 量化格式: {qtype.name} ({n_embd} dims, {vocab_size} tokens)")

    return LlamaEmbeddingTable(raw_data, qtype)


# =========================================================================
# Utilities
# =========================================================================


def text_to_tokens(vocab, text, add_special=False, parse_special=True):
    # 当前主要由 LlamaModel.tokenize() 间接调用。
    # 单独保留成模块级函数的好处是：把 ctypes 细节藏起来，LlamaModel 只保留语义接口。
    text_bytes = text.encode("utf-8")
    n_tokens_max = len(text_bytes) + 32
    tokens = (llama_token * n_tokens_max)()
    n = llama_tokenize(
        vocab,
        text_bytes,
        len(text_bytes),
        tokens,
        n_tokens_max,
        add_special,
        parse_special,
    )
    return [tokens[i] for i in range(n)] if n >= 0 else []


def token_to_bytes(vocab, token_id):
    # 当前主要由 LlamaModel.token_to_bytes() 间接调用，
    # 最终服务于 asr.py 的流式 utf-8 增量解码。
    buf = ctypes.create_string_buffer(256)
    n = llama_token_to_piece(vocab, token_id, buf, ctypes.sizeof(buf), 0, True)
    return buf.raw[:n] if n > 0 else b""
