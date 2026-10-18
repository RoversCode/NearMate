"""按公开固定清单准备实时输入模型。"""

from __future__ import annotations

import json
from pathlib import Path
import re
import tarfile
from uuid import uuid4

from .control import CancellationCheck, ProgressCallback, ProgressReporter, check_cancelled
from .io import (
    assert_no_reparse_points, copy_stream, copy_verified_asset, download_fixed_asset,
    keep_valid_asset, safe_asset_path, verify_asset,
)
from .locking import asset_lock


def prepare_realtime_assets(
    manifest_path: Path, destination: Path, cache_root: Path, source_root: Path | None = None, *,
    progress: ProgressCallback | None = None, is_cancelled: CancellationCheck | None = None,
) -> None:
    """下载同版本模型，镜像不可用时复用固定归档，保留模型原有精度。

    Args:
        manifest_path: 登记固定版本、公开来源和摘要的清单。
        destination: 当前 checkout 的模型目录。
        cache_root: 以摘要区分的下载缓存目录。
        source_root: 可选只读旧缓存；只复用逐文件完全匹配的内容。
        progress: 接收安全中文阶段与下载进度的回调。
        is_cancelled: 等锁、下载、解压、校验和复制时检查的取消信号。

    Returns:
        无；取消、非法路径或准备失败时抛出异常。
    """
    check_cancelled(is_cancelled)
    assert_no_reparse_points(manifest_path)
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema_version") != 1 or not isinstance(payload.get("assets"), list) or not payload["assets"]:
        raise ValueError("实时模型清单缺少 schema_version=1 或非空 assets")
    assert_no_reparse_points(destination)
    assert_no_reparse_points(cache_root)
    if source_root is not None:
        assert_no_reparse_points(source_root)

    # 写入之前先验证整份路径和摘要合同，不能下载一半才发现清单包含越界位置。
    seen: set[str] = set()
    for record in payload["assets"]:
        check_cancelled(is_cancelled)
        if not isinstance(record, dict):
            raise ValueError("实时模型清单必须包含文件记录")
        relative = record.get("path", "")
        safe_asset_path(destination, relative)
        if relative.casefold() in seen or not isinstance(record.get("sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", record["sha256"]):
            raise ValueError(f"实时模型清单路径重复或摘要无效：{relative}")
        if type(record.get("size_bytes")) is not int or record["size_bytes"] <= 0:
            raise ValueError(f"实时模型清单字节数无效：{relative}")
        urls = record.get("urls", [])
        if not isinstance(urls, list) or any(not isinstance(url, str) or not url.startswith("https://") for url in urls):
            raise ValueError(f"实时模型清单缺少公开来源：{relative}")
        archive = record.get("archive")
        if archive is not None:
            if not isinstance(archive, dict):
                raise ValueError(f"实时模型归档记录无效：{relative}")
            safe_asset_path(cache_root, archive.get("filename", ""))
            safe_asset_path(cache_root, archive.get("member", ""))
            if (
                type(archive.get("size_bytes")) is not int or archive["size_bytes"] <= 0
                or not isinstance(archive.get("sha256"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", archive["sha256"])
                or not isinstance(archive.get("url"), str) or not archive["url"].startswith("https://")
            ):
                raise ValueError(f"实时模型归档缺少固定大小、摘要或公开来源：{relative}")
        elif not urls:
            raise ValueError(f"实时模型清单缺少公开来源：{relative}")
        seen.add(relative.casefold())

    reporter = ProgressReporter(progress)
    with asset_lock(destination.parent / f".{destination.name}.runtime-assets.lock", is_cancelled):
        reporter.report("开始检查实时语音资源", force=True)
        for record in payload["assets"]:
            check_cancelled(is_cancelled)
            relative, size, digest = record["path"], record["size_bytes"], record["sha256"]
            target = safe_asset_path(destination, relative)
            reporter.report(f"正在检查：{relative}")
            if keep_valid_asset(target, size, digest, is_cancelled=is_cancelled):
                continue
            if source_root is not None:
                source = safe_asset_path(source_root, relative)
                if source.is_file():
                    try:
                        verify_asset(source, size, digest, is_cancelled=is_cancelled)
                    except ValueError:
                        pass
                    else:
                        copy_verified_asset(source, target, size, digest, is_cancelled=is_cancelled)
                        reporter.report(f"已复用固定模型缓存：{relative}", force=True)
                        continue

            # 优先使用逐文件镜像；只有全部地址下载失败，才允许改用清单登记的固定归档。
            urls = record.get("urls", [])
            archive = record.get("archive")
            if urls:
                cached = safe_asset_path(cache_root, f"{digest}/{Path(relative).name}")
                try:
                    download_fixed_asset(urls, cached, size, digest, progress=progress,
                                         is_cancelled=is_cancelled, relative_name=relative)
                except RuntimeError as error:
                    # 下载器保留历史 RuntimeError 合同；精确识别耗尽地址的结果，取消和其它错误原样上抛。
                    if type(error) is not RuntimeError or error.args != (f"公开构建资产下载失败：{relative}",) or archive is None:
                        raise
                    check_cancelled(is_cancelled)
                    reporter.report(f"镜像暂不可用，正在准备固定归档：{relative}", force=True)
                else:
                    copy_verified_asset(cached, target, size, digest, is_cancelled=is_cancelled)
                    reporter.report(f"资源准备完成：{relative}", force=True)
                    continue

            cached = safe_asset_path(cache_root, f"{archive['sha256']}/{archive['filename']}")
            if source_root is not None and not cached.exists():
                source_archive = safe_asset_path(source_root, archive["filename"])
                if source_archive.is_file():
                    try:
                        verify_asset(source_archive, archive["size_bytes"], archive["sha256"], is_cancelled=is_cancelled)
                    except ValueError:
                        pass
                    else:
                        copy_verified_asset(source_archive, cached, archive["size_bytes"], archive["sha256"], is_cancelled=is_cancelled)
            download_fixed_asset([archive["url"]], cached, archive["size_bytes"], archive["sha256"],
                                 progress=progress, is_cancelled=is_cancelled, relative_name=relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(f"{target.name}.{uuid4().hex}.part")
            try:
                # 只读取清单点名的普通成员，不调用 extractall，归档中的链接和额外文件没有写入资格。
                with tarfile.open(cached, "r:bz2") as package:
                    member = package.getmember(archive["member"])
                    if not member.isfile() or member.size != size:
                        raise ValueError(f"实时模型归档成员不是预期的普通文件：{relative}")
                    with package.extractfile(member) as source, temporary.open("xb") as output:
                        copy_stream(source, output, size_bytes=size, is_cancelled=is_cancelled,
                                    reporter=reporter, relative_name=relative)
                verify_asset(temporary, size, digest, is_cancelled=is_cancelled)
                check_cancelled(is_cancelled)
                assert_no_reparse_points(target)
                temporary.replace(target)
            finally:
                temporary.unlink(missing_ok=True)
            reporter.report(f"资源准备完成：{relative}", force=True)
        reporter.report("实时语音资源已准备完成", force=True)
