"""只在源码运行时准备实时输入资源，安装态坚持使用随包文件。"""

from pathlib import Path
import os

from .control import AssetPreparationCancelled, CancellationCheck, ProgressCallback, check_cancelled
from .io import assert_no_reparse_points
from .locking import asset_lock
from .native import prepare_native_runtime
from .realtime import prepare_realtime_assets


def prepare_source_runtime_assets(
    *, repository_root: Path | None = None, progress: ProgressCallback | None = None,
    is_cancelled: CancellationCheck | None = None,
) -> None:
    """为源码启动准备固定模型与 ASR 原生库，完整资源直接离线复用。

    Args:
        repository_root: 源码仓库根目录；省略时从当前源码包位置推导。
        progress: 接收限频中文阶段、文件名和下载进度的回调。
        is_cancelled: 等锁、下载和校验期间检查的取消信号。

    Returns:
        无；安装态直接返回，源码准备取消时抛出 AssetPreparationCancelled。
    """
    # 安装版由构建链固定资源，任何初始化重试都不能向只读安装目录下载或写锁。
    if os.environ.get("NEARMATE_PACKAGED") == "1":
        return
    check_cancelled(is_cancelled)
    root = Path(os.path.abspath(repository_root or Path(__file__).parents[4]))
    assert_no_reparse_points(root)
    manifest = root / "installer" / "realtime-assets.json"
    if not manifest.is_file():
        raise RuntimeError("源码仓库缺少 installer/realtime-assets.json，无法准备运行资源")
    with asset_lock(root / ".artifacts" / "runtime-assets-source.lock", is_cancelled):
        prepare_realtime_assets(manifest, root / "backend" / "ckpt", root / ".artifacts" / "realtime-assets",
                                progress=progress, is_cancelled=is_cancelled)
        check_cancelled(is_cancelled)
        prepare_native_runtime(root / "backend" / "bin", progress=progress, is_cancelled=is_cancelled)
