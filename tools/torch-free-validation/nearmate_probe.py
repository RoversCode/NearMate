"""检查 NearMate 语音输入的真实模型组件。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from time import perf_counter

import librosa
import numpy as np
import soundfile as sf


def run(args) -> None:
    """用固定参考音频运行输入链的模型并保存结果。

    Args:
        args: 模型目录、参考音频和输出目录。
    Returns:
        无；生成特征数组与模型结果报告。
    """
    if args.baseline:
        import torch  # noqa: F401 基准显式预加载原有计算库，候选进程绝不执行
    from companion.audio.bootstrap.realtime_asr_runtime import (
        SileroVadRuntimeConfig,
        SileroVadSession,
    )
    from companion.audio.bootstrap.smart_turn_runtime import (
        SmartTurnRuntimeConfig,
        build_smart_turn_input_features,
        create_onnx_session,
    )
    from companion.audio.bootstrap.speaker_verification_runtime import (
        SpeakerVerificationRuntimeConfig,
        create_speaker_embedding_extractor,
    )
    from companion.audio.bootstrap.wakeword_runtime import (
        DECODER_MODEL_NAME,
        ENCODER_MODEL_NAME,
        JOINER_MODEL_NAME,
        WakewordRuntimeConfig,
        create_keyword_spotter,
        keywords_to_tokens,
    )
    from companion.audio.providers.text_eou_provider import (
        RealTextEouProvider,
        TextEouProviderConfig,
    )
    from companion.audio.providers.smart_turn_provider import (
        RealSmartTurnProvider,
        SmartTurnProviderConfig,
    )
    from environment_fingerprint import environment_fingerprint, file_sha256
    from model_identity import model_identity

    root = Path(args.models)
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    audio, rate = sf.read(args.reference, dtype="float32")
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    audio = librosa.resample(audio, orig_sr=rate, target_sr=16000)
    report = {}
    features = {}
    config = SmartTurnRuntimeConfig(model_path=root / "smart-turn-v3.2-gpu.onnx")
    session = create_onnx_session(config)
    smart_turn_provider = RealSmartTurnProvider(
        config=SmartTurnProviderConfig(runtime_config=config)
    )
    for name, samples in {
        "reference": audio,
        "silence": np.zeros(128000, np.float32),
        "tiny": audio * 1e-8,
        "short": audio[:319],
        "long": np.tile(audio, 3),
        "boundary_160": audio[:160],
        "boundary_161": audio[:161],
    }.items():
        started = perf_counter()
        value = build_smart_turn_input_features(samples, config)
        result = session.run(None, {"input_features": value})
        features[name] = value
        report["smart_turn:" + name] = {
            "probability": float(np.asarray(result[0]).reshape(-1)[0]),
            "seconds": perf_counter() - started,
            "provider": session.get_providers()[0],
        }
        # 公共入口还会做 PCM 峰值归一化，不能用直接特征调用替代用户实际走的链路。
        pcm16 = np.clip(samples * 32768, -32768, 32767).astype(np.int16).tobytes()
        try:
            decision = smart_turn_provider.decide_turn_end(pcm16)
            report["smart_turn_public:" + name] = {
                "probability": decision.probability,
                "is_turn_finished": decision.is_turn_finished,
                "finite": bool(np.isfinite(decision.probability)),
                "threshold": smart_turn_provider.config.finish_threshold,
            }
        except Exception as error:  # noqa: BLE001 保留旧静音入口的异常，不能删除失败样例
            report["smart_turn_public:" + name] = {
                "error": f"{type(error).__name__}: {error}"
            }
    np.savez(output / "smart-turn-features.npz", **features)
    vad = SileroVadSession(
        SileroVadRuntimeConfig(model_path=root / "qwen_asr/silero_vad.onnx")
    )
    report["vad"] = {
        "probabilities": [
            vad.infer(audio[start : start + 512])[0] for start in range(0, 512 * 8, 512)
        ],
        "provider": vad._session.get_providers()[0],
    }
    try:
        started = perf_counter()
        speaker_config = SpeakerVerificationRuntimeConfig(
            model_path=root / "3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx",
            whitelist_audio_paths=(),
            provider="cpu",
            num_threads=1,
        )
        extractor = create_speaker_embedding_extractor(speaker_config)
        stream = extractor.create_stream()
        stream.accept_waveform(16000, audio.astype(np.float32))
        stream.input_finished()
        embedding = np.asarray(extractor.compute(stream), dtype=np.float32)
        np.save(output / "speaker-embedding.npy", embedding)
        report["speaker_verification"] = {
            "status": "运行成功，数值验收未完成",
            "provider": "cpu",
            "shape": list(embedding.shape),
            "dtype": str(embedding.dtype),
            "seconds": perf_counter() - started,
        }
    except Exception as error:  # noqa: BLE001 先保存每个组件的故障，末尾统一以失败退出
        report["speaker_verification"] = {
            "status": "未验证",
            "error": f"{type(error).__name__}: {error}",
        }
    try:
        wake_dir = root / "wakeword"
        wake_config = WakewordRuntimeConfig(
            model_dir=wake_dir,
            keywords_file=wake_dir / "keywords.txt",
            tokens_file=wake_dir / "tokens.txt",
            encoder=wake_dir / ENCODER_MODEL_NAME,
            decoder=wake_dir / DECODER_MODEL_NAME,
            joiner=wake_dir / JOINER_MODEL_NAME,
            keywords=("你好小练",),
            provider="cpu",
        )
        token_text = keywords_to_tokens(wake_config.keywords, wake_config)
        spotter = create_keyword_spotter(wake_config)
        stream = spotter.create_stream(keywords=token_text)
        detected = []
        for start in range(0, len(audio), 512):
            stream.accept_waveform(16000, audio[start : start + 512])
            while spotter.is_ready(stream):
                spotter.decode_stream(stream)
                value = spotter.get_result(stream)
                if value:
                    detected.append(value)
                    spotter.reset_stream(stream)
        report["wakeword"] = {
            "status": "已运行，未验证命中",
            "provider": wake_config.provider,
            "keywords": list(wake_config.keywords),
            "token_text": token_text,
            "detected": detected,
        }
    except Exception as error:  # noqa: BLE001 先保存每个组件的故障，末尾统一以失败退出
        report["wakeword"] = {
            "status": "未验证",
            "error": f"{type(error).__name__}: {error}",
        }
    try:
        eou_dir = root / "turn_detector"
        eou = RealTextEouProvider(
            config=TextEouProviderConfig(
                model_dir=str(eou_dir), providers=("CPUExecutionProvider",)
            )
        )
        report["text_eou"] = {
            "status": "运行成功，数值验收未完成",
            "provider": eou._session.get_providers()[0],
            "probability": eou.predict_end_probability("你好，今天怎么样？"),
        }
    except Exception as error:  # noqa: BLE001 先保存每个组件的故障，末尾统一以失败退出
        report["text_eou"] = {
            "status": "未验证",
            "error": f"{type(error).__name__}: {error}",
        }
    report["environment"] = environment_fingerprint()
    report["reference"] = {
        "path": str(Path(args.reference).resolve()),
        "sha256": file_sha256(Path(args.reference)),
    }
    report["smart_turn_model"] = model_identity(config.model_path)
    (output / "run.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False), flush=True)
    if any("error" in row for row in report.values()):
        raise RuntimeError("部分语音组件未能运行，详细原因已保存，不能将探针视为通过")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--models", required=True)
    parser.add_argument("--reference", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--baseline", action="store_true")
    run(parser.parse_args())
