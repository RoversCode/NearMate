"""验证源码资源准备的取消、隔离、离线复用和跨进程互斥。"""

import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
from types import SimpleNamespace

import pytest

from companion.runtime_assets import control, native, source
from companion.runtime_assets import io as asset_io
from companion.runtime_assets.locking import asset_lock
from companion.runtime_assets.realtime import prepare_realtime_assets
from companion.runtime_assets.source import AssetPreparationCancelled


def _manifest(tmp_path: Path, payload: bytes = b"fixed") -> Path:
    record = {
        "path": "voice/model.onnx", "size_bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "urls": ["https://example.invalid/model?signature=private"],
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"schema_version": 1, "assets": [record]}), encoding="utf-8")
    return path


def _manifest_with_archive(tmp_path: Path, archive_payload: bytes = b"fixed") -> tuple[Path, bytes, dict]:
    manifest = _manifest(tmp_path)
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:bz2") as package:
        member = tarfile.TarInfo("release/model.onnx")
        member.size = len(archive_payload)
        package.addfile(member, io.BytesIO(archive_payload))
    package_bytes = buffer.getvalue()
    archive = {
        "filename": "model.tar.bz2", "size_bytes": len(package_bytes),
        "sha256": hashlib.sha256(package_bytes).hexdigest(),
        "url": "https://upstream.example.invalid/model.tar.bz2", "member": "release/model.onnx",
    }
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["assets"][0]["archive"] = archive
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    return manifest, package_bytes, archive


@pytest.mark.parametrize("archive_cached", [False, True])
def test_mirror_failure_falls_back_to_fixed_archive(tmp_path: Path, monkeypatch, archive_cached: bool) -> None:
    """镜像全部失败后复用已验证归档缓存，或从固定上游下载同一归档。"""
    manifest, package_bytes, archive = _manifest_with_archive(tmp_path)
    cache_root = tmp_path / "cache"
    if archive_cached:
        cached = cache_root / archive["sha256"] / archive["filename"]
        cached.parent.mkdir(parents=True)
        cached.write_bytes(package_bytes)
    requests = []

    def download(request, timeout):
        requests.append(request.full_url)
        if request.full_url == archive["url"]:
            return io.BytesIO(package_bytes)
        raise OSError("镜像暂时不可用")

    monkeypatch.setattr(asset_io, "urlopen", download)
    prepare_realtime_assets(manifest, tmp_path / "ckpt", cache_root)

    assert (tmp_path / "ckpt/voice/model.onnx").read_bytes() == b"fixed"
    expected_requests = ["https://example.invalid/model?signature=private"]
    if not archive_cached:
        expected_requests.append(archive["url"])
    assert requests == expected_requests


def test_cancelled_mirror_does_not_start_archive_fallback(tmp_path: Path, monkeypatch) -> None:
    """镜像读取期间取消后，不能再请求上游归档或发布半个模型。"""
    manifest, _package_bytes, _archive = _manifest_with_archive(tmp_path)
    requests = []
    cancelled = False

    def download(request, timeout):
        nonlocal cancelled
        requests.append(request.full_url)
        cancelled = True
        raise OSError("镜像连接中断")

    monkeypatch.setattr(asset_io, "urlopen", download)
    with pytest.raises(AssetPreparationCancelled):
        prepare_realtime_assets(manifest, tmp_path / "ckpt", tmp_path / "cache",
                                is_cancelled=lambda: cancelled)
    assert requests == ["https://example.invalid/model?signature=private"]
    assert not (tmp_path / "ckpt/voice/model.onnx").exists()
    assert not list(tmp_path.rglob("*.part"))


@pytest.mark.parametrize("problem", ["manifest", "cache_directory", "progress_callback"])
def test_archive_fallback_does_not_hide_non_download_errors(tmp_path: Path, monkeypatch, problem: str) -> None:
    """非法清单、目录冲突与调用方异常都要直接报错，不能转为上游下载。"""
    manifest, _package_bytes, _archive = _manifest_with_archive(tmp_path)
    if problem == "manifest":
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        payload["assets"][0]["archive"]["member"] = "../outside"
        manifest.write_text(json.dumps(payload), encoding="utf-8")
    if problem == "cache_directory":
        digest = hashlib.sha256(b"fixed").hexdigest()
        (tmp_path / "cache" / digest / "model.onnx").mkdir(parents=True)

    def progress(message):
        if problem == "progress_callback" and message.startswith("开始下载"):
            raise RuntimeError("进度接收方失败")

    monkeypatch.setattr(asset_io, "urlopen", lambda *_a, **_k: pytest.fail("不应访问任何网络地址"))
    expected = RuntimeError if problem == "progress_callback" else ValueError
    with pytest.raises(expected):
        prepare_realtime_assets(manifest, tmp_path / "ckpt", tmp_path / "cache", progress=progress)
    assert not (tmp_path / "ckpt/voice/model.onnx").exists()


