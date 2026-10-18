"""固定资产的安全路径、分块校验、损坏备份与原子下载。"""

from __future__ import annotations

import hashlib
from http.client import HTTPException
import os
from pathlib import Path, PurePosixPath
import re
import stat
from typing import BinaryIO
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4

from .control import CancellationCheck, ProgressCallback, ProgressReporter, check_cancelled


CHUNK_SIZE = 1024 * 1024


def assert_no_reparse_points(path: Path) -> None:
    """拒绝路径自身及父目录中的链接，避免写入其它目录。

    Args:
        path: 准备读取或写入的路径；不存在的尾部目录允许稍后创建。

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
            raise ValueError("构建资产路径不能经过重解析点")


def safe_asset_path(root: Path, relative: str) -> Path:
    """把清单中的安全相对路径放到指定根目录中。

    Args:
        root: 当前操作拥有的资产根目录。
        relative: 清单中的正斜杠相对路径。

    Returns:
        没有越界或重解析点的绝对路径。
    """
    if not isinstance(relative, str):
        raise ValueError("构建资产必须使用安全相对路径")
    parsed = PurePosixPath(relative)
    segments = relative.split("/")
    if (
        not relative or "\\" in relative or ":" in relative
        or parsed.is_absolute() or any(part in ("", ".", "..") for part in segments)
        or any(character in '<>"|?*' or ord(character) < 32 or ord(character) == 127 for character in relative)
        or any(part.endswith((" ", ".")) for part in segments)
        or any(re.fullmatch(r"(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])", part.split(".")[0], re.I) for part in segments)
    ):
        raise ValueError("构建资产必须使用安全相对路径")
    result = Path(os.path.abspath(root)) / Path(*parsed.parts)
    assert_no_reparse_points(result)
    return result


def verify_asset(
    path: Path, size_bytes: int, sha256: str, *, is_cancelled: CancellationCheck | None = None,
) -> None:
    """分块核对普通文件的大小和 SHA256，校验中也能取消。

    Args:
        path: 已存在的待核验文件。
        size_bytes: 清单固定的字节数。
        sha256: 清单固定的 SHA256。
        is_cancelled: 每次读取前检查的取消信号。

    Returns:
        无；缺失、大小错误或摘要错误时抛出 ValueError。
    """
    check_cancelled(is_cancelled)
    assert_no_reparse_points(path)
    if not path.is_file() or path.stat().st_size != size_bytes:
        raise ValueError(f"构建资产不存在或大小不匹配：{path.name}")
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while True:
            check_cancelled(is_cancelled)
            chunk = source.read(CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
    if digest.hexdigest() != sha256:
        raise ValueError(f"构建资产 SHA256 不匹配：{path.name}")


def keep_valid_asset(
    path: Path, size_bytes: int, sha256: str, *, is_cancelled: CancellationCheck | None = None,
) -> bool:
    """复用完整文件，把损坏普通文件移到同目录独立备份。

    Args:
        path: 清单点名的目标或下载缓存。
        size_bytes: 清单固定的字节数。
        sha256: 清单固定的 SHA256。
        is_cancelled: 校验与备份前检查的取消信号。

    Returns:
        完整文件返回真；缺失或已备份的损坏文件返回假。
    """
    check_cancelled(is_cancelled)
    assert_no_reparse_points(path)
    if not path.exists():
        return False
    if not path.is_file():
        raise ValueError(f"清单资产位置必须是普通文件：{path.name}")
    try:
        verify_asset(path, size_bytes, sha256, is_cancelled=is_cancelled)
    except ValueError:
        check_cancelled(is_cancelled)
        assert_no_reparse_points(path)
        path.rename(path.with_name(f"{path.name}.{uuid4().hex}.backup"))
        return False
    return True


def copy_stream(
    source: BinaryIO, output: BinaryIO, *, size_bytes: int,
    is_cancelled: CancellationCheck | None = None,
    reporter: ProgressReporter | None = None, relative_name: str = "资源文件",
) -> None:
    """分块复制数据，拒绝超过清单大小的响应并及时响应取消。

    Args:
        source: 已打开的只读流。
        output: 本次准备拥有的临时文件流。
        size_bytes: 清单允许的最大字节数。
        is_cancelled: 每个数据块前后检查的取消信号。
        reporter: 已限频的进度报告器。
        relative_name: 已校验的清单相对文件名。

    Returns:
        无；取消、数据超长或 HTTP 协议错误时抛出异常。
    """
    copied = 0
    # HTTPResponse.read 会等满指定字节；read1 每次只做一次底层读取，慢连接也能及时检查取消。
    read_chunk = getattr(source, "read1", source.read)
    while True:
        check_cancelled(is_cancelled)
        try:
            chunk = read_chunk(min(CHUNK_SIZE, size_bytes - copied + 1))
        except HTTPException:
            # 分块截断等协议错误不是 OSError；只转换底层读取，避免把进度回调异常当成断网。
            check_cancelled(is_cancelled)
            raise OSError("资源响应协议无效或读取中断") from None
        check_cancelled(is_cancelled)
        if not chunk:
            return
        copied += len(chunk)
        if copied > size_bytes:
            raise ValueError("资源响应超过固定字节数")
        output.write(chunk)
        if reporter is not None:
            reporter.report(f"正在准备：{relative_name}（{copied * 100 // size_bytes}%）")


def copy_verified_asset(
    source: Path, destination: Path, size_bytes: int, sha256: str, *,
    is_cancelled: CancellationCheck | None = None,
) -> None:
    """完整核验来源，再以临时文件复制，避免留下半个正式资产。

    Args:
        source: 只读来源文件。
        destination: 当前准备拥有的清单目标文件。
        size_bytes: 固定字节数。
        sha256: 固定 SHA256。
        is_cancelled: 校验、复制和提交前检查的取消信号。

    Returns:
        无；成功后目标文件与固定摘要完全相同。
    """
    verify_asset(source, size_bytes, sha256, is_cancelled=is_cancelled)
    if keep_valid_asset(destination, size_bytes, sha256, is_cancelled=is_cancelled):
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f"{destination.name}.{uuid4().hex}.part")
    try:
        with source.open("rb") as input_file, temporary.open("xb") as output:
            copy_stream(input_file, output, size_bytes=size_bytes, is_cancelled=is_cancelled)
        verify_asset(temporary, size_bytes, sha256, is_cancelled=is_cancelled)
        check_cancelled(is_cancelled)
        assert_no_reparse_points(destination)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def download_fixed_asset(
    urls: list[str], destination: Path, size_bytes: int, sha256: str, *,
    progress: ProgressCallback | None = None, is_cancelled: CancellationCheck | None = None,
    relative_name: str | None = None,
) -> Path:
    """下载公开固定文件，缓存完整时不联网，损坏时保留备份。

    Args:
        urls: 按优先顺序尝试的公开 HTTPS 地址。
        destination: 以固定摘要区分的下载缓存文件。
        size_bytes: 固定字节数。
        sha256: 固定 SHA256。
        progress: 接收安全中文进度的回调。
        is_cancelled: 等锁、下载、校验和提交时检查的取消信号。
        relative_name: 已验证的清单相对文件名；省略时使用缓存文件名。

    Returns:
        通过固定大小与摘要核验的缓存文件路径；网络或协议失败时尝试下一地址。
    """
    from .locking import asset_lock

    if type(size_bytes) is not int or size_bytes <= 0 or not isinstance(sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise ValueError("构建资产必须登记固定字节数和 SHA256")
    if not urls or any(not isinstance(url, str) or not url.startswith("https://") for url in urls):
        raise ValueError("构建资产必须来自公开 HTTPS 地址")
    name = relative_name or destination.name
    safe_asset_path(destination.parent, name)
    reporter = ProgressReporter(progress)
    with asset_lock(destination.with_name(f".{destination.name}.lock"), is_cancelled):
        if keep_valid_asset(destination, size_bytes, sha256, is_cancelled=is_cancelled):
            return destination
        destination.parent.mkdir(parents=True, exist_ok=True)
        reporter.report(f"开始下载：{name}", force=True)
        for url in urls:
            check_cancelled(is_cancelled)
            temporary = destination.with_name(f"{destination.name}.{uuid4().hex}.part")
            try:
                request = Request(url, headers={"User-Agent": "NearMate-assets/0.2.0"})
                # 单次阻塞读有上限；每块检查取消，退出 with 时立即关闭连接与临时文件。
                try:
                    response = urlopen(request, timeout=5)
                except HTTPException:
                    # 损坏响应头同样属于来源不可用，不能跳过其余固定来源直接终止准备。
                    check_cancelled(is_cancelled)
                    raise OSError("资源响应协议无效或读取中断") from None
                with response, temporary.open("xb") as output:
                    copy_stream(response, output, size_bytes=size_bytes, is_cancelled=is_cancelled,
                                reporter=reporter, relative_name=name)
                verify_asset(temporary, size_bytes, sha256, is_cancelled=is_cancelled)
                check_cancelled(is_cancelled)
                assert_no_reparse_points(destination)
                temporary.replace(destination)
                reporter.report(f"下载完成：{name}", force=True)
                return destination
            except (OSError, ValueError) as error:
                # 错误正文可能包含签名 URL 或服务端凭据，只公开安全的清单文件名。
                if isinstance(error, HTTPError):
                    error.close()
                check_cancelled(is_cancelled)
            finally:
                temporary.unlink(missing_ok=True)
    raise RuntimeError(f"公开构建资产下载失败：{name}")
