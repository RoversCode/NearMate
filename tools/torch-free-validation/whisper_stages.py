"""逐阶段记录 Whisper 差异，区分实测观察和未证明的算法误差界。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import librosa
import numpy as np
import soundfile as sf
import torch
from numeric_metrics import compare_arrays
from environment_fingerprint import environment_fingerprint, file_sha256
from transformers import WhisperFeatureExtractor
from transformers.audio_utils import spectrogram, window_function


def compare_stages(waveform: np.ndarray, extractor: WhisperFeatureExtractor) -> dict:
    """复现两条实际路径，核对复现结果后再报告各阶段误差。

    Args:
        waveform: 两条路径共用的归一化后 float32 窗口。
        extractor: 冻结频谱参数的特征提取器。
    Returns:
        逐阶段指标；这些观察不能替代尚未证明的 FFT 和对数误差预算。
    """
    torch_window = torch.hann_window(extractor.n_fft)
    numpy_window = window_function(extractor.n_fft, "hann")
    old_spectrum = torch.stft(
        torch.from_numpy(waveform),
        extractor.n_fft,
        extractor.hop_length,
        window=torch_window,
        return_complex=True,
    )[..., :-1]
    new_spectrum = spectrogram(
        waveform, numpy_window, extractor.n_fft, extractor.hop_length, power=None
    )[..., :-1]
    old_power = old_spectrum.abs() ** 2
    new_power = np.abs(new_spectrum, dtype=np.float64) ** 2
    old_mel = torch.from_numpy(extractor.mel_filters.astype(np.float32)).T @ old_power
    new_mel = extractor.mel_filters.T @ new_power
    old_log = torch.clamp(old_mel, min=1e-10).log10()
    new_log = np.log10(np.maximum(new_mel, 1e-10)).astype(np.float32)
    old_final = (torch.maximum(old_log, old_log.max() - 8) + 4) / 4
    new_final = (np.maximum(new_log, new_log.max() - 8) + 4) / 4
    # 每条拆解路径必须先和库的原入口逐元素一致，避免用另写的算法解释另一个输出。
    expected_old = extractor._torch_extract_fbank_features(waveform[None], "cpu")[0]
    expected_new = extractor._np_extract_fbank_features(waveform[None], "cpu")[0]
    np.testing.assert_array_equal(old_final.numpy(), expected_old)
    np.testing.assert_array_equal(new_final, expected_new)
    pairs = {
        "window": (numpy_window, torch_window.numpy()),
        "fft_real": (new_spectrum.real, old_spectrum.real.numpy()),
        "fft_imag": (new_spectrum.imag, old_spectrum.imag.numpy()),
        "power": (new_power, old_power.numpy()),
        "mel": (new_mel, old_mel.numpy()),
        "log10": (new_log, old_log.numpy()),
        "final": (new_final, old_final.numpy()),
    }
    results = {}
    for name, (new, old) in pairs.items():
        metrics = compare_arrays(new, old)
        # 中间精度本来不同，额外报告 float64 下的距离，但不把转换后的比较冒充 dtype 一致。
        delta = np.abs(new.astype(np.float64) - old.astype(np.float64))
        metrics.update(
            max_abs_observed=float(delta.max(initial=0)),
            numpy_dtype=str(new.dtype),
            torch_dtype=str(old.dtype),
        )
        results[name] = metrics
    return {"reconstructed_outputs_exact": True, "stages": results}


def run(reference: Path, output: Path) -> None:
    """保存真实音频及边界输入的逐阶段观察结果。

    Args:
        reference: 固定参考音频。
        output: 报告文件。
    Returns:
        无；拆解和真实库路径不一致时立即失败。
    """
    extractor = WhisperFeatureExtractor(chunk_length=8)
    audio, rate = sf.read(reference, dtype="float32")
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    audio = librosa.resample(audio, orig_sr=rate, target_sr=16000)
    cases = {
        "reference": audio,
        "silence": np.zeros(128000, np.float32),
        "tiny": audio * np.float32(1e-8),
        "short": audio[:319],
        "boundary_160": audio[:160],
        "boundary_161": audio[:161],
        "long": np.tile(audio, 5),
    }
    rows = {}
    for name, samples in cases.items():
        window = np.pad(samples[-128000:], (max(0, 128000 - len(samples)), 0)).astype(
            np.float32
        )
        normalized = extractor.zero_mean_unit_var_norm([window], None)[0]
        rows[name] = compare_stages(normalized, extractor)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(
            {
                "numerical_acceptance": "未验证",
                "limitation": "逐阶段观测已与库路径核对，FFT 和 log10 的冻结误差界尚未得到证明",
                "cases": rows,
                "reference": {
                    "path": str(reference.resolve()),
                    "sha256": file_sha256(reference),
                },
                "extractor_config": extractor.to_dict(),
                "environment": environment_fingerprint(),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(
        {
            "cases": len(rows),
            "reconstructed_outputs_exact": True,
            "numerical_acceptance": "unverified",
        }
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    run(args.reference, args.output)
