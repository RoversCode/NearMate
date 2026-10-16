"""保存验证进程实际使用的硬件、依赖和动态库证据。"""

from __future__ import annotations

import ctypes
import hashlib
import importlib.metadata
import os
import platform
import subprocess
import sys
from pathlib import Path


def file_sha256(path: Path) -> str:
    """分块计算文件摘要，避免把大模型整体读进内存。

    Args:
        path: 待记录的模型或动态库文件。
    Returns:
        文件内容的 SHA-256 摘要。
    """
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def environment_fingerprint() -> dict:
    """读取当前进程已加载的库，不能用安装目录冒充实际来源。

    Returns:
        解释器、依赖版本、GPU 驱动和已加载动态库路径及摘要。
    """
    import onnxruntime as ort

    packages = {
        item.metadata["Name"]: item.version
        for item in importlib.metadata.distributions()
    }
    paths = loaded_library_paths()
    libraries = []
    for path in sorted(paths):
        # 只记计算后端及 NVIDIA 库；驱动来自系统目录是正常的，CUDA 用户态库应来自独立环境。
        if not path.name.lower().startswith(
            ("cu", "nv", "onnxruntime", "ggml", "llama")
        ):
            continue
        if path.is_file() and path.suffix.lower() in (".dll", ".pyd", ".so"):
            libraries.append({"path": str(path), "sha256": file_sha256(path)})
    try:
        gpu = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,uuid,driver_version",
                "--format=csv,noheader",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError) as error:
        gpu = f"未取得 GPU 信息：{type(error).__name__}"
    return {
        "pid": os.getpid(),
        "python": sys.version,
        "executable": sys.executable,
        "platform": platform.platform(),
        "packages": packages,
        "gpu": gpu,
        "ort_build": ort.get_build_info(),
        "libraries": libraries,
    }


def loaded_library_paths() -> set[Path]:
    """枚举本进程的 Windows 模块，不给旧环境额外安装依赖。

    Returns:
        当前已加载模块的绝对路径集合。
    """
    if os.name != "nt":
        raise RuntimeError("本阶段动态库证据采集仅支持 Windows")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = ctypes.c_void_p
    kernel.K32EnumProcessModules.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint32),
    ]
    kernel.GetModuleFileNameW.argtypes = [
        ctypes.c_void_p,
        ctypes.c_wchar_p,
        ctypes.c_uint32,
    ]
    process = kernel.GetCurrentProcess()
    modules = (ctypes.c_void_p * 4096)()
    needed = ctypes.c_uint32()
    if not kernel.K32EnumProcessModules(
        process, modules, ctypes.sizeof(modules), ctypes.byref(needed)
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    if needed.value > ctypes.sizeof(modules):
        raise RuntimeError("已加载模块数量超过采集上限，不能生成不完整证据")
    paths = set()
    for module in modules[: needed.value // ctypes.sizeof(ctypes.c_void_p)]:
        buffer = ctypes.create_unicode_buffer(32768)
        if not kernel.GetModuleFileNameW(module, buffer, len(buffer)):
            raise ctypes.WinError(ctypes.get_last_error())
        paths.add(Path(buffer.value))
    return paths


def compare_environments(expected: dict | None, actual: dict, provider: str) -> dict:
    """核对计算条件，允许移除 Torch 以及不同的安装目录。

    Args:
        expected: 记录时的原始环境证据，旧记录可能缺失。
        actual: 回放后的实际环境证据。
        provider: 当前回放所要求的执行后端。
    Returns:
        是否一致及不一致的字段；缺失证据不能视为相同。
    """
    if not expected:
        return {"match": False, "differences": ["基线缺少环境指纹"]}
    differences = []
    keys = ["python", "platform", "ort_build"]
    if provider != "CPUExecutionProvider":
        keys.append("gpu")
    for key in keys:
        if not expected.get(key) or expected.get(key) != actual.get(key):
            differences.append(key)
    if provider != "CPUExecutionProvider" and "未取得" in actual.get("gpu", ""):
        differences.append("GPU 信息缺失")
    if expected.get("packages", {}).get("numpy") != actual.get("packages", {}).get(
        "numpy"
    ):
        differences.append("numpy")
    # 只核对 ORT 及本发行所用的 NVIDIA 计算库；Torch 额外加载的训练库不属于回放链。
    prefixes = ("onnxruntime",)
    if provider == "CUDAExecutionProvider":
        prefixes += ("cublas", "cudnn", "cudart", "cufft64", "curand", "nvrtc")
    library_sets = []
    for environment in (expected, actual):
        libraries = {}
        for item in environment.get("libraries", []):
            name = Path(item["path"]).name.lower()
            if name.startswith(prefixes):
                libraries.setdefault(name, set()).add(item["sha256"])
        library_sets.append(libraries)
    before, after = library_sets
    if not before or not after:
        differences.append("计算库摘要缺失")
    for name in sorted(before.keys() | after.keys()):
        if before.get(name) != after.get(name) or len(after.get(name, set())) != 1:
            differences.append(name)
    return {"match": not differences, "differences": differences}