def test_archive_fallback_still_checks_the_extracted_model_digest(tmp_path: Path, monkeypatch) -> None:
    """归档本身通过校验仍不够，取出的模型必须继续匹配文件清单摘要。"""
    manifest, package_bytes, archive = _manifest_with_archive(tmp_path, archive_payload=b"wrong")
    requests = []

    def download(request, timeout):
        requests.append(request.full_url)
        if request.full_url == archive["url"]:
            return io.BytesIO(package_bytes)
        raise OSError("镜像暂时不可用")

    monkeypatch.setattr(asset_io, "urlopen", download)
    with pytest.raises(ValueError, match="SHA256"):
        prepare_realtime_assets(manifest, tmp_path / "ckpt", tmp_path / "cache")
    assert requests == ["https://example.invalid/model?signature=private", archive["url"]]
    assert not (tmp_path / "ckpt/voice/model.onnx").exists()
    assert not list(tmp_path.rglob("*.part"))


def test_mirror_failure_without_archive_keeps_existing_error_contract(tmp_path: Path, monkeypatch) -> None:
    """没有登记归档的旧直链清单仍按原合同报告下载失败。"""
    manifest = _manifest(tmp_path)

    def download(request, timeout):
        raise OSError("镜像暂时不可用")

    monkeypatch.setattr(asset_io, "urlopen", download)
    with pytest.raises(RuntimeError, match="公开构建资产下载失败：voice/model.onnx"):
        prepare_realtime_assets(manifest, tmp_path / "ckpt", tmp_path / "cache")


def test_later_mirror_success_does_not_request_archive(tmp_path: Path, monkeypatch) -> None:
    """仍有可用直链时先完成所有直链尝试，成功后不再下载归档。"""
    manifest, _package_bytes, _archive = _manifest_with_archive(tmp_path)
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    second_mirror = "https://second.example.invalid/model"
    payload["assets"][0]["urls"].append(second_mirror)
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    requests = []

    def download(request, timeout):
        requests.append(request.full_url)
        if request.full_url == second_mirror:
            return io.BytesIO(b"fixed")
        raise OSError("首个镜像暂时不可用")

    monkeypatch.setattr(asset_io, "urlopen", download)
    prepare_realtime_assets(manifest, tmp_path / "ckpt", tmp_path / "cache")
    assert requests == ["https://example.invalid/model?signature=private", second_mirror]
    assert (tmp_path / "ckpt/voice/model.onnx").read_bytes() == b"fixed"


def test_complete_assets_remain_offline_and_unrelated_files_untouched(tmp_path: Path, monkeypatch) -> None:
    """完整模型不联网，用户额外放入的文件也不参与清理。"""
    manifest = _manifest(tmp_path)
    destination = tmp_path / "ckpt"
    target = destination / "voice/model.onnx"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"fixed")
    user_file = destination / "personal.wav"
    user_file.write_bytes(b"user recording")
    monkeypatch.setattr(asset_io, "urlopen", lambda *_a, **_k: pytest.fail("不应联网"))

    prepare_realtime_assets(manifest, destination, tmp_path / "cache")

    assert target.read_bytes() == b"fixed"
    assert user_file.read_bytes() == b"user recording"
    assert not list(destination.rglob("*.backup"))


def test_corrupt_target_and_cache_are_backed_up_then_repaired(tmp_path: Path, monkeypatch) -> None:
    """坏目标与坏缓存各自备份，再用同一份固定下载修复。"""
    payload = b"fixed"
    manifest = _manifest(tmp_path, payload)
    target = tmp_path / "ckpt/voice/model.onnx"
    cached = tmp_path / "cache" / hashlib.sha256(payload).hexdigest() / "model.onnx"
    for path in (target, cached):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"wrong")
    monkeypatch.setattr(asset_io, "urlopen", lambda *_a, **_k: io.BytesIO(payload))

    prepare_realtime_assets(manifest, tmp_path / "ckpt", tmp_path / "cache")

    for path in (target, cached):
        assert path.read_bytes() == payload
        assert [backup.read_bytes() for backup in path.parent.glob(f"{path.name}.*.backup")] == [b"wrong"]


