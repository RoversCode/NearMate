"""把发行目录中的 Python 包编译成不带明文源码的运行布局。"""

from __future__ import annotations

import argparse
import compileall
import shutil
from pathlib import Path


def compile_sourceless(source_root: Path) -> int:
    """把 source_root 下的 Python 模块编译成旁边的 `.pyc` 文件并删除源码。

    Args:
        source_root: 发行 staging 中的 Python 包根目录。

    Returns:
        编译并保留的模块数量。

    Raises:
        RuntimeError: 任意模块编译失败时抛出。
    """
    if not source_root.is_dir():
        raise FileNotFoundError(f"运行代码目录不存在：{source_root}")

    # 先统一编译，避免删除源码后无法生成包内其它模块的字节码。
    if not compileall.compile_dir(
        str(source_root),
        quiet=1,
        legacy=True,
        optimize=0,
        force=True,
        ddir="<nearmate-runtime>",
    ):
        raise RuntimeError(f"Python 运行代码编译失败：{source_root}")

    compiled_count = 0
    for py_path in sorted(source_root.rglob("*.py")):
        pyc_path = py_path.with_suffix(".pyc")
        if not pyc_path.is_file():
            raise RuntimeError(f"编译后缺少字节码：{py_path}")
        py_path.unlink()
        compiled_count += 1

    # compileall 的 legacy 模式会在原目录旁生成 pyc；清除残留缓存目录，
    # 这样安装包不会携带构建机路径或重复字节码。
    for cache_dir in source_root.rglob("__pycache__"):
        if cache_dir.is_dir():
            shutil.rmtree(cache_dir)

    return compiled_count


def main() -> int:
    """执行命令行编译入口。"""
    parser = argparse.ArgumentParser(description="编译 NearMate Backend sourceless runtime")
    parser.add_argument("source_root", type=Path)
    args = parser.parse_args()
    count = compile_sourceless(args.source_root)
    print(f"已编译 sourceless Python 模块：{count} 个")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
