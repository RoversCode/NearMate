"""验证模型发布只包含白名单、保留许可并固定到匿名可核验的提交。"""

import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from installer import publish_realtime_assets as publisher


def _record(relative: str, content: bytes) -> dict:
    return {"path": relative, "size_bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}


def _publication_inputs(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    source = tmp_path / "source"
    notices = tmp_path / "notices"
    source.mkdir()
    notices.joinpath("licenses").mkdir(parents=True)
    source.joinpath("model.onnx").write_bytes(b"fixed model")
    source.joinpath("private-recording.wav").write_bytes(b"private")
    license_record = _record("licenses/LICENSE.txt", b"upstream Apache-2.0 license")
    notices.joinpath(license_record["path"]).write_bytes(b"upstream Apache-2.0 license")
    notices.joinpath("private-note.txt").write_bytes(b"private")
    sources = {
        "schema_version": 1,
        "groups": [{
            "assets": ["model.onnx"], "attribution": "Original Model Team",
            "license_files": [license_record["path"]],
            "source_urls": ["https://example.invalid/fixed-model"],
        }],
        "files": [license_record],
    }
    notices.joinpath("sources.json").write_text(json.dumps(sources), encoding="utf-8")
    manifest = tmp_path / "runtime-assets.json"
    record = _record("model.onnx", b"fixed model")
    record.update({"license": "Apache-2.0", "urls": ["https://example.invalid/fixed-model"]})
    manifest.write_text(json.dumps({"schema_version": 1, "assets": [record]}), encoding="utf-8")
    return manifest, source, tmp_path / "package", notices


def test_publication_copies_only_verified_assets_and_declared_licenses(tmp_path: Path) -> None:
    """私人同级文件没有发布资格，模型与原许可逐文件核验后才能进入包。"""
    inputs = _publication_inputs(tmp_path)
    publication = publisher.prepare_publication(*inputs)
    package = inputs[2]
    files = {path.relative_to(package).as_posix() for path in package.rglob("*") if path.is_file()}
    assert publication["asset_count"] == 1
    assert files == {"ckpt/model.onnx", "licenses/LICENSE.txt", "runtime-assets.json", "sources.json", "publication-manifest.json"}
    assert inputs[1].joinpath("private-recording.wav").read_bytes() == b"private"
    assert publisher.prepare_publication(*inputs) == publication


@pytest.mark.parametrize("failure", ["missing-attribution", "tampered-license", "extra-output", "traversal"])
def test_unsafe_or_unlicensed_package_fails_before_model_copy(tmp_path: Path, failure: str) -> None:
    """许可缺失、许可篡改、路径越界或混入文件都必须在模型复制前被拒绝。"""
    manifest, source, package, notices = _publication_inputs(tmp_path)
    sources = json.loads(notices.joinpath("sources.json").read_text(encoding="utf-8"))
    if failure == "missing-attribution":
        sources["groups"][0]["assets"] = []
    elif failure == "tampered-license":
        notices.joinpath("licenses/LICENSE.txt").write_bytes(b"tampered")
    elif failure == "extra-output":
        package.mkdir()
        package.joinpath("personal.wav").write_bytes(b"private")
    else:
        sources["files"][0]["path"] = "licenses/../../private.txt"
        sources["groups"][0]["license_files"] = ["licenses/../../private.txt"]
    notices.joinpath("sources.json").write_text(json.dumps(sources), encoding="utf-8")
    with pytest.raises(ValueError):
        publisher.prepare_publication(manifest, source, package, notices)
    assert not package.joinpath("ckpt/model.onnx").exists()


class FakeModelScope:
    """替代 SDK 的认证与上传边界，记录匿名验证是否真的使用提交摘要。"""

    endpoint = "https://www.modelscope.cn"

    def __init__(self, package: Path, conflict: bool = False, bad_remote_hash: bool = False) -> None:
        self.package = package
        self.conflict = conflict
        self.bad_remote_hash = bad_remote_hash
        self.uploads = []
        self.queries = []

    def get_model_files(self, repo_id: str, **kwargs) -> list[dict]:
        """模拟分支文件与匿名固定提交查询。

        Args:
            repo_id: 被测发布仓库。
            **kwargs: SDK 文件查询参数。

        Returns:
            模拟远程文件的路径、大小和 SHA256。
        """
        self.queries.append(kwargs)
        if kwargs["revision"] == "master":
            return [{"Path": "ckpt/model.onnx", "Size": 0, "Sha256": "0" * 64}] if self.conflict else []
        pytest.fail("匿名元数据不应经过带认证的 SDK")

    def published_files(self) -> list[dict]:
        """返回独立公开 HTTP 边界读取到的模拟文件摘要。

        Returns:
            模拟上传完成后的公开文件元数据。
        """
        result = []
        for path in self.package.rglob("*"):
            if path.is_file():
                content = path.read_bytes()
                result.append({"Path": path.relative_to(self.package).as_posix(), "Size": len(content), "Sha256": hashlib.sha256(content).hexdigest()})
        if self.bad_remote_hash:
            result[0]["Sha256"] = "0" * 64
        return result

    def upload_folder(self, **kwargs) -> SimpleNamespace:
        """记录上传参数并返回模拟不可变提交。

        Args:
            **kwargs: SDK 文件上传参数。

        Returns:
            含完整 Git oid 的模拟提交结果。
        """
        self.uploads.append(kwargs)
        return SimpleNamespace(oid="a" * 40)


def test_publication_pins_urls_and_verifies_without_login_cookies(tmp_path: Path, monkeypatch) -> None:
    """上传结果必须使用提交摘要，远程大小与摘要必须经过匿名查询。"""
    inputs = _publication_inputs(tmp_path)
    publisher.prepare_publication(*inputs)
    api = FakeModelScope(inputs[2])
    monkeypatch.setattr(publisher, "read_public_model_files", lambda repo, revision: api.published_files())
    result = publisher.publish_package(inputs[2], "Test/NearMate", api=api)
    assert result["revision"] == "a" * 40
    assert "Revision=" + "a" * 40 in result["asset_urls"]["model.onnx"]
    assert "FilePath=ckpt%2Fmodel.onnx" in result["asset_urls"]["model.onnx"]
    assert api.queries == [{"revision": "master", "recursive": True, "use_cookies": True}]
    assert len(api.uploads) == 1


def test_existing_different_remote_asset_is_never_overwritten(tmp_path: Path) -> None:
    """同名远程模型不同摘要时必须在上传之前停止。"""
    inputs = _publication_inputs(tmp_path)
    publisher.prepare_publication(*inputs)
    api = FakeModelScope(inputs[2], conflict=True)
    with pytest.raises(ValueError, match="同名文件"):
        publisher.publish_package(inputs[2], "Test/NearMate", api=api)
    assert not api.uploads


def test_tampered_package_or_extra_file_is_rejected_before_upload(tmp_path: Path) -> None:
    """已经审查的包被追加私人文件后不能继续上传。"""
    inputs = _publication_inputs(tmp_path)
    publisher.prepare_publication(*inputs)
    inputs[2].joinpath("private.txt").write_text("private", encoding="utf-8")
    api = FakeModelScope(inputs[2])
    with pytest.raises(ValueError, match="增加了文件"):
        publisher.publish_package(inputs[2], "Test/NearMate", api=api)
    assert not api.uploads


def test_remote_digest_mismatch_is_not_reported_as_success(tmp_path: Path, monkeypatch) -> None:
    """即使 SDK 上传成功，匿名元数据摘要不符也必须报告失败。"""
    inputs = _publication_inputs(tmp_path)
    publisher.prepare_publication(*inputs)
    api = FakeModelScope(inputs[2], bad_remote_hash=True)
    monkeypatch.setattr(publisher, "read_public_model_files", lambda repo, revision: api.published_files())
    with pytest.raises(RuntimeError, match="匿名元数据核验失败"):
        publisher.publish_package(inputs[2], "Test/NearMate", api=api)


@pytest.mark.parametrize("failure", [None, "receipt-digest", "receipt-size", "missing-receipt", "missing-revision", "malformed-revision", "fixed-file-digest", "fixed-file-missing"])
def test_empty_sdk_oid_resolves_only_matching_receipt_and_checks_fixed_commit(tmp_path: Path, monkeypatch, failure: str | None) -> None:
    """SDK 空 oid 只能从匹配回执恢复，随后仍须核验固定提交中的全部文件。"""
    inputs = _publication_inputs(tmp_path)
    publisher.prepare_publication(*inputs)
    api = FakeModelScope(inputs[2])
    monkeypatch.setattr(api, "upload_folder", lambda **kwargs: SimpleNamespace(oid=""))
    queried_revisions = []

    def public_metadata(repo_id, revision):
        queried_revisions.append(revision)
        records = api.published_files()
        if revision == "master":
            if failure == "missing-receipt":
                return [record for record in records if record["Path"] != "publication-manifest.json"]
            receipt = next(record for record in records if record["Path"] == "publication-manifest.json")
            if failure != "missing-revision":
                receipt["Revision"] = "master" if failure == "malformed-revision" else "b" * 40
            if failure == "receipt-digest":
                receipt["Sha256"] = "0" * 64
            elif failure == "receipt-size":
                receipt["Size"] += 1
        elif failure == "fixed-file-digest":
            next(record for record in records if record["Path"] == "ckpt/model.onnx")["Sha256"] = "0" * 64
        elif failure == "fixed-file-missing":
            return [record for record in records if record["Path"] != "ckpt/model.onnx"]
        return records

    monkeypatch.setattr(publisher, "read_public_model_files", public_metadata)
    if failure:
        with pytest.raises(RuntimeError):
            publisher.publish_package(inputs[2], "Test/NearMate", api=api)
    else:
        result = publisher.publish_package(inputs[2], "Test/NearMate", api=api)
        assert result["revision"] == "b" * 40
        assert queried_revisions == ["master", "b" * 40]


def test_malformed_nonempty_sdk_oid_never_falls_back_to_branch(tmp_path: Path, monkeypatch) -> None:
    """非空但畸形的 SDK oid 必须直接失败，不能混同空 oid 的已知兼容情况。"""
    inputs = _publication_inputs(tmp_path)
    publisher.prepare_publication(*inputs)
    api = FakeModelScope(inputs[2])
    monkeypatch.setattr(api, "upload_folder", lambda **kwargs: SimpleNamespace(oid="malformed-oid"))
    monkeypatch.setattr(publisher, "read_public_model_files", lambda *_args: pytest.fail("不应回退到分支"))
    with pytest.raises(ValueError, match="不可变"):
        publisher.publish_package(inputs[2], "Test/NearMate", api=api)


def test_download_url_rejects_mutable_revision() -> None:
    """客户下载地址禁止固定到可被后续上传改写的 master 分支。"""
    with pytest.raises(ValueError, match="不可变"):
        publisher.modelscope_asset_url("Test/NearMate", "master", "ckpt/model.onnx")


@pytest.mark.parametrize("failure", [None, "small-digest", "range-status", "range-content"])
def test_anonymous_download_checks_small_file_and_large_byte_range(tmp_path: Path, monkeypatch, failure: str | None) -> None:
    """匿名检查必须核对小文件全文，以及大文件的 206 范围和实际字节。"""
    small = b"small public file"
    large = b"large model " * 256
    tmp_path.joinpath("ckpt").mkdir()
    tmp_path.joinpath("ckpt/large.onnx").write_bytes(large)
    assets = [_record("small.txt", small), _record("large.onnx", large)]
    tmp_path.joinpath("runtime-assets.json").write_text(json.dumps({"assets": assets}), encoding="utf-8")
    calls = []

    def open_download(request, timeout):
        calls.append(request)
        assert not {"cookie", "authorization"} & {key.lower() for key in request.headers}
        if request.full_url.endswith("/small"):
            return io.BytesIO(b"wrong" if failure == "small-digest" else small)
        response = io.BytesIO(b"wrong" if failure == "range-content" else large[:1024])
        response.status = 200 if failure == "range-status" else 206
        response.headers = {"Content-Range": f"bytes 0-1023/{len(large)}"}
        assert request.headers["Range"] == "bytes=0-1023"
        return response

    monkeypatch.setattr(publisher, "build_opener", lambda: SimpleNamespace(open=open_download))
    publication = {"asset_urls": {"small.txt": "https://example.invalid/small", "large.onnx": "https://example.invalid/large"}}
    if failure:
        with pytest.raises(RuntimeError):
            publisher.verify_public_downloads(tmp_path, publication)
    else:
        publisher.verify_public_downloads(tmp_path, publication)
        assert len(calls) == 2


def test_anonymous_metadata_never_reads_cached_credentials_or_sends_auth(monkeypatch) -> None:
    """即使 SDK 存有登录，匿名核验也不能读取凭据或发送认证头。"""
    from modelscope.hub.api import ModelScopeConfig

    monkeypatch.setattr(ModelScopeConfig, "get_cookies", lambda: pytest.fail("匿名查询不应读取登录缓存"))
    monkeypatch.setattr(ModelScopeConfig, "get_token", lambda: pytest.fail("匿名查询不应读取 Token"))
    requests = []

    def open_metadata(request, timeout):
        requests.append(request)
        assert not {"cookie", "authorization"} & {key.lower() for key in request.headers}
        return io.BytesIO(json.dumps({"Code": 200, "Data": {"Files": [{"Path": "ckpt/model.onnx"}]}}).encode())

    monkeypatch.setattr(publisher, "build_opener", lambda: SimpleNamespace(open=open_metadata))
    assert publisher.read_public_model_files("Test/NearMate", "a" * 40) == [{"Path": "ckpt/model.onnx"}]
    assert len(requests) == 1
