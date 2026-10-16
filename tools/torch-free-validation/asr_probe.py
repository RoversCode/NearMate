"""验证无 Torch 环境中的 Qwen ASR 编码和真实分块语音识别。"""

from __future__ import annotations

import argparse
import dataclasses
import json
from pathlib import Path
from time import perf_counter

from ort_trace import install_trace

install_trace()


def run(models: Path, reference: Path, output: Path, baseline: bool = False) -> None:
    """使用真实音频运行分块识别，并保存实际计算后端。

    Args:
        models: Qwen ASR 模型目录。
        reference: 输入的参考音频。
        output: 报告保存目录。
        baseline: 显式加载旧环境中与候选对应的计算库。
    Returns:
        无；后端回退或推理失败时抛出异常。
    """
    if baseline:
        from baseline_runtime import initialize_baseline_runtime

        initialize_baseline_runtime()
    from companion.audio.qwen_asr_gguf.inference.asr import QwenASREngine
    from companion.audio.qwen_asr_gguf.inference.audio import load_audio
    from companion.audio.qwen_asr_gguf.inference.schema import ASREngineConfig
    from environment_fingerprint import environment_fingerprint

    output.mkdir(parents=True, exist_ok=True)
    started = perf_counter()
    engine = QwenASREngine(
        ASREngineConfig(
            model_dir=str(models),
            onnx_provider="cuda",
            llm_backend="cuda",
            verbose=False,
            chunk_size=4.0,
        )
    )
    try:
        providers = [
            engine.encoder.sess_fe.get_providers(),
            engine.encoder.sess_be.get_providers(),
        ]
        if (
            any(item[0] != "CUDAExecutionProvider" for item in providers)
            or engine.model.actual_backend != "cuda"
        ):
            raise RuntimeError(
                f"ASR 计算后端回退：{providers}, {engine.model.actual_backend}"
            )
        load_seconds = perf_counter() - started
        started = perf_counter()
        # 实时输入链传入完整波形；文件包装层 duration=0 会得到空音频，不能作为有效验证。
        audio = load_audio(str(reference))
        if not len(audio):
            raise ValueError("参考音频为空")
        result = engine.asr(
            audio, context="", language="Chinese", chunk_size_sec=4.0, memory_chunks=1
        )
        if result.performance["prefill_tokens"] == 0 or not result.text.strip():
            raise AssertionError("真实语音未执行有效识别")
        report = {
            "load_seconds": load_seconds,
            "inference_seconds": perf_counter() - started,
            "providers": providers,
            "llama_backend": engine.model.actual_backend,
            "reference": str(reference),
            "result": dataclasses.asdict(result),
            "environment": environment_fingerprint(),
            "scope": "真实分块识别运行；固定 token 历史下的 LLM 数值对拍仍需单独验收",
        }
        (output / "run.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(report["result"], flush=True)
    finally:
        engine.shutdown()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--models", type=Path, required=True)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--baseline", action="store_true")
    args = parser.parse_args()
    run(args.models, args.reference, args.output, args.baseline)
