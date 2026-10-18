"""兼容安装脚本的历史导入，实际 IO 统一由 Backend 资源包提供。"""

from pathlib import Path
import sys


# 冻结 Python 可能尚未复制本次 Backend 包；构建 CLI 必须优先读取当前 checkout 的实现。
source_root = str(Path(__file__).resolve().parents[1] / "backend" / "src")
if source_root not in sys.path:
    sys.path.insert(0, source_root)

from companion.runtime_assets import io as _shared_io


# 保留模块身份，使历史调用方替换 urlopen 的测试钩子仍作用在同一份实现上。
sys.modules[__name__] = _shared_io
