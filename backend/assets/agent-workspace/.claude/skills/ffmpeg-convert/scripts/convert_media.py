"""使用 ffmpeg 完成一次可控的音视频格式转换。"""

from __future__ import annotations

import argparse
import shutil
import subprocess
from pathlib import Path


def main() -> int:
    """解析命令行参数并运行 ffmpeg。

    Returns:
        转换成功时返回零；参数、依赖或输出校验失败时由异常终止。
    """

    parser = argparse.ArgumentParser(description="使用 ffmpeg 转换音频或视频文件")
    parser.add_argument("--input", required=True, help="输入文件绝对路径")
    parser.add_argument("--output", required=True, help="输出文件绝对路径")
    parser.add_argument(
        "--ffmpeg-arg",
        action="append",
        default=[],
        help="额外传给 ffmpeg 的单个参数，可重复使用",
    )
    arguments = parser.parse_args()

    input_path = Path(arguments.input).expanduser().resolve(strict=True)
    output_path = Path(arguments.output).expanduser().resolve(strict=False)
    if not input_path.is_file():
        raise FileNotFoundError(f"输入文件不存在：{input_path}")
    if input_path == output_path:
        raise ValueError("输出文件不能覆盖输入文件")
    ffmpeg_path = shutil.which("ffmpeg")
    if ffmpeg_path is None:
        raise RuntimeError("系统没有安装 ffmpeg，无法执行格式转换")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        ffmpeg_path,
        "-hide_banner",
        "-nostdin",
        "-y",
        "-i",
        str(input_path),
        *arguments.ffmpeg_arg,
        str(output_path),
    ]
    subprocess.run(command, check=True)
    if not output_path.is_file() or output_path.stat().st_size <= 0:
        raise RuntimeError("ffmpeg 没有生成有效输出文件")
    print(str(output_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
