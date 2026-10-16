"""比较 CUDA 与 DirectML Text EOU 输出的开发验收工具。"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any


DEFAULT_MAX_ABS_ERROR = 1e-4


def compare_probability_records(
    cuda_records: list[dict[str, Any]],
    dml_records: list[dict[str, Any]],
    *,
    max_abs_error: float = DEFAULT_MAX_ABS_ERROR,
    high_finish_probability: float = 0.95,
    unlikely_probability: float = 0.08,
) -> dict[str, Any]:
    """比较两种 ONNX provider 的 Text EOU 概率和业务判定。

    Args:
        cuda_records: CUDA 探针输出的文本、概率和输入摘要。
        dml_records: DirectML 探针输出的文本、概率和输入摘要。
        max_abs_error: 本次 Text EOU 允许的最大绝对概率误差。
        high_finish_probability: 高置信结束阈值。
        unlikely_probability: 低置信未结束阈值。

    Returns:
        包含逐项差异、最大误差和是否通过的 JSON 兼容字典。

    Raises:
        ValueError: 两边样例数量、文本或输入摘要不一致时抛出。
    """

    if len(cuda_records) != len(dml_records):
        raise ValueError("CUDA 与 DirectML 样例数量不一致")

    comparisons: list[dict[str, Any]] = []
    max_abs = 0.0
    max_relative = 0.0
    all_passed = True
    for index, (cuda, dml) in enumerate(zip(cuda_records, dml_records, strict=True)):
        if cuda.get("text") != dml.get("text"):
            raise ValueError(f"第 {index} 个 Text EOU 文本不一致")
        if cuda.get("input_ids") != dml.get("input_ids"):
            raise ValueError(f"第 {index} 个 Text EOU token 输入不一致")

        cuda_probability = float(cuda["probability"])
        dml_probability = float(dml["probability"])
        absolute_error = abs(cuda_probability - dml_probability)
        scale = max(abs(cuda_probability), abs(dml_probability))
        relative_error = absolute_error / scale if scale else 0.0
        cuda_decision = _classify_probability(
            cuda_probability,
            high_finish_probability=high_finish_probability,
            unlikely_probability=unlikely_probability,
        )
        dml_decision = _classify_probability(
            dml_probability,
            high_finish_probability=high_finish_probability,
            unlikely_probability=unlikely_probability,
        )
        passed = absolute_error <= max_abs_error and cuda_decision == dml_decision
        all_passed = all_passed and passed
        max_abs = max(max_abs, absolute_error)
        max_relative = max(max_relative, relative_error)
        comparisons.append(
            {
                "index": index,
                "text": cuda["text"],
                "cuda_probability": cuda_probability,
                "dml_probability": dml_probability,
                "absolute_error": absolute_error,
                "relative_error": relative_error,
                "cuda_decision": cuda_decision,
                "dml_decision": dml_decision,
                "passed": passed,
            }
        )

    return {
        "passed": all_passed,
        "max_absolute_error": max_abs,
        "max_relative_error": max_relative,
        "max_absolute_error_budget": max_abs_error,
        "comparisons": comparisons,
    }


def _classify_probability(
    probability: float,
    *,
    high_finish_probability: float,
    unlikely_probability: float,
) -> str:
    """把概率映射为 Smart Turn 使用的三个业务区间。"""

    if probability >= high_finish_probability:
        return "finish"
    if probability < unlikely_probability:
        return "continue"
    return "ambiguous"


def _load_records(path: Path) -> list[dict[str, Any]]:
    """读取探针 JSON 并校验其基本结构。"""

    payload = json.loads(path.read_text(encoding="utf-8"))
    records = payload.get("records") if isinstance(payload, dict) else None
    if not isinstance(records, list):
        raise ValueError(f"Text EOU 探针文件缺少 records：{path}")
    return [record for record in records if isinstance(record, dict)]


def main() -> None:
    """执行两个 Text EOU 探针结果的比较。"""

    parser = argparse.ArgumentParser(description="Compare CUDA and DirectML Text EOU outputs")
    parser.add_argument("--cuda-result", type=Path, required=True)
    parser.add_argument("--dml-result", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-abs-error", type=float, default=DEFAULT_MAX_ABS_ERROR)
    args = parser.parse_args()
    report = compare_probability_records(
        _load_records(args.cuda_result),
        _load_records(args.dml_result),
        max_abs_error=args.max_abs_error,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
