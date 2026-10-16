from __future__ import annotations

import pytest
from pydantic import ValidationError

from companion.character_cards.models import VoiceCaptionTimingSection


def _caption_timing_payload() -> dict[str, object]:
    """构造一次真实字幕校准会生成的浮点权重载荷。

    Returns:
        包含正数时长、浮点权重和测量时间的合法载荷。
    """

    return {
        "ms_per_unit": 80.645,
        "calibration_text": "我在这里，听见你说话了。先别急，我们一句一句来。",
        "duration_ms": 4000,
        "unit_count": 49.6,
        "measured_at": 1710000000.25,
    }


def test_voice_caption_timing_accepts_weighted_fractional_unit_count() -> None:
    """字幕权重算法生成的小数单位数必须能写入角色卡。"""

    timing = VoiceCaptionTimingSection.model_validate(_caption_timing_payload())

    assert timing.unit_count == 49.6


@pytest.mark.parametrize(
    ("field_name", "invalid_value"),
    [
        ("ms_per_unit", 0),
        ("ms_per_unit", float("inf")),
        ("unit_count", float("nan")),
        ("measured_at", float("-inf")),
        ("duration_ms", 0),
    ],
)
def test_voice_caption_timing_rejects_non_positive_or_non_finite_numbers(
    field_name: str,
    invalid_value: object,
) -> None:
    """无效校准数值必须在 Backend 领域模型处被拒绝。

    Args:
        field_name: 本次替换成非法值的校准字段。
        invalid_value: 零值或无法跨 JSON 安全传输的非有限数值。
    """

    payload = _caption_timing_payload()
    payload[field_name] = invalid_value

    with pytest.raises(ValidationError):
        VoiceCaptionTimingSection.model_validate(payload)
