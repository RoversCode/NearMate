"""在独立进程中验证真实模型并生成待用户试听的 WAV。"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import sys
from pathlib import Path
from time import perf_counter

import numpy as np

# spawn 会重载入口模块，必须在它导入模型代码前安装同一记录器。
from ort_trace import install_trace

install_trace()


def run(args) -> None:
    """使用模型适配器做开发验收，不构造或修改商业授权服务。

    Args:
        args: 命令行配置，包含模型、参考音频、设备及输出目录。
    Returns:
        无；生成音频和包含真实 session provider 的报告。
    """
    if args.baseline:
        from baseline_runtime import initialize_baseline_runtime

        initialize_baseline_runtime()
    import psutil
    import soundfile as sf
    from environment_fingerprint import environment_fingerprint
    from streamvox.models import default_registry
    from streamvox.models.base import RuntimeRequest

    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    adapter = default_registry.get(args.model)
    plan = adapter.resolve_execution_plan(args.device, llama_backend=args.llama_backend)
    started = perf_counter()
    runtime = adapter.load_runtime(
        RuntimeRequest(
            model_dir=Path(args.models) / args.model,
            device=args.device,
            n_ctx=2048,
            verify_model_sha256=True,
            execution_plan=plan,
            text_preprocess_config=adapter.get_text_preprocess_config(),
        )
    )
    timings = {"load_seconds": perf_counter() - started}
    try:
        started = perf_counter()
        prompt = adapter.make_prompt(
            runtime=runtime,
            role_name="numerical-validation",
            sample_rate=24000,
            audio_path=args.reference,
            prompt_text=args.reference_text,
        )
        timings["reference_seconds"] = perf_counter() - started
        serialized = adapter.serialize_prompt_asset(prompt)
        np.savez(output / "prompt.npz", **serialized.arrays)
        prompt = adapter.deserialize_prompt_asset(serialized)
        started = perf_counter()
        chunks = list(
            adapter.synthesize(runtime=runtime, text=args.text, prompt=prompt)
        )
        timings["synthesis_seconds"] = perf_counter() - started
        audio = np.concatenate([np.asarray(chunk).reshape(-1) for chunk in chunks])
        sf.write(output / "listening.wav", audio, runtime.sample_rate, subtype="FLOAT")
        report = {
            "model": args.model,
            "text": args.text,
            "reference": args.reference,
            "reference_text": args.reference_text,
            "device": args.device,
            "llama_backend": args.llama_backend,
            "python": sys.version,
            "timings": timings,
            "chunk_samples": [int(np.asarray(c).size) for c in chunks],
            "sample_rate": runtime.sample_rate,
            "audio_samples": int(audio.size),
            "rss_bytes_at_end": psutil.Process().memory_info().rss,
            "scope": "开发适配器与模型验证；商业授权端到端未验证；音频待用户试听",
            "packages": {
                dist.metadata["Name"]: dist.version
                for dist in importlib.metadata.distributions()
            },
            "environment": environment_fingerprint(),
        }
        (output / "run.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(
            json.dumps(
                {
                    "model": args.model,
                    "timings": timings,
                    "audio_seconds": audio.size / runtime.sample_rate,
                }
            ),
            flush=True,
        )
    finally:
        runtime.shutdown()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--models", required=True)
    parser.add_argument("--reference", required=True)
    parser.add_argument(
        "--reference-text",
        default="所以今天我想要讨论另外两个问题，很相似。可是却不一样的问题，我相信自己是谁。",
    )
    parser.add_argument("--text", default="你好，我是你的语音伙伴。今天过得怎么样？")
    parser.add_argument("--device", default="gpu:0")
    parser.add_argument("--llama-backend", default="cuda")
    parser.add_argument("--output", required=True)
    parser.add_argument("--baseline", action="store_true")
    run(parser.parse_args())
