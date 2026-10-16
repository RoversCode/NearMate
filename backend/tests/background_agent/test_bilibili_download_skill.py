"""bilibili-download Skill 脚本的单元测试（不触网、不执行真实下载）。"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

# 受管 Skill 脚本是独立资产，不在 pythonpath 上，按文件路径直接加载。
_SCRIPT_PATH = (
    Path(__file__).resolve().parents[3]
    / "backend"
    / "assets"
    / "agent-workspace"
    / ".claude"
    / "skills"
    / "bilibili-download"
    / "scripts"
    / "download_bilibili.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location("download_bilibili", _SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


download_bilibili = _load_module()


def _arguments(**overrides) -> SimpleNamespace:
    defaults = {
        "url": "https://www.bilibili.com/video/BV1xx411c7mD",
        "output_dir": "out",
        "quality": "best",
        "audio_only": False,
        "pages": None,
        "danmaku": False,
        "cookie_file": None,
    }
    return SimpleNamespace(**{**defaults, **overrides})


def test_build_command_maps_quality_priority() -> None:
    command = download_bilibili._build_bbdown_command(
        _arguments(quality="1080p"), Path("BBDown.exe"), Path("work")
    )

    assert command[command.index("-q") + 1] == "1080P 高码率,1080P 高清"
    assert "--work-dir" in command


def test_build_command_skips_quality_for_audio_only() -> None:
    command = download_bilibili._build_bbdown_command(
        _arguments(quality="1080p", audio_only=True), Path("BBDown.exe"), Path("work")
    )

    assert "--audio-only" in command
    assert "-q" not in command


def test_build_command_passes_pages_and_danmaku() -> None:
    command = download_bilibili._build_bbdown_command(
        _arguments(pages="1-3", danmaku=True), Path("BBDown.exe"), Path("work")
    )

    assert command[command.index("-p") + 1] == "1-3"
    assert "--download-danmaku" in command


def test_build_command_writes_cookie_into_config_file(tmp_path) -> None:
    cookie_file = tmp_path / "cookie.txt"
    cookie_file.write_text("SESSDATA=abc123", encoding="utf-8")
    work_dir = tmp_path / "work"
    work_dir.mkdir()

    command = download_bilibili._build_bbdown_command(
        _arguments(cookie_file=str(cookie_file)), Path("BBDown.exe"), work_dir
    )

    config_path = Path(command[command.index("--config-file") + 1])
    # cookie 走配置文件而不是命令行，避免出现在进程列表与日志里。
    assert config_path.read_text(encoding="utf-8").splitlines() == ["-c", "SESSDATA=abc123"]
    assert "SESSDATA" not in " ".join(command)


def test_ensure_bbdown_prefers_path(monkeypatch, tmp_path) -> None:
    path_bbdown = tmp_path / "bin" / "BBDown.exe"
    path_bbdown.parent.mkdir()
    path_bbdown.write_bytes(b"on-path")
    monkeypatch.setattr(download_bilibili.shutil, "which", lambda name: str(path_bbdown))

    assert download_bilibili.ensure_bbdown_executable() == path_bbdown


def test_ensure_bbdown_uses_local_cache(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(download_bilibili.shutil, "which", lambda name: None)
    monkeypatch.setattr(download_bilibili, "_find_repo_root", lambda: tmp_path)
    cached_exe = tmp_path / "backend" / "data" / "agent-tools" / "bbdown" / "BBDown.exe"
    cached_exe.parent.mkdir(parents=True)
    cached_exe.write_bytes(b"cached")

    assert download_bilibili.ensure_bbdown_executable() == cached_exe


def _fake_bbdown_zip() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("BBDown.exe", b"fake-exe")
    return buffer.getvalue()


def test_ensure_bbdown_downloads_and_verifies_checksum(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(download_bilibili.shutil, "which", lambda name: None)
    monkeypatch.setattr(download_bilibili, "_find_repo_root", lambda: tmp_path)
    zip_bytes = _fake_bbdown_zip()

    def fake_urlretrieve(url: str, destination: str) -> None:
        Path(destination).write_bytes(zip_bytes)

    monkeypatch.setattr(download_bilibili.urllib.request, "urlretrieve", fake_urlretrieve)
    monkeypatch.setattr(
        download_bilibili,
        "_BBDOWN_ZIP_SHA256",
        hashlib.sha256(zip_bytes).hexdigest(),
    )

    cached_exe = download_bilibili.ensure_bbdown_executable()

    assert cached_exe == tmp_path / "backend" / "data" / "agent-tools" / "bbdown" / "BBDown.exe"
    assert cached_exe.read_bytes() == b"fake-exe"


def test_ensure_bbdown_rejects_checksum_mismatch(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(download_bilibili.shutil, "which", lambda name: None)
    monkeypatch.setattr(download_bilibili, "_find_repo_root", lambda: tmp_path)

    def fake_urlretrieve(url: str, destination: str) -> None:
        Path(destination).write_bytes(_fake_bbdown_zip())

    monkeypatch.setattr(download_bilibili.urllib.request, "urlretrieve", fake_urlretrieve)
    # 不替换模块内钉死的校验和，伪造 zip 的真实校验和必然对不上。
    with pytest.raises(RuntimeError, match="校验和不匹配"):
        download_bilibili.ensure_bbdown_executable()
    assert not (tmp_path / "backend" / "data" / "agent-tools" / "bbdown" / "BBDown.exe").exists()


def test_collect_deliverables_moves_media_and_filters_junk(tmp_path) -> None:
    work_dir = tmp_path / "work"
    (work_dir / "合集").mkdir(parents=True)
    (work_dir / "正片.mp4").write_bytes(b"video")
    (work_dir / "合集" / "P1.m4a").write_bytes(b"audio")
    (work_dir / "说明.txt").write_bytes(b"not deliverable")
    output_dir = tmp_path / "delivery"
    output_dir.mkdir()

    deliverables = download_bilibili._collect_deliverables(work_dir, output_dir)

    assert (output_dir / "正片.mp4").read_bytes() == b"video"
    assert (output_dir / "合集" / "P1.m4a").read_bytes() == b"audio"
    # .txt 不在交付范围内，应被留在工作区而不是搬进交付目录。
    assert not (output_dir / "说明.txt").exists()
    assert len(deliverables) == 2


def test_collect_deliverables_refuses_to_overwrite(tmp_path) -> None:
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    (work_dir / "正片.mp4").write_bytes(b"new")
    output_dir = tmp_path / "delivery"
    output_dir.mkdir()
    (output_dir / "正片.mp4").write_bytes(b"existing")

    with pytest.raises(FileExistsError):
        download_bilibili._collect_deliverables(work_dir, output_dir)
