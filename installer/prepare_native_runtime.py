"""复用公开 StreamVox 固定 wheel 中的同版本 ASR 原生库。"""

from __future__ import annotations

import argparse
import hashlib
from importlib.metadata import version
from pathlib import Path

if __package__:
    from .runtime_asset_io import assert_no_reparse_points, copy_verified_asset, verify_asset
else:
    from runtime_asset_io import assert_no_reparse_points, copy_verified_asset, verify_asset


def prepare_native_runtime(destination: Path) -> None:
    """把经过固定摘要核验的 SDK 原生库复制到当前 checkout。

    Args:
        destination: 当前 checkout 的 backend/bin 目录；现有文件必须匹配固定摘要。

    Returns:
        无；SDK 版本或任一 DLL 摘要不匹配时抛出异常，拒绝混合原生版本。
    """
    if version("streamvox") != "2.0.0":
        raise ValueError("ASR 原生库必须来自公开固定 StreamVox 2.0.0 wheel")
    from streamvox import native_assets

    source_root = Path(native_assets.__file__).resolve().parent / "bin"
    assert_no_reparse_points(source_root)
    assert_no_reparse_points(destination)
    native_assets.validate_native_directory(source_root)
    # llama.cpp DLL 与既有 ASR 逐字节相同；OpenMP 沿用 SDK 的公开 LLVM 运行库替换。
    for name, digest in native_assets.NATIVE_DLL_SHA256.items():
        source = source_root / name
        target = destination / name
        size = source.stat().st_size
        if target.exists():
            verify_asset(target, size, digest)
        else:
            copy_verified_asset(source, target, size, digest)
    manifest = source_root / "native-manifest.json"
    digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
    copy_verified_asset(manifest, destination / manifest.name, manifest.stat().st_size, digest)
    print(f"已准备公开固定 ASR 原生库：{destination}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, default=Path(__file__).resolve().parents[1] / "backend/bin")
    prepare_native_runtime(parser.parse_args().destination)
