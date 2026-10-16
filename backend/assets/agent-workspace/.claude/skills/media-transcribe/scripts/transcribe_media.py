"""media-transcribe Skill 的薄启动器：定位 NearMate 虚拟环境并执行离线转写 CLI。

转写的重逻辑全部在仓库源码树内（companion.audio.offline_transcribe），
本脚本会被物化复制到各任务的 Skill 目录下运行，只负责三件事：
1. 从自身位置向上定位 NearMate 仓库根（标记：backend/src/companion 目录）；
2. 找到仓库虚拟环境解释器（.venv/Scripts/python.exe 或 .venv/bin/python）；
3. 用该解释器执行 `python -m companion.audio.offline_transcribe` 并透传全部参数与退出码。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def main() -> int:
    """定位解释器并透传执行离线转写 CLI。

    Returns:
        离线转写 CLI 的退出码。
    """
    repo_root = _find_repo_root()
    venv_python = _find_venv_python(repo_root)
    completed = subprocess.run(
        [str(venv_python), "-m", "companion.audio.offline_transcribe", *sys.argv[1:]],
        check=False,
    )
    return completed.returncode


def _find_repo_root() -> Path:
    """定位 NearMate 仓库根目录。

    本脚本的物化位置固定在 backend/data/agent-runtime/activities/<任务>/skills/
    media-transcribe/scripts/ 下，向上逐层查找仓库标记目录即可。
    NEARMATE_REPO_ROOT 环境变量可显式覆盖。
    """
    override = os.environ.get("NEARMATE_REPO_ROOT")
    if override:
        override_root = Path(override).expanduser().resolve()
        if (override_root / "backend" / "src" / "companion").is_dir():
            return override_root
        raise RuntimeError(f"NEARMATE_REPO_ROOT 指向的目录不是 NearMate 仓库根：{override_root}")
    for candidate in Path(__file__).resolve().parents:
        if (candidate / "backend" / "src" / "companion").is_dir():
            return candidate
    raise RuntimeError(
        "无法定位 NearMate 仓库根目录（缺少 backend/src/companion 标记）；"
        "可设置 NEARMATE_REPO_ROOT 环境变量显式指定"
    )


def _find_venv_python(repo_root: Path) -> Path:
    """在仓库根下定位虚拟环境解释器（Windows 与 POSIX 布局都认）。"""
    candidates = [
        repo_root / ".venv" / "Scripts" / "python.exe",
        repo_root / ".venv" / "bin" / "python",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise RuntimeError(f"NearMate 虚拟环境解释器缺失（尝试过：{candidates}）")


if __name__ == "__main__":
    raise SystemExit(main())