def test_cancelled_download_closes_response_and_removes_partial(tmp_path: Path, monkeypatch) -> None:
    """读取中取消会关连接、删除半文件，并保留损坏旧文件的备份。"""
    cancelled = False

    class Response(io.BytesIO):
        def read(self, size=-1):
            nonlocal cancelled
            data = super().read(size)
            cancelled = True
            return data

        read1 = read

    response = Response(b"fixed")
    destination = tmp_path / "model.onnx"
    destination.write_bytes(b"old")
    monkeypatch.setattr(asset_io, "urlopen", lambda *_a, **_k: response)

    with pytest.raises(AssetPreparationCancelled):
        asset_io.download_fixed_asset(["https://example.invalid/model"], destination, 5,
                                      hashlib.sha256(b"fixed").hexdigest(), is_cancelled=lambda: cancelled)

    assert response.closed
    assert not destination.exists()
    assert not list(tmp_path.glob("*.part"))
    assert [path.read_bytes() for path in tmp_path.glob("*.backup")] == [b"old"]


def test_failed_attempt_cannot_pollute_successful_retry(tmp_path: Path, monkeypatch) -> None:
    """前一个地址返回坏数据时，后一个地址必须从全新临时文件重新下载。"""
    requests = []

    def download(request, timeout):
        requests.append(request.full_url)
        return io.BytesIO(b"bad" if len(requests) == 1 else b"fixed")

    monkeypatch.setattr(asset_io, "urlopen", download)
    destination = tmp_path / "model.onnx"
    asset_io.download_fixed_asset(["https://example.invalid/first", "https://example.invalid/second"],
                                  destination, 5, hashlib.sha256(b"fixed").hexdigest())

    assert len(requests) == 2
    assert destination.read_bytes() == b"fixed"
    assert not list(tmp_path.glob("*.part"))


def test_failed_repair_preserves_the_corrupt_backup(tmp_path: Path, monkeypatch) -> None:
    """所有地址失败时仍保留损坏原件，正式文件不能变成半个下载。"""
    destination = tmp_path / "model.onnx"
    destination.write_bytes(b"old")
    monkeypatch.setattr(asset_io, "urlopen", lambda *_a, **_k: io.BytesIO(b"bad"))
    with pytest.raises(RuntimeError, match="公开构建资产下载失败"):
        asset_io.download_fixed_asset(["https://example.invalid/model"], destination, 5,
                                      hashlib.sha256(b"fixed").hexdigest())
    assert not destination.exists()
    assert [path.read_bytes() for path in tmp_path.glob("*.backup")] == [b"old"]
    assert not list(tmp_path.glob("*.part"))


def test_cancellation_during_hashing_keeps_existing_target(tmp_path: Path) -> None:
    """校验途中取消不能把未判定损坏的正式文件当坏文件移走。"""
    payload = b"x" * (asset_io.CHUNK_SIZE * 2)
    destination = tmp_path / "model.onnx"
    destination.write_bytes(payload)
    checks = 0

    def cancelled():
        nonlocal checks
        checks += 1
        return checks >= 4

    with pytest.raises(AssetPreparationCancelled):
        asset_io.keep_valid_asset(destination, len(payload), hashlib.sha256(payload).hexdigest(),
                                  is_cancelled=cancelled)
    assert destination.read_bytes() == payload
    assert not list(tmp_path.glob("*.backup"))


def test_download_failure_and_progress_never_expose_signed_url(tmp_path: Path, monkeypatch) -> None:
    """回调和最终错误只包含安全资源名，不包含签名地址或网络异常正文。"""
    messages = []

    def download(request, timeout):
        raise OSError("https://provider.invalid/?token=private")

    monkeypatch.setattr(asset_io, "urlopen", download)
    with pytest.raises(RuntimeError, match="公开构建资产下载失败") as failure:
        asset_io.download_fixed_asset(["https://example.invalid/model?secret=private"], tmp_path / "model.onnx",
                                      5, hashlib.sha256(b"fixed").hexdigest(), progress=messages.append,
                                      relative_name="voice/model.onnx")

    visible = " ".join(messages) + str(failure.value)
    assert "voice/model.onnx" in visible
    assert all(secret not in visible for secret in ("https://", "private", str(tmp_path)))


