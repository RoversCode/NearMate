"""按公开固定清单准备实时输入模型；只写当前 checkout 的 backend/ckpt。"""

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

from companion.runtime_assets.realtime import prepare_realtime_assets


if __name__ == "__main__":
    repository_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=Path(__file__).with_name("realtime-assets.json"))
    parser.add_argument("--destination", type=Path, default=repository_root / "backend/ckpt")
    parser.add_argument("--cache-root", type=Path, default=repository_root / ".artifacts/realtime-assets")
    parser.add_argument("--source-root", type=Path)
    arguments = parser.parse_args()
    prepare_realtime_assets(arguments.manifest, arguments.destination, arguments.cache_root, arguments.source_root,
                            progress=lambda message: print(message, flush=True))
