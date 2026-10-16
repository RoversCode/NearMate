"""运行 CUDA/DirectML Text EOU 对拍；缺少硬件或模型时明确输出未验证。"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any


def build_unverified_report(*, reason: str, model_dir: Path) -> dict[str, Any]:
    """构造不能被误读为通过的未验证报告。"""

    return {
        "status": "unverified",
        "passed": False,
        "reason": reason,
        "model_dir": str(model_dir),
        "max_absolute_error_budget": 1e-4,
    }


def write_report(path: Path, payload: dict[str, Any]) -> None:
    """创建父目录并写入 JSON 报告。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    """执行两个独立 Python 环境的探针并比较结果。"""

    parser = argparse.ArgumentParser(description="Compare CUDA and DirectML Text EOU in isolated runtimes")
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--texts", type=Path, required=True)
    parser.add_argument("--cuda-python", type=Path, required=True)
    parser.add_argument("--dml-python", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    if not args.model_dir.is_dir():
        report = build_unverified_report(reason="Text EOU 模型目录不存在", model_dir=args.model_dir)
        write_report(args.output, report)
        return 2
    if not args.texts.is_file():
        report = build_unverified_report(reason="固定 Text EOU 输入文件不存在", model_dir=args.model_dir)
        write_report(args.output, report)
        return 2
    missing = [str(path) for path in (args.cuda_python, args.dml_python) if not path.is_file()]
    if missing:
        report = build_unverified_report(reason=f"独立 Python 运行时不存在：{', '.join(missing)}", model_dir=args.model_dir)
        write_report(args.output, report)
        return 2

    output_dir = args.output.parent
    output_dir.mkdir(parents=True, exist_ok=True)
    cuda_result = output_dir / "text-eou-cuda.json"
    dml_result = output_dir / "text-eou-dml.json"
    for python_exe, provider, result_path in (
        (args.cuda_python, "CUDAExecutionProvider", cuda_result),
        (args.dml_python, "DmlExecutionProvider", dml_result),
    ):
        subprocess.run(
            [
                str(python_exe),
                str(Path(__file__).with_name("text_eou_probe.py")),
                "--model-dir",
                str(args.model_dir),
                "--texts",
                str(args.texts),
                "--provider",
                provider,
                "--output",
                str(result_path),
            ],
            check=True,
        )

    completed = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).with_name("text_eou_compare.py")),
            "--cuda-result",
            str(cuda_result),
            "--dml-result",
            str(dml_result),
            "--output",
            str(args.output),
        ],
        check=False,
    )
    payload = json.loads(args.output.read_text(encoding="utf-8"))
    payload["status"] = "verified" if completed.returncode == 0 else "failed"
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