def test_progress_is_throttled_but_stage_edges_are_immediate(monkeypatch) -> None:
    """连续进度每秒最多一次，开始和结束仍能立即显示。"""
    now = 0.0
    monkeypatch.setattr(control.time, "monotonic", lambda: now)
    messages = []
    reporter = control.ProgressReporter(messages.append)
    reporter.report("开始", force=True)
    reporter.report("过于频繁")
    now = 1.1
    reporter.report("一秒后")
    reporter.report("结束", force=True)
    assert messages == ["开始", "一秒后", "结束"]


def test_manifest_is_fully_validated_before_any_download_or_backup(tmp_path: Path, monkeypatch) -> None:
    """清单后面的非法路径必须在第一次下载或移动旧文件前被拒绝。"""
    manifest = _manifest(tmp_path)
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["assets"].append({**payload["assets"][0], "path": "../outside"})
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    target = tmp_path / "ckpt/voice/model.onnx"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"old")
    monkeypatch.setattr(asset_io, "urlopen", lambda *_a, **_k: pytest.fail("不应联网"))
    with pytest.raises(ValueError, match="安全相对路径"):
        prepare_realtime_assets(manifest, tmp_path / "ckpt", tmp_path / "cache")
    assert target.read_bytes() == b"old"
    assert not list(tmp_path.rglob("*.backup"))


def test_packaged_preparation_does_not_read_write_or_download(tmp_path: Path, monkeypatch) -> None:
    """安装态即使收到取消或无效根目录，也不调用源码准备器。"""
    monkeypatch.setenv("NEARMATE_PACKAGED", "1")
    monkeypatch.setattr(source, "prepare_realtime_assets", lambda *_a, **_k: pytest.fail("不应准备模型"))
    monkeypatch.setattr(source, "prepare_native_runtime", lambda *_a, **_k: pytest.fail("不应准备原生库"))
    source.prepare_source_runtime_assets(repository_root=tmp_path / "missing", is_cancelled=lambda: True)
    assert list(tmp_path.iterdir()) == []


def test_source_entry_reuses_shared_preparers_and_forwards_cancellation(tmp_path: Path, monkeypatch) -> None:
    """源码入口共用模型和 DLL 准备器，并把同一取消回调传到底层。"""
    monkeypatch.delenv("NEARMATE_PACKAGED", raising=False)
    (tmp_path / "installer").mkdir()
    (tmp_path / "installer/realtime-assets.json").write_text("{}", encoding="utf-8")
    calls = []
    cancelled = lambda: False
    progress = lambda message: None
    monkeypatch.setattr(source, "prepare_realtime_assets", lambda *a, **k: calls.append(("models", a, k)))
    monkeypatch.setattr(source, "prepare_native_runtime", lambda *a, **k: calls.append(("native", a, k)))

    source.prepare_source_runtime_assets(repository_root=tmp_path, progress=progress, is_cancelled=cancelled)

    assert [call[0] for call in calls] == ["models", "native"]
    assert calls[0][1] == (tmp_path / "installer/realtime-assets.json", tmp_path / "backend/ckpt", tmp_path / ".artifacts/realtime-assets")
    assert all(call[2] == {"progress": progress, "is_cancelled": cancelled} for call in calls)


