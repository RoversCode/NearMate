"""只在开发基准进程显式加载旧环境的 NVIDIA 库。"""

import ctypes
from pathlib import Path


def initialize_baseline_runtime() -> None:
    """让基准和候选记录同一组计算库，消除按需加载造成的证据缺项。

    Returns:
        无；所有 DLL 仍来自未修改的基准环境。
    """
    import torch

    library_dir = Path(torch.__file__).parent / "lib"
    prefixes = ("cublas", "cudnn", "cudart", "cufft64", "curand", "nvrtc")
    # Torch 初始化先加载共同依赖，再保留与候选发行对应的库句柄到进程结束。
    for path in sorted(library_dir.glob("*.dll")):
        if path.name.lower().startswith(prefixes):
            _libraries.append(ctypes.WinDLL(str(path)))


_libraries = []
