from __future__ import annotations

import os
import importlib.metadata
import ctypes
from pathlib import Path

from streamvox_runtime import ensure_single_onnxruntime_distribution
from companion.core.runtime_profile import runtime_profile_defaults
from companion.core.runtime_paths import NATIVE_ROOT


_DLL_DIRECTORY_HANDLES: dict[str, object] = {}
_LOADED_RUNTIME_LIBRARIES: dict[str, object] = {}


def configure_native_runtime_environment() -> None:
    """校验安装版依赖组合，并为当前进程绑定唯一的原生推理运行库。

    Args:
        无；读取当前进程的发行 profile 和随包依赖。

    Returns:
        无；保留 DLL 句柄并补齐默认环境变量，不覆盖用户显式设置。

    Raises:
        RuntimeError: 安装版依赖组合未经验证，或 DLL 加载来源与发行目录不符。
    """

    # 当前输入链路会同时加载多种预编译 native runtime；本地 Windows 环境下只能接受 OpenMP 共存妥协。
    os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
    # 实时语音链路更看重尾延迟稳定性；默认限制 CPU OpenMP/MKL 线程，用户仍可在启动前显式覆盖。
    os.environ.setdefault("OMP_NUM_THREADS", "1")
    os.environ.setdefault("MKL_NUM_THREADS", "1")
    # 控制面自检不需要 CUDA。只有明确选择 CUDA 的推理子进程才加载它的计算库。
    ensure_single_onnxruntime_distribution()
    if os.environ.get("NEARMATE_PACKAGED") != "1":
        return
    profile = runtime_profile_defaults()
    distribution_name = "onnxruntime-gpu" if profile.name == "cuda" else "onnxruntime-directml"
    distribution = importlib.metadata.distribution(distribution_name)
    if distribution.version != "1.21.1":
        raise RuntimeError(f"NearMate {profile.name} 安装版需要 {distribution_name} 1.21.1，请修复运行组件")
    # sherpa 新版会在首次创建模型时请求更高的 ORT C API 并直接崩溃，必须在加载 DLL 前拒绝混装。
    # 两种 profile 的唤醒和声纹均固定 CPU；这份组合已实际执行相同的发行模型。
    for package_name in ("sherpa-onnx", "sherpa-onnx-core"):
        try:
            package_version = importlib.metadata.version(package_name)
        except importlib.metadata.PackageNotFoundError as exception:
            raise RuntimeError(f"NearMate 安装版缺少 {package_name} 1.12.20，请修复运行组件") from exception
        if package_version != "1.12.20":
            raise RuntimeError(f"NearMate 安装版需要 {package_name} 1.12.20（CPU）搭配 ONNX Runtime 1.21.1，请修复运行组件")
    if os.name != 'nt':
        return
    library_path = Path(distribution.locate_file('onnxruntime/capi/onnxruntime.dll')).resolve()
    if not NATIVE_ROOT.is_dir():
        raise RuntimeError(f"安装运行时缺少 native 目录：{NATIVE_ROOT}")
    for directory in (NATIVE_ROOT, library_path.parent):
        key = str(directory)
        if key not in _DLL_DIRECTORY_HANDLES:
            # 句柄必须保留到进程退出，否则微软运行库目录会被 Python 立即移除。
            _DLL_DIRECTORY_HANDLES[key] = os.add_dll_directory(key)
    if 'onnxruntime' in _LOADED_RUNTIME_LIBRARIES:
        return
    # sherpa 的原生扩展也链接 ONNX Runtime；先绑定应用唯一版本，不能误用 Windows 自带 DLL。
    library = ctypes.WinDLL(str(library_path))
    buffer = ctypes.create_unicode_buffer(32768)
    get_module_path = ctypes.windll.kernel32.GetModuleFileNameW
    get_module_path.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint32]
    get_module_path.restype = ctypes.c_uint32
    if not get_module_path(library._handle, buffer, len(buffer)) or Path(buffer.value).resolve() != library_path:
        raise RuntimeError('ONNX Runtime 加载来源与安装清单不符，请修复运行组件')
    _LOADED_RUNTIME_LIBRARIES['onnxruntime'] = library
