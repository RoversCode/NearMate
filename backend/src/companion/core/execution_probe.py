"""在短命子进程中探测推理后端，避免设备故障拖垮控制台。"""

from __future__ import annotations

import base64
import ctypes
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor

from companion.core.execution_config import atomic_write_json
from companion.core.runtime_profile import runtime_profile_defaults


# 输入 [[1, 2]] 与固定二维权重相乘，期望 [[7, 10]]；只用于证明一次运算确实执行。
_MATMUL_MODEL = "CAkSCG5lYXJtYXRlOoEBCigKAVgKAVcSAVkaFW5lYXJtYXRlX3Byb2JlX21hdG11bCIGTWF0TXVsEg5uZWFybWF0ZV9wcm9iZSobCAIIAhABIhAAAIA/AAAAQAAAQEAAAIBAQgFXWhMKAVgSDgoMCAESCAoCCAEKAggCYhMKAVkSDgoMCAESCAoCCAEKAggCQgIQDQ=="


def _probe_onnx(provider: str, directory: Path) -> dict[str, object]:
    """执行小矩阵并读取 ORT 算子记录，区别于只查询提供方名称。"""

    if provider == "cuda":
        # NVIDIA wheels 默认不在 Windows 搜索路径中；不先注册 DLL，GPU 包会静默回落 CPU。
        from streamvox_runtime import initialize_nvidia_runtime

        initialize_nvidia_runtime(consumer="onnx_cuda", required=True)

    import numpy as np
    import onnxruntime as ort

    name = {
        "cuda": "CUDAExecutionProvider",
        "directml": "DmlExecutionProvider",
        "cpu": "CPUExecutionProvider",
    }[provider]
    if name not in ort.get_available_providers():
        raise RuntimeError(f"当前 ONNX Runtime 没有 {name}")
    options = ort.SessionOptions()
    options.enable_profiling = True
    options.profile_file_prefix = str(directory / "onnx-probe")
    options.enable_mem_pattern = False
    options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    session = ort.InferenceSession(base64.b64decode(_MATMUL_MODEL), sess_options=options, providers=[name])
    actual_session_providers = session.get_providers()
    if name not in actual_session_providers:
        session.end_profiling()
        raise RuntimeError(
            f"已请求 {name}，但会话实际只启用了 {actual_session_providers}；请检查 CUDA/cuDNN DLL 和显卡驱动"
        )
    session.disable_fallback()
    result = session.run(None, {"X": np.array([[1, 2]], dtype=np.float32)})[0]
    events = json.loads(Path(session.end_profiling()).read_text(encoding="utf-8"))
    executed = sorted({item.get("args", {}).get("provider") for item in events if item.get("args", {}).get("provider")})
    if not np.array_equal(result, np.array([[7, 10]], dtype=np.float32)) or name not in executed:
        raise RuntimeError(f"小矩阵未由指定后端正确执行，实际提供方：{executed}")
    return {"available": True, "evidence": "matmul", "actual_providers": executed}


