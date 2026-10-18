"""从固定 StreamVox wheel 准备同版本 ASR 原生库。"""

from __future__ import annotations

import hashlib
from importlib.metadata import PackageNotFoundError, version
import json
from pathlib import Path

from .control import CancellationCheck, ProgressCallback, ProgressReporter, check_cancelled
from .io import assert_no_reparse_points, copy_verified_asset, keep_valid_asset, safe_asset_path, verify_asset
from .locking import asset_lock


def prepare_native_runtime(
    destination: Path, *, progress: ProgressCallback | None = None,
    is_cancelled: CancellationCheck | None = None,
) -> None:
    """核验固定 SDK 内的原生库，并修复当前 checkout 缺失或损坏的副本。

    Args:
        destination: 当前 checkout 的 backend/bin 目录。
        progress: 接收安全中文进度的回调。
        is_cancelled: 等锁、校验和复制时检查的取消信号。

    Returns:
        无；SDK 缺失、版本错误或源文件损坏时清晰报错，不修改 SDK。
    """
    check_cancelled(is_cancelled)
    try:
        sdk_version = version("streamvox")
    except PackageNotFoundError:
        raise RuntimeError("缺少公开固定 StreamVox 2.0.0，请先安装项目依赖") from None
    if sdk_version != "2.0.0":
        raise ValueError("ASR 原生库必须来自公开固定 StreamVox 2.0.0 wheel")
    try:
        from streamvox import native_assets
    except ImportError:
        raise RuntimeError("StreamVox 2.0.0 原生资源模块不可用，请修复项目依赖") from None

    source_root = Path(native_assets.__file__).resolve().parent / "bin"
    assert_no_reparse_points(source_root)
    assert_no_reparse_points(destination)
    expected = dict(native_assets.NATIVE_DLL_SHA256)
    manifest = source_root / "native-manifest.json"
    reporter = ProgressReporter(progress)
    reporter.report("开始检查 ASR 原生运行库", force=True)
    # SDK 的来源清单也必须与固定版本声明一致，不能只凭 DLL 文件名接受另一套运行库。
    try:
        assert_no_reparse_points(manifest)
        if json.loads(manifest.read_text(encoding="utf-8")) != native_assets.native_manifest():
            raise ValueError("SDK 清单不匹配")
        if {item.name for item in source_root.iterdir()} != {*expected, manifest.name}:
            raise ValueError("SDK 目录文件不匹配")
        for name, digest in expected.items():
            source = safe_asset_path(source_root, name)
            verify_asset(source, source.stat().st_size, digest, is_cancelled=is_cancelled)
    except (OSError, ValueError):
        raise RuntimeError("StreamVox 2.0.0 原生资源不完整或摘要错误，请修复项目依赖") from None

    manifest_bytes = manifest.read_bytes()
    expected[manifest.name] = hashlib.sha256(manifest_bytes).hexdigest()
    with asset_lock(destination.parent / f".{destination.name}.runtime-assets.lock", is_cancelled):
        # llama.cpp DLL 与既有 ASR 逐字节相同；OpenMP 沿用 SDK 的公开 LLVM 运行库替换。
        for name, digest in expected.items():
            check_cancelled(is_cancelled)
            source = safe_asset_path(source_root, name)
            target = safe_asset_path(destination, name)
            size = source.stat().st_size
            reporter.report(f"正在检查原生运行库：{name}")
            if not keep_valid_asset(target, size, digest, is_cancelled=is_cancelled):
                copy_verified_asset(source, target, size, digest, is_cancelled=is_cancelled)
        reporter.report("ASR 原生运行库已准备完成", force=True)
