"""复用公开 StreamVox 固定 wheel 中的同版本 ASR 原生库。"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

if __package__:
    from . import runtime_asset_io
else:
    # 隔离 Python 不自动加入脚本目录；明确定位兼容入口，不依赖宿主 PYTHONPATH。
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import runtime_asset_io

from companion.runtime_assets.native import prepare_native_runtime


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, default=Path(__file__).resolve().parents[1] / "backend/bin")
    prepare_native_runtime(parser.parse_args().destination, progress=lambda message: print(message, flush=True))
