"""构建资产共用的安全路径、固定摘要和原子下载工具。"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
from urllib.request import Request, urlopen
from uuid import uuid4


def assert_no_reparse_points(path: Path) -> None:
    """拒绝路径自身及父目录中的链接，避免构建写入其它目录。

    Args:
        path: 准备读取或写入的文件、目录；不存在的尾部路径允许稍后创建。

    Returns:
        无；发现符号链接或 Windows 重解析点时抛出 ValueError。
    """
    current = Path(os.path.abspath(path))
    for candidate in (current, *current.parents):
        try:
            metadata = candidate.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(metadata.st_mode) or (
            getattr(metadata, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
        ):
            raise ValueError(f"构建资产路径不能经过重解析点：{candidate}")


def safe_asset_path(root: Path, relative: str) -> Path:
    """把清单中的安全相对路径放到指定根目录中。

    Args:
        root: 当前操作拥有的资产根目录。
        relative: 清单中的正斜杠相对路径。

    Returns:
        没有越界或重解析点的绝对路径。
    """
    parsed = PurePosixPath(relative)
    segments = relative.split("/")
    if (
        not relative or "\\" in relative or ":" in relative
        or parsed.is_absolute() or any(part in ("", ".", "..") for part in segments)
        or any(character in '<>"|?*' or ord(character) < 32 or ord(character) == 127 for character in relative)
        or any(part.endswith((" ", ".")) for part in segments)
        or any(re.fullmatch(r"(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])", part.split(".")[0], re.I) for part in segments)
    ):
        raise ValueError(f"构建资产必须使用安全相对路径：{relative}")
    result = Path(os.path.abspath(root)) / Path(*parsed.parts)
    assert_no_reparse_points(result)
    return result


def verify_asset(path: Path, size_bytes: int, sha256: str) -> None:
    """核对普通文件的大小和 SHA256，不能用同名文件充当固定资产。

    Args:
        path: 已存在的待核验文件。
        size_bytes: 清单固定的字节数。
        sha256: 清单固定的 SHA256。

    Returns:
        无；文件缺失、大小错误或摘要错误时抛出 ValueError。
    """
    assert_no_reparse_points(path)
    if not path.is_file() or path.stat().st_size != size_bytes:
        raise ValueError(f"构建资产不存在或大小不匹配：{path}")
    with path.open("rb") as source:
        actual = hashlib.file_digest(source, "sha256").hexdigest()
    if actual != sha256:
        raise ValueError(f"构建资产 SHA256 不匹配：{path}")


def copy_verified_asset(source: Path, destination: Path, size_bytes: int, sha256: str) -> None:
    """完整核验来源，再以临时文件复制，避免中断留下半个正式资产。

    Args:
        source: 只读来源文件。
        destination: 当前构建拥有的目标文件。
        size_bytes: 固定字节数。
        sha256: 固定 SHA256。

    Returns:
        无；成功后目标文件与固定摘要完全相同。
    """
    verify_asset(source, size_bytes, sha256)
    assert_no_reparse_points(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f"{destination.name}.{uuid4()}.part")
    try:
        shutil.copyfile(source, temporary)
        verify_asset(temporary, size_bytes, sha256)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def download_fixed_asset(urls: list[str], destination: Path, size_bytes: int, sha256: str) -> Path:
    """下载公开固定文件，只在大小和摘要全部通过后登记为缓存。

    Args:
        urls: 按优先顺序尝试的公开 HTTPS 地址。
        destination: 以固定摘要区分的本地缓存文件。
        size_bytes: 固定字节数。
        sha256: 固定 SHA256。

    Returns:
        完成核验的缓存路径；全部地址失败时抛出 RuntimeError。
    """
    assert_no_reparse_points(destination)
    if destination.exists():
        verify_asset(destination, size_bytes, sha256)
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    failures: list[str] = []
    for url in urls:
        if not url.startswith("https://"):
            raise ValueError(f"构建资产必须来自公开 HTTPS 地址：{url}")
        temporary = destination.with_name(f"{destination.name}.{uuid4()}.part")
        try:
            request = Request(url, headers={"User-Agent": "NearMate-build/0.2.0"})
            with urlopen(request, timeout=120) as response, temporary.open("xb") as output:
                shutil.copyfileobj(response, output, length=1024 * 1024)
            verify_asset(temporary, size_bytes, sha256)
            temporary.replace(destination)
            return destination
        except (OSError, ValueError) as error:
            failures.append(f"{url}: {error}")
        finally:
            temporary.unlink(missing_ok=True)
    raise RuntimeError("公开构建资产下载失败：\n" + "\n".join(failures))