def _probe_llama(backend: str) -> dict[str, object]:
    """加载指定插件并初始化设备；模型是否可运行仍由加载阶段验证。"""

    spec = importlib.util.find_spec("streamvox")
    if spec is None or spec.submodule_search_locations is None:
        raise RuntimeError("未安装 StreamVox")
    binary_root = Path(next(iter(spec.submodule_search_locations))) / "bin"
    if os.name != "nt":
        raise RuntimeError("此发行探测只支持 Windows x64")
    if backend == "cuda":
        from streamvox_runtime import initialize_nvidia_runtime
        initialize_nvidia_runtime(consumer="llama", required=True)
    with os.add_dll_directory(str(binary_root)):
        base = ctypes.CDLL(str(binary_root / "ggml-base.dll"))
        library = ctypes.CDLL(str(binary_root / "ggml.dll"))
        def symbol(name: str):
            """从两个固定 ggml 库解析公开设备接口。"""

            return getattr(library, name, None) or getattr(base, name)
        load_backend = symbol("ggml_backend_load")
        load_backend.argtypes = [ctypes.c_char_p]
        load_backend.restype = ctypes.c_void_p
        filename = "ggml-cpu-x64.dll" if backend == "cpu" else f"ggml-{backend}.dll"
        registration = load_backend(str(binary_root / filename).encode("utf-8"))
        if not registration:
            raise RuntimeError(f"无法加载 {backend} 插件")
        count = symbol("ggml_backend_reg_dev_count")
        count.argtypes, count.restype = [ctypes.c_void_p], ctypes.c_size_t
        if count(registration) == 0:
            raise RuntimeError(f"{backend} 插件未发现可用设备，请检查显卡驱动")
        get_device = symbol("ggml_backend_reg_dev_get")
        get_device.argtypes, get_device.restype = [ctypes.c_void_p, ctypes.c_size_t], ctypes.c_void_p
        device = get_device(registration, 0)
        initialize = symbol("ggml_backend_dev_init")
        initialize.argtypes, initialize.restype = [ctypes.c_void_p, ctypes.c_char_p], ctypes.c_void_p
        instance = initialize(device, None)
        if not instance:
            raise RuntimeError(f"{backend} 设备初始化失败")
        release = symbol("ggml_backend_free")
        release.argtypes, release.restype = [ctypes.c_void_p], None
        release(instance)
        description = symbol("ggml_backend_dev_description")
        description.argtypes, description.restype = [ctypes.c_void_p], ctypes.c_char_p
        return {"available": True, "evidence": "device_initialization", "device": description(device).decode("utf-8", errors="replace")}


def write_probe_result(kind: str, backend: str, output_path: str) -> None:
    """子进程把结构化结果写入受管临时文件，不让前端解析终端日志。

    Args:
        kind: onnx 或 llama。
        backend: 本次明确检测的后端。
        output_path: 父进程建立的临时结果文件。
    Returns:
        无返回值。
    """

    output = Path(output_path)
    try:
        from companion.core.native_runtime_env import configure_native_runtime_environment
        configure_native_runtime_environment()
        result = _probe_onnx(backend, output.parent) if kind == "onnx" else _probe_llama(backend)
    except Exception as error:
        result = {"available": False, "reason": str(error)}
    atomic_write_json(output, result)


def probe_backend(kind: str, backend: str) -> dict[str, object]:
    """以超时边界运行一次设备检测，崩溃和缺依赖均作为不可用原因返回。"""

    with tempfile.TemporaryDirectory(prefix="nearmate-probe-") as directory:
        result_path = Path(directory) / "result.json"
        command = "from companion.core.execution_probe import write_probe_result; import sys; write_probe_result(*sys.argv[1:])"
        try:
            # 普通子进程不会继承父进程的解释器开关，必须显式禁止向安装目录写缓存。
            subprocess.run([sys.executable, "-I", "-B", "-X", "utf8", "-c", command, kind, backend, str(result_path)],
                           capture_output=True, timeout=25, check=False, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            result = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, subprocess.TimeoutExpired):
            result = {"available": False, "reason": "设备检测超时或进程退出，请检查运行组件与驱动"}
    return {"kind": kind, "backend": backend, **result}


def probe_execution_capabilities() -> dict[str, object]:
    """并行检查独立后端，并返回可供首次配置确认的推荐组合。"""

    candidates = [("onnx", "cuda"), ("onnx", "directml"), ("onnx", "cpu"), ("llama", "cuda"), ("llama", "vulkan"), ("llama", "cpu")]
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = [executor.submit(probe_backend, kind, backend) for kind, backend in candidates]
        results = [future.result() for future in futures]
    available = {(item["kind"], item["backend"]) for item in results if item["available"]}
    preferred_onnx = ("cuda", "directml", "cpu") if runtime_profile_defaults().name == "cuda" else ("directml", "cpu")
    onnx = next((item for item in preferred_onnx if ("onnx", item) in available), None)
    llama = next((item for item in ("cuda", "vulkan", "cpu") if ("llama", item) in available), None)
    return {"backends": results, "recommendation": {"onnx_provider": onnx, "llama_backend": llama} if onnx and llama else None}