@pytest.mark.parametrize("script_name", ["prepare_realtime_assets.py", "prepare_native_runtime.py"])
def test_installer_cli_imports_shared_package_in_isolated_python(script_name: str, tmp_path: Path) -> None:
    """构建 CLI 不依赖工作目录或 PYTHONPATH，也能找到当前 checkout 的共享实现。"""
    repository_root = Path(__file__).resolve().parents[3]
    result = subprocess.run([sys.executable, "-I", "-B", str(repository_root / "installer" / script_name), "--help"],
                            cwd=tmp_path, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert "--destination" in result.stdout


def test_native_sdk_missing_is_clear_and_does_not_write(tmp_path: Path, monkeypatch) -> None:
    """缺少固定 SDK 时只提示安装项目依赖，不能自行切换版本。"""
    def missing(_name):
        raise native.PackageNotFoundError("private package location")

    monkeypatch.setattr(native, "version", missing)
    with pytest.raises(RuntimeError, match="StreamVox 2.0.0") as failure:
        native.prepare_native_runtime(tmp_path / "bin")
    assert "private" not in str(failure.value)
    assert list(tmp_path.iterdir()) == []


def test_native_preparation_repairs_only_sdk_declared_files(tmp_path: Path, monkeypatch) -> None:
    """DLL 修复保留坏版本备份和用户额外文件，完整重跑不再复制。"""
    sdk = tmp_path / "sdk"
    (sdk / "bin").mkdir(parents=True)
    payload = b"fixed native"
    (sdk / "bin/llama.dll").write_bytes(payload)
    sdk_manifest = {"sdk": "2.0.0"}
    (sdk / "bin/native-manifest.json").write_text(json.dumps(sdk_manifest), encoding="utf-8")
    assets = SimpleNamespace(__file__=str(sdk / "native_assets.py"),
                             NATIVE_DLL_SHA256={"llama.dll": hashlib.sha256(payload).hexdigest()},
                             native_manifest=lambda: sdk_manifest)
    monkeypatch.setitem(sys.modules, "streamvox", SimpleNamespace(native_assets=assets))
    monkeypatch.setattr(native, "version", lambda _name: "2.0.0")
    destination = tmp_path / "bin"
    destination.mkdir()
    (destination / "llama.dll").write_bytes(b"old")
    (destination / "personal.txt").write_bytes(b"keep")
    native.prepare_native_runtime(destination)
    native.prepare_native_runtime(destination)
    assert (destination / "llama.dll").read_bytes() == payload
    assert (destination / "personal.txt").read_bytes() == b"keep"
    assert [path.read_bytes() for path in destination.glob("llama.dll.*.backup")] == [b"old"]


def test_native_source_failure_does_not_move_existing_target(tmp_path: Path, monkeypatch) -> None:
    """SDK 自身损坏时停止准备，不备份或覆盖用户目录中的既有 DLL。"""
    sdk = tmp_path / "sdk"
    (sdk / "bin").mkdir(parents=True)
    (sdk / "bin/llama.dll").write_bytes(b"bad")
    (sdk / "bin/native-manifest.json").write_text("{}", encoding="utf-8")
    assets = SimpleNamespace(__file__=str(sdk / "native_assets.py"),
                             NATIVE_DLL_SHA256={"llama.dll": "0" * 64}, native_manifest=lambda: {})
    monkeypatch.setitem(sys.modules, "streamvox", SimpleNamespace(native_assets=assets))
    monkeypatch.setattr(native, "version", lambda _name: "2.0.0")
    destination = tmp_path / "bin"
    destination.mkdir()
    (destination / "llama.dll").write_bytes(b"existing")
    with pytest.raises(RuntimeError, match="原生资源不完整或摘要错误"):
        native.prepare_native_runtime(destination)
    assert (destination / "llama.dll").read_bytes() == b"existing"
    assert not list(destination.glob("*.backup"))


def test_waiting_for_another_process_can_be_cancelled(tmp_path: Path) -> None:
    """另一个进程持锁时可取消等待，取消后锁仍由原进程持有。"""
    script = """
import sys, time
from pathlib import Path
from companion.runtime_assets.locking import asset_lock
with asset_lock(Path(sys.argv[1])):
    print('locked', flush=True)
    time.sleep(30)
"""
    environment = {**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)}
    child = subprocess.Popen([sys.executable, "-c", script, str(tmp_path / "shared.lock")],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=environment)
    try:
        assert child.stdout.readline().strip() == "locked"
        checks = 0

        def cancelled():
            nonlocal checks
            checks += 1
            return checks >= 4

        with pytest.raises(AssetPreparationCancelled):
            with asset_lock(tmp_path / "shared.lock", cancelled):
                pytest.fail("不应获得其他进程持有的锁")
        assert child.poll() is None
    finally:
        child.terminate()
        child.communicate(timeout=10)


def test_parallel_download_processes_publish_only_one_network_result(tmp_path: Path) -> None:
    """两个独立进程准备同一缓存时，只有首个进程真正下载。"""
    script = """
import hashlib, io, sys, time
from pathlib import Path
from companion.runtime_assets import io as assets
root = Path(sys.argv[1])
def download(request, timeout):
    with (root / 'requests.txt').open('a') as log:
        log.write('download\\n')
    time.sleep(0.2)
    return io.BytesIO(b'fixed')
assets.urlopen = download
assets.download_fixed_asset(['https://example.invalid/model'], root / 'model.onnx', 5,
                            hashlib.sha256(b'fixed').hexdigest())
"""
    environment = {**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)}
    children = [subprocess.Popen([sys.executable, "-c", script, str(tmp_path)],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=environment)
                for _ in range(2)]
    try:
        for child in children:
            stdout, stderr = child.communicate(timeout=10)
            assert child.returncode == 0, stdout + stderr
        assert (tmp_path / "requests.txt").read_text().splitlines() == ["download"]
        assert (tmp_path / "model.onnx").read_bytes() == b"fixed"
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.communicate(timeout=10)
