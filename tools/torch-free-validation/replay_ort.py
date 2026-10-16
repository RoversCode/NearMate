"""在候选环境中重放基线保存的完全相同的 ONNX 输入和缓存。"""

from __future__ import annotations

import argparse
import gc
import json
from pathlib import Path
from time import perf_counter

import numpy as np
from environment_fingerprint import compare_environments, environment_fingerprint
from model_identity import model_identity
from numeric_metrics import compare_arrays


def replay(trace: Path, output: Path, baseline: bool = False) -> None:
    """逐 session 重放同一输入，精确比较所有输出。

    Args:
        trace: 基线输入和输出的保存目录。
        output: 对拍报告的保存路径。
        baseline: 为 True 时使用基线环境的 Torch 运行库做自对拍。
    Returns:
        无；不一致时先写报告再抛出异常。
    """
    if baseline:
        from baseline_runtime import initialize_baseline_runtime

        initialize_baseline_runtime()
    else:
        from streamvox_runtime import (
            ensure_single_onnxruntime_distribution,
            initialize_nvidia_runtime,
        )

        ensure_single_onnxruntime_distribution()
    import onnxruntime as ort

    results = []
    environments = []
    model_checks = []
    for metadata_path in sorted(trace.rglob("session.json")):
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        # 必须在创建 session 前核对图和外部权重。旧记录不能补写今天的摘要冒充历史证据。
        identity = model_identity(Path(metadata["model"]))
        recorded_identity = metadata.get("model_identity")
        identity_match = recorded_identity == identity
        if recorded_identity is None and not identity["external_weights"]:
            # 不含外部权重的图已把参数写在图内，原始图摘要足以证明这一模型的身份。
            identity_match = metadata.get("model_sha256") == identity["graph_sha256"]
        if recorded_identity is not None and not identity_match:
            raise RuntimeError(f"模型图或外部权重已改变：{metadata['model']}")
        model_checks.append(
            {"session": str(metadata_path.relative_to(trace)), "match": identity_match}
        )
        expected_provider = metadata["providers"][0]
        if expected_provider == "CUDAExecutionProvider" and not baseline:
            initialize_nvidia_runtime(required=True)
        options = ort.SessionOptions()
        for name, value in metadata.get("options", {}).items():
            if name == "graph_optimization_level":
                value = ort.GraphOptimizationLevel(value)
            elif name == "execution_mode":
                value = ort.ExecutionMode(value)
            setattr(options, name, value)
        options.log_severity_level = 3
        providers = (
            [
                (name, metadata["provider_options"][name])
                for name in metadata["providers"]
            ]
            if "provider_options" in metadata
            else metadata["providers"]
        )
        session = ort.InferenceSession(
            metadata["model"], sess_options=options, providers=providers
        )
        if session.get_providers() != metadata["providers"]:
            raise RuntimeError(
                f"后端回退：期望 {expected_provider}，实际 {session.get_providers()}"
            )
        if (
            "provider_options" in metadata
            and session.get_provider_options() != metadata["provider_options"]
        ):
            raise RuntimeError(
                f"回放后的 provider 参数发生变化：{session.get_provider_options()}"
            )
        model_hash = identity["graph_sha256"]
        if metadata.get("model_sha256", model_hash) != model_hash:
            raise RuntimeError(f"模型文件已改变：{metadata['model']}")
        for sample in sorted(metadata_path.parent.glob("call-*.npz")):
            with np.load(sample) as saved:
                feeds = {
                    name[6:]: saved[name]
                    for name in saved.files
                    if name.startswith("input:")
                }
                names = [name[7:] for name in saved.files if name.startswith("output:")]
                if not names:
                    raise ValueError(f"样本没有输出，不能做空集合验收：{sample}")
                started = perf_counter()
                values = session.run(names, feeds)
                seconds = perf_counter() - started
                rows = {
                    name: compare_arrays(value, saved["output:" + name])
                    for name, value in zip(names, values, strict=True)
                }
                results.append(
                    {
                        "case": str(sample.relative_to(trace)),
                        "model": metadata["model"],
                        "model_sha256": model_hash,
                        "provider": expected_provider,
                        "seconds": seconds,
                        "outputs": rows,
                    }
                )
        actual_environment = environment_fingerprint()
        environments.append(
            {
                "session": str(metadata_path.relative_to(trace)),
                "capture_mode": metadata.get("capture_mode", "legacy_sampled"),
                "baseline": metadata.get("environment"),
                "replay": actual_environment,
                "comparison": compare_environments(
                    metadata.get("environment"), actual_environment, expected_provider
                ),
            }
        )
        del session
        gc.collect()
    numeric_exact = bool(results) and all(
        row["exact"] for case in results for row in case["outputs"].values()
    )
    environment_match = bool(environments) and all(
        row["comparison"]["match"] for row in environments
    )
    model_match = bool(model_checks) and all(row["match"] for row in model_checks)
    passed = numeric_exact and environment_match and model_match
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(
            {
                "recorded_calls_pass": passed,
                "numeric_exact": numeric_exact,
                "environment_match": environment_match,
                "model_identity_match": model_match,
                "model_checks": model_checks,
                "cases": results,
                "environments": environments,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(
        {
            "cases": len(results),
            "recorded_calls_pass": passed,
            "numeric_exact": numeric_exact,
            "environment_match": environment_match,
            "model_identity_match": model_match,
        },
        flush=True,
    )
    if not passed:
        raise AssertionError(
            "固定输入回放的数值或比较条件未通过；详见报告，禁止自动放宽门槛"
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--trace", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--baseline", action="store_true")
    args = parser.parse_args()
    replay(args.trace, args.output, args.baseline)
