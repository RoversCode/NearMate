"""检查安装态 Python 的解释器、标准库和依赖是否都来自自身目录。"""

import ctypes
from ctypes import wintypes
import hashlib
import importlib
import importlib.metadata
import json
from pathlib import Path
import subprocess
import stat
import sys
import os
from tempfile import TemporaryDirectory


def verify_msvc_runtime(root: Path) -> dict[str, object]:
    """检查随包 C++ 运行库完整，并确认原生依赖实际加载的是随包 DLL。

    Args:
        root: 当前冻结 Python 的根目录。

    Returns:
        已完成完整性校验的固定版本及关键 DLL 实际加载路径；缺失或借用系统 DLL 时抛出异常。
    """
    scripts = root / "Scripts"
    license_directory = root / "licenses" / "msvc-runtime"
    manifest_path = license_directory / "msvc-runtime.json"
    expected_files = {
        "concrt140.dll", "msvcp140.dll", "msvcp140_1.dll", "msvcp140_2.dll",
        "msvcp140_atomic_wait.dll", "msvcp140_codecvt_ids.dll", "vccorlib140.dll",
        "vcruntime140.dll", "vcruntime140_1.dll", "vcruntime140_threads.dll",
    }
    expected_licenses = {
        "microsoft-vc-redist-license-en.rtf", "microsoft-vc-redist-license-zh-CN.rtf",
    }
    # 内容相同的外部 DLL 也不能借用；在读取或解析路径前拒绝文件及其父目录的重解析点。
    material_paths = (
        manifest_path, license_directory / "SOURCE.txt",
        *(scripts / name for name in expected_files),
        *(license_directory / name for name in expected_licenses),
    )
    path_segments = {segment for path in material_paths for segment in (path, *path.parents)}
    for segment in path_segments:
        try:
            attributes = segment.lstat()
        except FileNotFoundError:
            continue
        if (stat.S_ISLNK(attributes.st_mode)
                or getattr(attributes, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
            raise RuntimeError(f"Microsoft C++ 运行库路径不能经过重解析点：{segment}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (manifest.get("schema_version") != 1 or manifest.get("version") != "14.42.34433.0"
            or manifest.get("platform") != "windows-x64"):
        raise RuntimeError("Microsoft C++ 运行库清单版本或平台不符合固定合同")
    for records, expected_names, directory in (
        (manifest.get("files", []), expected_files, scripts),
        (manifest.get("licenses", []), expected_licenses, license_directory),
    ):
        if len(records) != len(expected_names) or {record["name"] for record in records} != expected_names:
            raise RuntimeError("Microsoft C++ 运行库或原始许可清单不完整")
        for record in records:
            path = directory / record["name"]
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != record["sha256"]:
                raise RuntimeError(f"Microsoft C++ 运行库或原始许可 SHA256 不匹配：{path}")
    if not (license_directory / "SOURCE.txt").is_file():
        raise RuntimeError("Microsoft C++ 运行库缺少来源说明")

    # 在真实 ORT 和语音依赖导入之后查询已加载模块，不能靠预加载把缺包问题掩盖掉。
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel32.GetModuleHandleW.restype = wintypes.HMODULE
    kernel32.GetModuleFileNameW.argtypes = [wintypes.HMODULE, wintypes.LPWSTR, wintypes.DWORD]
    kernel32.GetModuleFileNameW.restype = wintypes.DWORD
    loaded = {}
    for name in ("msvcp140.dll", "msvcp140_1.dll", "vcruntime140.dll", "vcruntime140_1.dll"):
        handle = kernel32.GetModuleHandleW(name)
        if not handle:
            raise RuntimeError(f"原生依赖没有加载必需的 Microsoft C++ 运行库：{name}")
        buffer = ctypes.create_unicode_buffer(32_768)
        length = kernel32.GetModuleFileNameW(handle, buffer, len(buffer))
        if not length or length >= len(buffer):
            raise RuntimeError(f"无法取得 Microsoft C++ 运行库实际路径：{name}")
        actual_path = Path(buffer.value).resolve()
        if actual_path.parent != scripts.resolve() or actual_path.name.casefold() != name:
            raise RuntimeError(f"Microsoft C++ 运行库必须从随包 Scripts 加载：{name} -> {actual_path}")
        loaded[name] = str(actual_path)
    return {"version": manifest["version"], "dll_directory": str(scripts), "loaded": loaded}


def verify_voice_preprocessing() -> dict[str, object]:
    """实际执行语音模型的公共音频前处理，拦住被错误裁剪的懒加载依赖。

    Args:
        无；使用当前解释器的 StreamVox 和音频依赖，不下载或加载模型权重。

    Returns:
        模型模块来源和本次音频处理的结果摘要。
    """
    import numpy as np
    import soundfile as sf

    # 仅导入 streamvox 或 librosa 不会触发模型内部的 SciPy/Numba 依赖。
    # 首次 JIT 编译缓存写到临时目录，不能把构建机缓存带进只读安装目录。
    previous_numba_cache = os.environ.get("NUMBA_CACHE_DIR")
    with TemporaryDirectory(prefix="nearmate-audio-smoke-") as temporary:
        os.environ["NUMBA_CACHE_DIR"] = str(Path(temporary) / "numba")
        try:
            model_modules = (
                "streamvox.models.qwen3.runtime",
                "streamvox.models.s2_pro.runtime",
                "streamvox.models.voxcpm2.runtime",
                "streamvox.models.moss_tts_nano.runtime",
            )
            model_origins = {
                name: str(Path(importlib.import_module(name).__spec__.origin).resolve())
                for name in model_modules
            }
            import librosa

            sample_rate = 24_000
            sample_count = 4_800
            time_axis = np.arange(sample_count, dtype=np.float32) / sample_rate
            reference = (0.1 * np.sin(2 * np.pi * 440 * time_axis)).astype(np.float32)
            audio_path = Path(temporary) / "reference.wav"
            sf.write(audio_path, reference, sample_rate, subtype="PCM_16")
            restored, restored_rate = librosa.load(audio_path, sr=16_000, mono=True)
            assert restored_rate == 16_000
            assert restored.shape == (3_200,), restored.shape
            assert restored.dtype == np.float32, restored.dtype
            assert np.isfinite(restored).all()
            assert np.max(np.abs(restored)) > 0.05
            mel = librosa.feature.melspectrogram(
                y=restored, sr=restored_rate, n_fft=512, hop_length=160, n_mels=40
            )
            assert mel.shape[0] == 40 and mel.shape[1] > 0, mel.shape
            assert np.isfinite(mel).all() and np.max(mel) > 0
            return {
                "model_origins": model_origins,
                "resampled_samples": int(restored.size),
                "mel_shape": list(mel.shape),
            }
        finally:
            if previous_numba_cache is None:
                os.environ.pop("NUMBA_CACHE_DIR", None)
            else:
                os.environ["NUMBA_CACHE_DIR"] = previous_numba_cache


def verify_runtime() -> None:
    """校验安装 Python 自包含边界，并确认语音前处理所需依赖能实际运行。

    Args:
        无；直接检查运行本脚本的 Python。

    Returns:
        无；依赖缺失、音频处理失败或模块来自安装目录外时抛出异常，构建必须停止。
    """
    root = Path(sys.executable).resolve().parent.parent
    assert sys.version_info[:3] == (3, 12, 13), sys.version
    assert Path(sys.base_prefix).resolve() == root, (sys.base_prefix, root)
    assert Path(sys.prefix).resolve() == root, (sys.prefix, root)
    assert not (root / "pyvenv.cfg").exists()
    voice_preprocessing = verify_voice_preprocessing()
    for origin in voice_preprocessing["model_origins"].values():
        assert Path(origin).is_relative_to(root), origin
    modules = (
        "encodings", "os", "ssl", "sqlite3", "ctypes", "multiprocessing",
        "numpy", "numpy.testing", "scipy", "scipy.signal", "numba",
        "onnxruntime", "streamvox", "streamvox_runtime", "soundcard",
        "sherpa_onnx", "cryptography", "librosa", "transformers", "fastapi",
    )
    origins = {}
    for name in modules:
        module = importlib.import_module(name)
        origin = Path(module.__file__).resolve()
        assert origin.is_relative_to(root), (name, origin)
        origins[name] = str(origin.relative_to(root))
    msvc_runtime = verify_msvc_runtime(root)
    packages = {d.metadata["Name"].lower() for d in importlib.metadata.distributions()}
    assert not packages.intersection({"torch", "torchaudio", "torchvision"})
    ort_packages = sorted(name for name in packages if name.startswith("onnxruntime"))
    assert len(ort_packages) == 1, ort_packages
    profile = os.environ.get("NEARMATE_RUNTIME_PROFILE", "cuda").strip().lower()
    assert profile in {"cuda", "universal"}, profile
    expected_ort = "onnxruntime-gpu" if profile == "cuda" else "onnxruntime-directml"
    assert ort_packages == [expected_ort], (profile, ort_packages)
    mediafoundation_header = root / "Lib" / "site-packages" / "soundcard" / "mediafoundation.py.h"
    assert mediafoundation_header.is_file(), mediafoundation_header
    import soundcard.mediafoundation  # noqa: F401
    child = subprocess.check_output(
        [sys.executable, "-I", "-B", "-c", "import sys; print(sys.base_prefix)"],
        text=True,
    ).strip()
    assert Path(child).resolve() == root, child
    print(json.dumps({"python_root": str(root), "profile": profile, "ort": ort_packages, "mediafoundation_header": str(mediafoundation_header), "origins": origins, "voice_preprocessing": voice_preprocessing, "msvc_runtime": msvc_runtime}))


if __name__ == "__main__":
    verify_runtime()
