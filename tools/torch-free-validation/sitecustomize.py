"""仅验证环境通过 PYTHONPATH 启用，覆盖不重载主模块的解码子进程。"""

import os

if os.environ.get("TORCH_FREE_TRACE"):
    from ort_trace import install_trace

    install_trace()
