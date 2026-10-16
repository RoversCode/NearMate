"""保留 Whisper 初版误差区间实验；该估计不具备数值验收资格。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from time import perf_counter

import librosa
import numpy as np
import soundfile as sf
from numeric_metrics import compare_arrays
from transformers import WhisperFeatureExtractor


def feature_interval(
    waveform: np.ndarray, extractor: WhisperFeatureExtractor
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """按直接 DFT 的假设估计区间，仅用于定位差异。

    Args:
        waveform: 与两个被测实现完全相同的 float32 归一化窗口。
        extractor: 含固定频谱参数及 mel 系数的提取器。
    Returns:
        float64 参考特征及估计区间；未证明该区间覆盖实际 FFT 内核。
    """
    size = extractor.n_fft
    unit = 2.0**-24
    gamma = lambda count: count * unit / (1 - count * unit)
    padded = np.pad(waveform.astype(np.float64), (size // 2, size // 2), mode="reflect")
    frames = np.lib.stride_tricks.sliding_window_view(padded, size)[
        :: extractor.hop_length
    ][:-1]
    window = (1 - np.cos(2 * np.pi * np.arange(size) / size)) / 2
    weighted = frames * window
    basis = np.exp(
        -2j
        * np.pi
        * np.arange(size // 2 + 1)[:, None]
        * np.arange(size)[None, :]
        / size
    )
    spectrum = basis @ weighted.T
    absolute = np.abs(spectrum)
    error = gamma(8 * size + 4) * np.sum(np.abs(weighted), axis=1)
    error += 8 * unit * np.sum(np.abs(frames), axis=1)
    power = absolute**2
    power_error = 2 * absolute * error + error**2 + gamma(4) * power
    mel = extractor.mel_filters.T @ power
    mel_error = (
        extractor.mel_filters.T @ power_error
        + gamma(2 * len(power)) * np.abs(mel)
        + unit * np.abs(mel)
    )
    reference = np.log10(np.maximum(1e-10, mel))
    lower = np.log10(np.maximum(1e-10, mel - mel_error))
    upper = np.log10(np.maximum(1e-10, mel + mel_error))
    log_error = 4 * np.abs(np.spacing(reference.astype(np.float32)).astype(np.float64))
    lower -= log_error
    upper += log_error
    lower = np.maximum(lower, lower.max() - 8)
    upper = np.maximum(upper, upper.max() - 8)
    reference = (np.maximum(reference, reference.max() - 8) + 4) / 4
    lower = (lower + 4) / 4
    upper = (upper + 4) / 4
    final_error = 2 * np.abs(
        np.spacing(reference.astype(np.float32)).astype(np.float64)
    )
    return reference, lower - final_error, upper + final_error


def run(reference_audio: Path, output: Path) -> None:
    """检查真实音频和边界输入，并保存独立的数值与耗时报告。

    Args:
        reference_audio: 固定参考音频文件。
        output: JSON 报告路径。
    Returns:
        无返回；任一预算失败时抛出异常。
    """
    extractor = WhisperFeatureExtractor(chunk_length=8)
    audio, rate = sf.read(reference_audio, dtype="float32")
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    audio = librosa.resample(audio, orig_sr=rate, target_sr=16000)
    generator = np.random.default_rng(829)
    cases = {
        "reference": audio,
        "silence": np.zeros(128000, np.float32),
        "tiny": audio * np.float32(1e-8),
        "one_sample": np.array([1e-7], np.float32),
        "short": audio[:319],
        "boundary_160": audio[:160],
        "boundary_161": audio[:161],
        "long": np.tile(audio, 5),
        "noise": generator.normal(0, 0.1, 128000).astype(np.float32),
    }
    results = {}
    for name, value in cases.items():
        value = np.pad(value[-128000:], (max(0, 128000 - len(value)), 0)).astype(
            np.float32
        )
        normalized = extractor.zero_mean_unit_var_norm([value], None)[0]
        started = perf_counter()
        old = extractor._torch_extract_fbank_features(normalized[None], "cpu")
        torch_seconds = perf_counter() - started
        started = perf_counter()
        new = extractor._np_extract_fbank_features(normalized[None], "cpu")
        numpy_seconds = perf_counter() - started
        reference, lower, upper = feature_interval(normalized, extractor)
        metrics = compare_arrays(new, old)
        inside = bool(
            np.all(new[0] >= lower)
            and np.all(new[0] <= upper)
            and np.all(old[0] >= lower)
            and np.all(old[0] <= upper)
        )
        results[name] = metrics | {
            "inside_frozen_interval": inside,
            "torch_seconds": torch_seconds,
            "numpy_seconds": numpy_seconds,
            "max_interval_width": float(np.max(upper - lower)),
            "numpy_max_reference_error": float(np.max(np.abs(new[0] - reference))),
            "torch_max_reference_error": float(np.max(np.abs(old[0] - reference))),
        }
        print(name, results[name], flush=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(
            {
                "budget_version": 1,
                "numerical_acceptance": "未验证",
                "limitation": "直接 DFT 预算未证明覆盖实际 FFT，窗口和 log10 的 ULP 假设未证明；区间覆盖不表示等价验收通过",
                "cases": results,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    assert all(
        row["shape_equal"]
        and row["dtype_equal"]
        and row["finite"]
        and row["inside_frozen_interval"]
        for row in results.values()
    )
    raise RuntimeError("已保存诊断结果；初版误差预算未经证明，禁止作为数值验收通过依据")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    run(args.reference, args.output)
