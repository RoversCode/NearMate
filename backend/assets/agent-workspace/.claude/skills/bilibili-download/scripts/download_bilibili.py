"""使用 BBDown 下载 B 站视频/音频的命令行包装。

BBDown 是开源的命令行哔哩哔哩下载器（https://github.com/nilaoda/BBDown）。
本脚本负责三件事：
1. 定位 BBDown 可执行文件（PATH 优先，其次产品本地缓存，最后按钉死的
   版本从 GitHub Releases 下载并校验 SHA256）；
2. 把产品化参数（清晰度、分P、纯音频、弹幕、cookie）翻译成 BBDown 命令行；
3. 下载完成后把产出从临时工作目录整理进交付目录并校验。

版权红线：BBDown 仅供个人学习研究，下载内容的合规性由使用者自行负责。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

# 钉死的 BBDown 版本与下载信息；升级时必须同步更新校验和。
_BBDOWN_VERSION = "1.6.3"
_BBDOWN_DOWNLOAD_URL = (
    "https://github.com/nilaoda/BBDown/releases/download/1.6.3/"
    "BBDown_1.6.3_20240814_win-x64.zip"
)
_BBDOWN_ZIP_SHA256 = "40f1e2af0d4e74df765c6f93d2e931f9bea201d5168d0bc62dc35a54b7e0ec02"
_BBDOWN_ZIP_MEMBER = "BBDown.exe"

# BBDown 缓存目录（相对仓库根）：backend/data/agent-tools/bbdown。
_BBDOWN_CACHE_PARTS = ("backend", "data", "agent-tools", "bbdown")

# BBDown 画质优先级参数（-q/--dfn-priority）的候选名，逗号分隔表示依次回退。
_QUALITY_PRIORITY = {
    "1080p": "1080P 高码率,1080P 高清",
    "720p": "720P 高清",
    "480p": "480P 清晰",
    "360p": "360P 流畅",
}

# 下载完成后会被整理进交付目录的产出扩展名（媒体 + 弹幕 + 字幕 + 封面）。
_DELIVERABLE_EXTENSIONS = {".mp4", ".m4a", ".ass", ".xml", ".srt", ".jpg", ".png"}
# 判定“确实下载到了内容”的媒体扩展名。
_MEDIA_EXTENSIONS = {".mp4", ".m4a"}


def main(argv: list[str] | None = None) -> int:
    """解析命令行参数，执行一次 B 站下载并把产出路径打印到 stdout。

    Returns:
        下载成功返回零；依赖获取、下载或产出校验失败时由异常终止。
    """
    parser = argparse.ArgumentParser(description="使用 BBDown 下载 B 站视频/音频")
    parser.add_argument("--url", required=True, help="B 站视频地址，支持 BV/av/b23 短链/番剧链接")
    parser.add_argument("--output-dir", required=True, help="交付目录（下载产出的最终存放位置）")
    parser.add_argument(
        "--quality",
        choices=["best", "1080p", "720p", "480p", "360p"],
        default="best",
        help="视频清晰度上限，默认 best（不限制，取可用的最高清晰度）",
    )
    parser.add_argument("--audio-only", action="store_true", help="只下载音频（.m4a）")
    parser.add_argument(
        "--pages",
        default=None,
        help="分P 选择，如 3、1,2,10、1-10、ALL、LAST；不传则按链接自身的分P 语义",
    )
    parser.add_argument("--danmaku", action="store_true", help="同时下载弹幕（xml/ass）")
    parser.add_argument(
        "--cookie-file",
        default=None,
        help="包含 B 站 cookie（如 SESSDATA=...）的文本文件，用于解锁登录清晰度/会员内容",
    )
    arguments = parser.parse_args(argv)

    output_dir = Path(arguments.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    bbdown_path = ensure_bbdown_executable()

    # 下载先落在交付目录下的临时工作区，成功后再整理上来：
    # 失败时交付目录不会被半成品污染。
    work_dir = Path(tempfile.mkdtemp(prefix=".bbdown-work-", dir=output_dir))
    try:
        command = _build_bbdown_command(arguments, bbdown_path, work_dir)
        # stdin 置空：BBDown 遇到多分P 未指定等情况可能交互式询问，
        # 后台任务里没有人回答，宁可以非零退出快速失败也不能挂起。
        completed = subprocess.run(command, stdin=subprocess.DEVNULL, check=False)
        if completed.returncode != 0:
            raise RuntimeError(f"BBDown 下载失败（退出码 {completed.returncode}）")
        deliverables = _collect_deliverables(work_dir, output_dir)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

    media_files = [path for path in deliverables if path.suffix.lower() in _MEDIA_EXTENSIONS]
    if not media_files:
        raise RuntimeError("BBDown 没有产出任何媒体文件，不能作为成功结果交付")

    # 机器可读的最终结果只放 stdout 最后一行，供 Skill 调用方与 Agent 解析。
    print(
        json.dumps(
            {
                "url": arguments.url,
                "bbdown_path": str(bbdown_path),
                "media_files": [str(path) for path in media_files],
                "deliverables": [str(path) for path in deliverables],
            },
            ensure_ascii=False,
        )
    )
    return 0


def ensure_bbdown_executable() -> Path:
    """按 PATH → 本地缓存 → 钉版本下载的顺序获取 BBDown 可执行文件路径。

    Returns:
        可用的 BBDown 可执行文件绝对路径。

    Raises:
        RuntimeError: 三条路径都不可用，或下载内容校验和不匹配。
    """
    path_hit = shutil.which("BBDown")
    if path_hit:
        return Path(path_hit).resolve()

    repo_root = _find_repo_root()
    cache_dir = repo_root.joinpath(*_BBDOWN_CACHE_PARTS)
    cached_exe = cache_dir / _BBDOWN_ZIP_MEMBER
    if cached_exe.is_file():
        return cached_exe

    if sys.platform != "win32":
        raise RuntimeError(
            "BBDown 自动下载目前只提供 Windows x64 版本；请手动安装 BBDown 并加入 PATH 后重试"
        )

    cache_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=cache_dir) as staging_dir:
        zip_path = Path(staging_dir) / "bbdown.zip"
        urllib.request.urlretrieve(_BBDOWN_DOWNLOAD_URL, zip_path)
        actual_sha256 = hashlib.sha256(zip_path.read_bytes()).hexdigest()
        if actual_sha256.casefold() != _BBDOWN_ZIP_SHA256.casefold():
            raise RuntimeError(
                f"BBDown 下载文件校验和不匹配（期望 {_BBDOWN_ZIP_SHA256}，实际 {actual_sha256}），"
                "已拒绝使用，请检查网络或手动安装 BBDown 并加入 PATH"
            )
        with zipfile.ZipFile(zip_path) as archive:
            archive.extract(_BBDOWN_ZIP_MEMBER, staging_dir)
        shutil.move(str(Path(staging_dir) / _BBDOWN_ZIP_MEMBER), cached_exe)
    return cached_exe


def _build_bbdown_command(arguments: argparse.Namespace, bbdown_path: Path, work_dir: Path) -> list[str]:
    """把产品化参数翻译成 BBDown 命令行。"""
    command = [str(bbdown_path), arguments.url, "--work-dir", str(work_dir)]
    # 纯音频下载没有视频流，清晰度参数无意义，不传避免 BBDown 行为分歧。
    if not arguments.audio_only and arguments.quality != "best":
        command.extend(["-q", _QUALITY_PRIORITY[arguments.quality]])
    if arguments.audio_only:
        command.append("--audio-only")
    if arguments.pages:
        command.extend(["-p", arguments.pages])
    if arguments.danmaku:
        command.append("--download-danmaku")
    if arguments.cookie_file:
        command.extend(["--config-file", str(_write_cookie_config(arguments.cookie_file, work_dir))])
    return command


def _write_cookie_config(cookie_file: str, work_dir: Path) -> Path:
    """把 cookie 写成 BBDown 配置文件，避免它出现在进程命令行里被日志/进程列表看到。"""
    cookie_path = Path(cookie_file).expanduser().resolve(strict=True)
    cookie_text = cookie_path.read_text(encoding="utf-8").strip()
    if not cookie_text:
        raise ValueError(f"cookie 文件为空：{cookie_path}")
    config_path = work_dir / "BBDown.config"
    # BBDown 配置文件格式：选项一行、参数值一行。
    config_path.write_text(f"-c\n{cookie_text}\n", encoding="utf-8")
    return config_path


def _collect_deliverables(work_dir: Path, output_dir: Path) -> list[Path]:
    """把临时工作区里的产出移动到交付目录，返回所有产出文件的最终路径。"""
    deliverables: list[Path] = []
    for produced in sorted(work_dir.rglob("*")):
        if not produced.is_file() or produced.suffix.lower() not in _DELIVERABLE_EXTENSIONS:
            continue
        relative = produced.relative_to(work_dir)
        target = output_dir / relative
        if target.exists():
            raise FileExistsError(f"交付目录已存在同名文件，为避免覆盖请清理后重试：{target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(produced), target)
        deliverables.append(target)
    return deliverables


def _find_repo_root() -> Path:
    """定位 NearMate 仓库根目录。

    本脚本会被物化复制到 backend/data/agent-runtime/activities/<任务>/skills/
    bilibili-download/scripts/ 下运行，从自身路径向上逐层查找仓库标记目录。
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


if __name__ == "__main__":
    raise SystemExit(main())
