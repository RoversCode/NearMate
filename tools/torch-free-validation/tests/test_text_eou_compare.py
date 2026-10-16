from __future__ import annotations

from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from text_eou_compare import compare_probability_records
from text_eou_matrix import build_unverified_report


def _record(text: str, probability: float) -> dict[str, object]:
    return {"text": text, "input_ids": [1, 2, 3], "probability": probability}


def test_text_eou_compare_accepts_small_provider_rounding_difference() -> None:
    report = compare_probability_records(
        [_record("你好", 0.9500000)],
        [_record("你好", 0.9500500)],
    )

    assert report["passed"] is True
    assert report["max_absolute_error"] == pytest.approx(5e-5)
    assert report["comparisons"][0]["cuda_decision"] == "finish"


def test_text_eou_compare_rejects_threshold_decision_change() -> None:
    report = compare_probability_records(
        [_record("边界", 0.94995)],
        [_record("边界", 0.95005)],
    )

    assert report["passed"] is False
    assert report["comparisons"][0]["cuda_decision"] == "ambiguous"
    assert report["comparisons"][0]["dml_decision"] == "finish"


def test_text_eou_compare_rejects_mismatched_token_input() -> None:
    with pytest.raises(ValueError, match="token 输入不一致"):
        compare_probability_records(
            [{"text": "你好", "input_ids": [1], "probability": 0.5}],
            [{"text": "你好", "input_ids": [2], "probability": 0.5}],
        )


def test_missing_hardware_is_reported_as_unverified() -> None:
    report = build_unverified_report(reason="模型不存在", model_dir=Path("missing"))
    assert report["status"] == "unverified"
    assert report["passed"] is False
