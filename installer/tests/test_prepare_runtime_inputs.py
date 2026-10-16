"""验证公开资产准备器的固定摘要、缓存复用和安全失败边界。"""

import hashlib
import io
import json
from pathlib import Path
import tarfile

import pytest

from installer.prepare_realtime_assets import prepare_realtime_assets
from installer import runtime_asset_io


def test_fixed_download_verifies_before_caching_and_reuses_cache(tmp_path: Path, monkeypatch) -> None:
    """只有通过固定大小和摘要的下载可复用，第二次运行不再访问网络。"""
    payload = b"fixed public model"
    digest = hashlib.sha256(payload).hexdigest()
    requests = []

    def download(request, timeout):
        requests.append(request.full_url)
        return io.BytesIO(payload)

    monkeypatch.setattr(runtime_asset_io, "urlopen", download)
    destination = tmp_path / "cache/model.onnx"
    for _ in range(2):
        result = runtime_asset_io.download_fixed_asset(
            ["https://example.invalid/model"], destination, len(payload), digest,
        )
        assert result.read_bytes() == payload
    assert requests == ["https://example.invalid/model"]
    assert not list(destination.parent.glob("*.part"))


@pytest.mark.parametrize("failure", ["size", "digest", "interrupted"])
def test_failed_download_never_becomes_a_reusable_asset(tmp_path: Path, monkeypatch, failure: str) -> None:
    """断网、大小错误或摘要错误都不能留下看似完成的文件。"""
    payload = b"corrupt or partial model"

    def download(request, timeout):
        if failure == "interrupted":
            raise OSError("download interrupted")
        return io.BytesIO(payload)

    monkeypatch.setattr(runtime_asset_io, "urlopen", download)
    destination = tmp_path / "cache/model.onnx"
    size = len(payload) + (failure == "size")
    digest = "0" * 64 if failure == "digest" else hashlib.sha256(payload).hexdigest()
    with pytest.raises(RuntimeError, match="公开构建资产下载失败"):
        runtime_asset_io.download_fixed_asset(["https://example.invalid/model"], destination, size, digest)
    assert not destination.exists()
    assert not list(destination.parent.glob("*.part"))


def test_corrupt_existing_cache_fails_without_overwriting_it(tmp_path: Path, monkeypatch) -> None:
    """损坏的既有缓存必须显式暴露，不能被网络结果悄悄覆盖。"""
    destination = tmp_path / "model.onnx"
    destination.write_bytes(b"old")
    monkeypatch.setattr(runtime_asset_io, "urlopen", lambda *_args, **_kwargs: pytest.fail("不应访问网络"))
    with pytest.raises(ValueError, match="SHA256"):
        runtime_asset_io.download_fixed_asset(["https://example.invalid/model"], destination, 3, "0" * 64)
    assert destination.read_bytes() == b"old"


def test_realtime_assets_reuse_only_matching_read_only_source(tmp_path: Path, monkeypatch) -> None:
    """可复用旧缓存，但错误版本必须转用公开固定来源，旧缓存保持原样。"""
    source = tmp_path / "old"
    source.mkdir()
    source.joinpath("first.onnx").write_bytes(b"first")
    source.joinpath("second.onnx").write_bytes(b"wrong version")
    records = []
    for name, payload in (("first.onnx", b"first"), ("second.onnx", b"second")):
        records.append({
            "path": name, "size_bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "urls": ["https://example.invalid/" + name],
        })
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"schema_version": 1, "assets": records}), encoding="utf-8")
    requests = []

    def download(request, timeout):
        requests.append(request.full_url)
        return io.BytesIO(b"second")

    monkeypatch.setattr(runtime_asset_io, "urlopen", download)
    destination = tmp_path / "output"
    prepare_realtime_assets(manifest, destination, tmp_path / "cache", source)
    assert destination.joinpath("first.onnx").read_bytes() == b"first"
    assert destination.joinpath("second.onnx").read_bytes() == b"second"
    assert source.joinpath("second.onnx").read_bytes() == b"wrong version"
    assert requests == ["https://example.invalid/second.onnx"]


@pytest.mark.parametrize("member_type", [tarfile.REGTYPE, tarfile.SYMTYPE])
def test_archive_extracts_only_the_declared_regular_member(tmp_path: Path, monkeypatch, member_type) -> None:
    """归档只允许读取点名的普通文件，额外路径或链接没有解压资格。"""
    payload = b"model weights"
    package = tmp_path / "models.tar.bz2"
    with tarfile.open(package, "w:bz2") as archive:
        member = tarfile.TarInfo("upstream/model.onnx")
        member.type = member_type
        if member_type == tarfile.SYMTYPE:
            member.linkname = "../../outside"
        else:
            member.size = len(payload)
        archive.addfile(member, io.BytesIO(payload) if member.isfile() else None)
        unexpected = tarfile.TarInfo("../../outside")
        unexpected.size = 3
        archive.addfile(unexpected, io.BytesIO(b"bad"))
    package_bytes = package.read_bytes()
    record = {
        "path": "model.onnx", "size_bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(), "urls": [],
        "archive": {
            "filename": package.name, "size_bytes": len(package_bytes),
            "sha256": hashlib.sha256(package_bytes).hexdigest(),
            "url": "https://example.invalid/models.tar.bz2", "member": "upstream/model.onnx",
        },
    }
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"schema_version": 1, "assets": [record]}), encoding="utf-8")
    monkeypatch.setattr(runtime_asset_io, "urlopen", lambda *_args, **_kwargs: io.BytesIO(package_bytes))
    destination = tmp_path / "output"
    if member_type == tarfile.SYMTYPE:
        with pytest.raises(ValueError, match="普通文件"):
            prepare_realtime_assets(manifest, destination, tmp_path / "cache")
        assert not destination.joinpath("model.onnx").exists()
    else:
        prepare_realtime_assets(manifest, destination, tmp_path / "cache")
        assert destination.joinpath("model.onnx").read_bytes() == payload
    assert not tmp_path.joinpath("outside").exists()
    assert not list(destination.glob("*.part"))


@pytest.mark.parametrize("relative", [
    "../outside", "C:/outside", "nested\\outside", "nested/../outside",
    "model.onnx ", "model.onnx.", "CON", "nested/LPT1.onnx", "model?", "model\x00.onnx",
])
def test_asset_path_cannot_escape_owned_directory(tmp_path: Path, relative: str) -> None:
    """清单路径在写入前拒绝目录穿越和 Windows 绝对路径。"""
    with pytest.raises(ValueError, match="安全相对路径"):
        runtime_asset_io.safe_asset_path(tmp_path, relative)
