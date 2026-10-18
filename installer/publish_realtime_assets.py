"""核验并发布实时模型白名单，不扫描或上传完整的本机模型目录。"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, build_opener

if __package__:
    from .prepare_realtime_assets import prepare_realtime_assets
    from .runtime_asset_io import assert_no_reparse_points, copy_verified_asset, safe_asset_path, verify_asset
else:
    from prepare_realtime_assets import prepare_realtime_assets
    from runtime_asset_io import assert_no_reparse_points, copy_verified_asset, safe_asset_path, verify_asset


def _file_record(path: Path, relative: str) -> dict[str, Any]:
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    return {"path": relative, "size_bytes": path.stat().st_size, "sha256": digest}


def _write_json(path: Path, payload: Any) -> None:
    content = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if path.exists() and path.read_text(encoding="utf-8") != content:
        raise ValueError(f"发布目录已有不同内容，不能覆盖：{path.name}")
    path.write_text(content, encoding="utf-8", newline="\n")


def prepare_publication(
    manifest_path: Path,
    source_root: Path,
    staging: Path,
    notices_root: Path,
) -> dict[str, Any]:
    """只复制清单点名的模型与已经审查的原许可，生成可复核的发布包。

    Args:
        manifest_path: 固定模型路径、大小和摘要的公开清单。
        source_root: 只读本地模型缓存；错误版本只能转用公开固定来源。
        staging: 本次发布独占的输出目录。
        notices_root: 含 sources.json、许可原文和来源说明的审查目录。

    Returns:
        发布文件的完整路径白名单与摘要，不包含本机绝对路径。
    """
    for path in (manifest_path, source_root, staging, notices_root):
        assert_no_reparse_points(path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    sources_path = safe_asset_path(notices_root, "sources.json")
    sources = json.loads(sources_path.read_text(encoding="utf-8"))
    assets = manifest.get("assets", [])
    if manifest.get("schema_version") != 1 or not assets:
        raise ValueError("发布清单必须包含 schema_version=1 和非空 assets")
    if sources.get("schema_version") != 1 or not sources.get("groups") or not sources.get("files"):
        raise ValueError("发布前必须提供完整的许可和来源审查记录")

    # 发布前先审查整份白名单；不能复制完模型后才发现缺少许可或路径越界。
    asset_paths: set[str] = set()
    for asset in assets:
        relative = asset["path"]
        safe_asset_path(staging, f"ckpt/{relative}")
        if relative.casefold() in asset_paths or asset.get("license") not in {"Apache-2.0", "MIT", "BSD-2-Clause"}:
            raise ValueError(f"模型路径重复或尚未审查再分发许可：{relative}")
        asset_paths.add(relative.casefold())
    covered_paths: set[str] = set()
    notice_paths = {record["path"] for record in sources["files"]}
    if len(notice_paths) != len(sources["files"]):
        raise ValueError("许可文件路径不能重复")
    for group in sources["groups"]:
        if not group.get("attribution") or not group.get("source_urls") or not group.get("license_files"):
            raise ValueError("每组模型都必须保留归属、公开来源和许可原文")
        if any(not url.startswith("https://") for url in group["source_urls"]):
            raise ValueError("模型来源必须是公开 HTTPS 地址")
        if not set(group["license_files"]).issubset(notice_paths):
            raise ValueError("来源声明引用了缺失的许可文件")
        for relative in group["assets"]:
            if relative.casefold() in covered_paths:
                raise ValueError(f"模型的许可归属重复：{relative}")
            covered_paths.add(relative.casefold())
    if covered_paths != asset_paths:
        raise ValueError("许可审查记录必须恰好覆盖本次发布的全部模型")
    expected_paths = {f"ckpt/{asset['path']}" for asset in assets}
    expected_paths.update({"runtime-assets.json", "sources.json", "publication-manifest.json"})
    for record in sources["files"]:
        relative = record["path"]
        if not relative.startswith(("licenses/", "model-cards/")) and relative != "SOURCES.md":
            raise ValueError(f"来源目录只接受许可原文和公开说明：{relative}")
        source = safe_asset_path(notices_root, relative)
        safe_asset_path(staging, relative)
        verify_asset(source, record["size_bytes"], record["sha256"])
        expected_paths.add(relative)
    if staging.exists():
        for path in staging.rglob("*"):
            assert_no_reparse_points(path)
            if path.is_file() and path.relative_to(staging).as_posix() not in expected_paths:
                raise ValueError("发布目录含白名单以外的文件，不能上传")

    staging.mkdir(parents=True, exist_ok=True)
    # 准备器会保留跨进程锁文件；把它放在发布目录外，上传包只接收已核验的模型。
    prepared_models = staging.parent / f"prepared-{staging.name}"
    prepare_realtime_assets(manifest_path, prepared_models, staging.parent / "download-cache", source_root)
    for asset in assets:
        target = safe_asset_path(staging / "ckpt", asset["path"])
        if target.exists():
            verify_asset(target, asset["size_bytes"], asset["sha256"])
            continue
        copy_verified_asset(safe_asset_path(prepared_models, asset["path"]), target, asset["size_bytes"], asset["sha256"])
    for record in sources["files"]:
        target = safe_asset_path(staging, record["path"])
        if target.exists():
            verify_asset(target, record["size_bytes"], record["sha256"])
            continue
        copy_verified_asset(safe_asset_path(notices_root, record["path"]), target, record["size_bytes"], record["sha256"])
    _write_json(staging / "runtime-assets.json", manifest)
    _write_json(staging / "sources.json", sources)
    files = [_file_record(safe_asset_path(staging, relative), relative) for relative in sorted(expected_paths - {"publication-manifest.json"})]
    publication = {"schema_version": 1, "asset_count": len(assets), "files": files}
    _write_json(staging / "publication-manifest.json", publication)
    return publication


def modelscope_asset_url(repo_id: str, revision: str, relative: str) -> str:
    """按 ModelScope SDK 的文件接口生成固定提交下载地址。

    Args:
        repo_id: ModelScope 的 owner/name 仓库标识。
        revision: 上传结果返回的不可变 Git 提交摘要。
        relative: 已核验的仓库相对文件路径。

    Returns:
        与 SDK get_file_download_url 合同一致的公开 HTTPS 下载地址。
    """
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo_id) or not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("下载地址必须使用合法仓库和完整不可变提交摘要")
    safe_asset_path(Path.cwd(), relative)
    return f"https://www.modelscope.cn/api/v1/models/{repo_id}/repo?" + urlencode({"Revision": revision, "FilePath": relative})


def read_public_model_files(repo_id: str, revision: str) -> list[dict[str, Any]]:
    """用独立的无认证 HTTP 客户端读取公开文件元数据。

    Args:
        repo_id: 公开 ModelScope 仓库的 owner/name 标识。
        revision: 不可变提交摘要，或仅用于定位发布回执的分支名称。

    Returns:
        公开文件 API 返回的文件路径、大小和 SHA256 记录。
    """
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo_id) or not re.fullmatch(r"[A-Za-z0-9_.-]+", revision) or revision in {".", ".."}:
        raise ValueError("匿名元数据查询必须指定合法仓库和版本")
    url = f"https://www.modelscope.cn/api/v1/models/{repo_id}/repo/files?" + urlencode({"Revision": revision, "Recursive": "True"})
    # SDK 的 use_cookies=False 仍可能读取本机登录；新 opener 不安装 Cookie 或认证处理器。
    request = Request(url, headers={"User-Agent": "NearMate-asset-verification/0.2.0"})
    with build_opener().open(request, timeout=120) as response:
        payload = json.load(response)
    if payload.get("Code") != 200 or not isinstance(payload.get("Data", {}).get("Files"), list):
        raise RuntimeError("ModelScope 公开文件 API 未返回有效元数据")
    return payload["Data"]["Files"]


def publish_package(staging: Path, repo_id: str, revision: str = "master", api: Any = None) -> dict[str, Any]:
    """用官方 SDK 上传完整核验包，并匿名验证提交后的模型元数据。

    Args:
        staging: prepare_publication 生成的专用发布目录。
        repo_id: 用户授权的 ModelScope 目标仓库。
        revision: SDK 写入的分支名称；返回值始终固定到实际提交摘要。
        api: 可选官方 HubApi 实例，供测试替换网络边界。

    Returns:
        不可变提交摘要及每个模型的固定公开下载 URL。
    """
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo_id):
        raise ValueError("ModelScope 仓库必须使用 owner/name")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", revision) or revision in {".", ".."}:
        raise ValueError("发布分支名称无效")
    publication_path = safe_asset_path(staging, "publication-manifest.json")
    publication = json.loads(publication_path.read_text(encoding="utf-8"))
    records = publication["files"] + [_file_record(publication_path, "publication-manifest.json")]
    planned_paths = {record["path"] for record in records}
    for path in staging.rglob("*"):
        assert_no_reparse_points(path)
        if path.is_file() and path.relative_to(staging).as_posix() not in planned_paths:
            raise ValueError("发布目录在审查后增加了文件，拒绝上传")
    for record in records:
        verify_asset(safe_asset_path(staging, record["path"]), record["size_bytes"], record["sha256"])
    if api is None:
        from modelscope.hub.api import HubApi, ModelScopeConfig

        if not ModelScopeConfig.get_cookies():
            raise RuntimeError("ModelScope SDK 尚未登录；请在本机通过 HubApi.login 完成认证")
        api = HubApi()
    if api.endpoint != "https://www.modelscope.cn":
        raise ValueError("发布仅允许官方 ModelScope HTTPS 端点")

    # 目标仓库已有不同的同名文件时停止，避免把别的版本悄悄覆盖为当前模型。
    remote = api.get_model_files(repo_id, revision=revision, recursive=True, use_cookies=True)
    remote_by_path = {record["Path"]: record for record in remote}
    for record in records:
        previous = remote_by_path.get(record["path"])
        if previous and (previous.get("Size") != record["size_bytes"] or previous.get("Sha256") != record["sha256"]):
            raise ValueError(f"ModelScope 存在不同内容的同名文件：{record['path']}")
    result = api.upload_folder(
        repo_id=repo_id, folder_path=str(staging), revision=revision,
        commit_message="Publish verified NearMate realtime runtime assets",
        allow_patterns=sorted(planned_paths), max_workers=2,
    )
    commit = result[-1] if isinstance(result, list) else result
    if commit.oid and not re.fullmatch(r"[0-9a-f]{40}", commit.oid):
        raise ValueError("ModelScope 未返回不可变提交摘要")
    return verify_published_package(staging, repo_id, commit.oid or revision)


def verify_published_package(staging: Path, repo_id: str, revision: str) -> dict[str, Any]:
    """用发布回执定位实际提交，并匿名核对完整包，支持上传后的独立恢复验证。

    Args:
        staging: 保存已核验模型和 publication-manifest.json 的发布目录。
        repo_id: 用户授权发布的公开 ModelScope 仓库。
        revision: SDK 返回的提交摘要；SDK 未提供摘要时可传入原写入分支。

    Returns:
        通过完整匿名摘要核验的不可变提交和模型公开下载地址。
    """
    publication_path = safe_asset_path(staging, "publication-manifest.json")
    publication = json.loads(publication_path.read_text(encoding="utf-8"))
    receipt = _file_record(publication_path, "publication-manifest.json")
    records = publication["files"] + [receipt]
    for record in records:
        verify_asset(safe_asset_path(staging, record["path"]), record["size_bytes"], record["sha256"])
    commit_id = revision
    if not re.fullmatch(r"[0-9a-f]{40}", commit_id):
        # SDK 1.30 可能上传成功却返回空 oid。只接受与本地发布回执大小、摘要均相同的文件版本。
        branch_files = read_public_model_files(repo_id, revision)
        remote_receipt = next((record for record in branch_files if record["Path"] == receipt["path"]), {})
        if remote_receipt.get("Size") != receipt["size_bytes"] or remote_receipt.get("Sha256") != receipt["sha256"]:
            raise RuntimeError("远程发布回执与本地核验包不同，不能从分支推断提交")
        commit_id = remote_receipt.get("Revision", "")
        if not re.fullmatch(r"[0-9a-f]{40}", commit_id):
            raise RuntimeError("远程发布回执没有提供不可变文件版本")
    public_files = read_public_model_files(repo_id, commit_id)
    public_by_path = {record["Path"]: record for record in public_files}
    for record in records:
        remote_record = public_by_path.get(record["path"], {})
        if remote_record.get("Size") != record["size_bytes"] or remote_record.get("Sha256") != record["sha256"]:
            raise RuntimeError(f"匿名元数据核验失败：{record['path']}")
    assets = json.loads((staging / "runtime-assets.json").read_text(encoding="utf-8"))["assets"]
    return {"repo_id": repo_id, "revision": commit_id, "asset_urls": {asset["path"]: modelscope_asset_url(repo_id, commit_id, f"ckpt/{asset['path']}") for asset in assets}}


def verify_public_downloads(staging: Path, publication: dict[str, Any]) -> None:
    """匿名完整下载一个小文件，并检查大模型的 HTTP Range 可用性。

    Args:
        staging: 保存全部已核验模型的发布目录。
        publication: publish_package 返回的固定提交和下载地址。

    Returns:
        无；下载内容、范围响应或摘要不匹配时抛出异常。
    """
    assets = json.loads((staging / "runtime-assets.json").read_text(encoding="utf-8"))["assets"]
    smallest = min(assets, key=lambda record: record["size_bytes"])
    largest = max(assets, key=lambda record: record["size_bytes"])
    # 与元数据检查一样，不复用 SDK session，也不使用可能被外部安装了 CookieProcessor 的全局 opener。
    opener = build_opener()
    with opener.open(Request(publication["asset_urls"][smallest["path"]]), timeout=120) as response:
        content = response.read(smallest["size_bytes"] + 1)
    if len(content) != smallest["size_bytes"] or hashlib.sha256(content).hexdigest() != smallest["sha256"]:
        raise RuntimeError("匿名小文件下载摘要不匹配")
    request = Request(publication["asset_urls"][largest["path"]], headers={"Range": "bytes=0-1023"})
    with opener.open(request, timeout=120) as response:
        if response.status != 206 or response.headers.get("Content-Range") != f"bytes 0-1023/{largest['size_bytes']}":
            raise RuntimeError("匿名大文件不支持预期的 HTTP Range 响应")
        content = response.read(1025)
    with safe_asset_path(staging, f"ckpt/{largest['path']}").open("rb") as stream:
        if content != stream.read(1024):
            raise RuntimeError("匿名大文件范围内容与本地核验包不同")


def main() -> None:
    """先准备可审查的发布包，仅在指定 --publish 时访问远程写接口。

    Args:
        无；参数由命令行提供。

    Returns:
        无；失败时通过异常使进程非零退出。
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=Path(__file__).with_name("realtime-assets.json"))
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--staging", type=Path, required=True)
    parser.add_argument("--notices-root", type=Path, required=True)
    parser.add_argument("--repo-id", default="ChengHee/NearMate")
    parser.add_argument("--revision", default="master")
    parser.add_argument("--publish", action="store_true")
    arguments = parser.parse_args()
    package = prepare_publication(arguments.manifest, arguments.source_root, arguments.staging, arguments.notices_root)
    print(f"发布包已完整核验：{package['asset_count']} 个模型，{len(package['files']) + 1} 个白名单文件", flush=True)
    if not arguments.publish:
        return
    result = publish_package(arguments.staging, arguments.repo_id, arguments.revision)
    verify_public_downloads(arguments.staging, result)
    report = arguments.staging.parent / "publication-result.json"
    report.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"ModelScope 匿名验证通过，固定提交：{result['revision']}", flush=True)


if __name__ == "__main__":
    main()
