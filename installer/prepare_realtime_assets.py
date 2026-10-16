"""按公开固定清单准备实时输入模型；只写当前 checkout 的 backend/ckpt。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import shutil
import tarfile
from uuid import uuid4

if __package__:
    from .runtime_asset_io import (
        assert_no_reparse_points, copy_verified_asset, download_fixed_asset,
        safe_asset_path, verify_asset,
    )
else:
    from runtime_asset_io import (
        assert_no_reparse_points, copy_verified_asset, download_fixed_asset,
        safe_asset_path, verify_asset,
    )


def prepare_realtime_assets(
    manifest_path: Path,
    destination: Path,
    cache_root: Path,
    source_root: Path | None = None,
) -> None:
    """下载同版本模型，或从已经核验的本地缓存复制，保留模型原有精度。

    Args:
        manifest_path: 仓库登记的固定版本、公开来源和摘要清单。
        destination: 当前 checkout 的模型目录。
        cache_root: 以摘要区分的下载缓存目录。
        source_root: 可选只读旧缓存；必须逐文件匹配清单，不能成为隐式依赖。

    Returns:
        无；缺失、损坏、越界或下载失败时抛出异常并停止构建。
    """
    assert_no_reparse_points(manifest_path)
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1 or not isinstance(payload.get("assets"), list) or not payload["assets"]:
        raise ValueError("实时模型清单缺少 schema_version=1 或非空 assets")
    assert_no_reparse_points(destination)
    assert_no_reparse_points(cache_root)
    if source_root is not None:
        assert_no_reparse_points(source_root)

    # 写入之前先验证整份路径和摘要合同，不能下载一半才发现清单包含越界位置。
    seen: set[str] = set()
    for record in payload["assets"]:
        relative = record.get("path", "")
        safe_asset_path(destination, relative)
        if relative.casefold() in seen or not re.fullmatch(r"[0-9a-f]{64}", record.get("sha256", "")):
            raise ValueError(f"实时模型清单路径重复或摘要无效：{relative}")
        if type(record.get("size_bytes")) is not int or record["size_bytes"] <= 0:
            raise ValueError(f"实时模型清单字节数无效：{relative}")
        archive = record.get("archive")
        if archive is not None:
            safe_asset_path(cache_root, archive["filename"])
            safe_asset_path(cache_root, archive["member"])
            if (
                type(archive.get("size_bytes")) is not int or archive["size_bytes"] <= 0
                or not re.fullmatch(r"[0-9a-f]{64}", archive.get("sha256", ""))
                or not archive.get("url", "").startswith("https://")
            ):
                raise ValueError(f"实时模型归档缺少固定大小、摘要或公开来源：{relative}")
        elif (
            not isinstance(record.get("urls"), list) or not record["urls"]
            or any(not isinstance(url, str) or not url.startswith("https://") for url in record["urls"])
        ):
            raise ValueError(f"实时模型清单缺少公开来源：{relative}")
        seen.add(relative.casefold())

    for record in payload["assets"]:
        relative, size, digest = record["path"], record["size_bytes"], record["sha256"]
        target = safe_asset_path(destination, relative)
        if target.exists():
            verify_asset(target, size, digest)
            print(f"已验证实时模型：{relative}", flush=True)
            continue
        if source_root is not None:
            source = safe_asset_path(source_root, relative)
            if source.is_file():
                try:
                    verify_asset(source, size, digest)
                except ValueError:
                    pass
                else:
                    copy_verified_asset(source, target, size, digest)
                    print(f"已复用固定模型缓存：{relative}", flush=True)
                    continue

        archive = record.get("archive")
        if archive is None:
            cached = safe_asset_path(cache_root, f"{digest}/{Path(relative).name}")
            download_fixed_asset(record["urls"], cached, size, digest)
            copy_verified_asset(cached, target, size, digest)
            print(f"已下载固定实时模型：{relative}", flush=True)
            continue

        cached = safe_asset_path(cache_root, f"{archive['sha256']}/{archive['filename']}")
        if source_root is not None and not cached.exists():
            source_archive = safe_asset_path(source_root, archive["filename"])
            if source_archive.is_file():
                try:
                    verify_asset(source_archive, archive["size_bytes"], archive["sha256"])
                except ValueError:
                    pass
                else:
                    copy_verified_asset(source_archive, cached, archive["size_bytes"], archive["sha256"])
        download_fixed_asset([archive["url"]], cached, archive["size_bytes"], archive["sha256"])
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f"{target.name}.{uuid4()}.part")
        try:
            # 只读取清单点名的普通成员，不调用 extractall，归档中的链接和额外文件没有写入资格。
            with tarfile.open(cached, "r:bz2") as package:
                member = package.getmember(archive["member"])
                if not member.isfile() or member.size != size:
                    raise ValueError(f"实时模型归档成员不是预期的普通文件：{relative}")
                with package.extractfile(member) as source, temporary.open("xb") as output:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
            verify_asset(temporary, size, digest)
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
        print(f"已从固定公开归档准备：{relative}", flush=True)


if __name__ == "__main__":
    repository_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=Path(__file__).with_name("realtime-assets.json"))
    parser.add_argument("--destination", type=Path, default=repository_root / "backend/ckpt")
    parser.add_argument("--cache-root", type=Path, default=repository_root / ".artifacts/realtime-assets")
    parser.add_argument("--source-root", type=Path)
    arguments = parser.parse_args()
    prepare_realtime_assets(arguments.manifest, arguments.destination, arguments.cache_root, arguments.source_root)
