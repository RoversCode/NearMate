from __future__ import annotations

import io
import re
import time
import unicodedata
import wave

from companion.character_cards.models import VoiceBindingSection, VoiceCaptionTimingSection

CAPTION_CALIBRATION_TEXT = (
    "我在这里，听见你说话了。先别急，我们一句一句来。"
    "如果需要我帮忙，我会先说明要做什么，再开始处理。"
)

_TAG_UNIT_WEIGHTS = {
    "[short pause]": 1.5,
    "[pause]": 3.0,
    "[emphasis]": 0.8,
}
_SOFT_PUNCTUATION_UNIT = 0.45
_HARD_PUNCTUATION_UNIT = 0.9
_ELLIPSIS_UNIT = 1.3
_LATIN_WORD_UNIT = 1.6
_NUMBER_GROUP_UNIT = 1.4
_TAG_PATTERN = re.compile(r"\[[a-zA-Z ]+\]")


def invalidate_changed_voice_caption_timing(
    previous: VoiceBindingSection,
    updated: VoiceBindingSection,
) -> VoiceBindingSection:
    """语音绑定变化后清除旧语速，同一绑定保留已经测量的结果。

    Args:
        previous: 变更前的角色语音绑定。
        updated: 准备保存的新语音绑定。

    Returns:
        可保存的绑定；模型、素材或推理参数变化时不再携带旧字幕校准。
    """
    # 字幕校准本身的更新不算更换音色，否则刚测量的结果也会被清掉。
    excluded = {"caption_timing"}
    if previous.model_dump(exclude=excluded) == updated.model_dump(exclude=excluded):
        return updated
    return updated.model_copy(update={"caption_timing": None})


def calculate_caption_unit_count(text: str) -> float:
    """计算字幕文本的加权朗读单位数。

    Args:
        text: 待折算的字幕文本，可包含 StreamVox 控制标签。

    Returns:
        至少为 1 的加权单位数，用于把真实音频时长转换成平均字幕推进速度。
    """

    index = 0
    units = 0.0
    while index < len(text):
        matched_tag = _TAG_PATTERN.match(text, index)
        if matched_tag is not None:
            units += _TAG_UNIT_WEIGHTS.get(matched_tag.group(0).lower(), 0.6)
            index = matched_tag.end()
            continue

        current_char = text[index]
        if current_char.isspace():
            index += 1
            continue

        if current_char.isascii() and current_char.isalpha():
            next_index = index + 1
            while next_index < len(text) and text[next_index].isascii() and text[next_index].isalpha():
                next_index += 1
            units += _LATIN_WORD_UNIT
            index = next_index
            continue

        if current_char.isdigit():
            next_index = index + 1
            while next_index < len(text) and text[next_index].isdigit():
                next_index += 1
            units += max(_NUMBER_GROUP_UNIT, (next_index - index) * 0.55)
            index = next_index
            continue

        units += _character_unit_weight(current_char)
        index += 1

    return round(max(1.0, units), 3)


def measure_wav_duration_ms(wav_bytes: bytes) -> int:
    """从 WAV header 和帧数读取音频真实时长。

    Args:
        wav_bytes: StreamVox 试听接口返回的 WAV 字节。

    Returns:
        四舍五入后的音频时长毫秒数。

    Raises:
        ValueError: 音频为空、不是合法 WAV 或时长为零。
    """

    if not wav_bytes:
        raise ValueError("校准音频为空")

    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as wav_file:
            frame_rate = wav_file.getframerate()
            frame_count = wav_file.getnframes()
    except wave.Error as exception:
        raise ValueError("校准音频不是合法 WAV") from exception

    if frame_rate <= 0 or frame_count <= 0:
        raise ValueError("校准音频时长为空")

    return int(round(frame_count / frame_rate * 1000))


def build_caption_timing_from_wav(
    wav_bytes: bytes,
    *,
    calibration_text: str = CAPTION_CALIBRATION_TEXT,
    measured_at: float | None = None,
) -> VoiceCaptionTimingSection:
    """根据校准音频构造角色卡字幕节奏字段。

    Args:
        wav_bytes: 标准校准文本合成出的 WAV 字节。
        calibration_text: 本次用于合成的标准文本。
        measured_at: 可注入时间戳，测试可固定。

    Returns:
        可直接写入角色卡的字幕节奏校准结果。
    """

    duration_ms = measure_wav_duration_ms(wav_bytes)
    unit_count = calculate_caption_unit_count(calibration_text)
    return VoiceCaptionTimingSection(
        ms_per_unit=round(duration_ms / unit_count, 3),
        calibration_text=calibration_text,
        duration_ms=duration_ms,
        unit_count=unit_count,
        measured_at=measured_at if measured_at is not None else time.time(),
    )


def _character_unit_weight(character: str) -> float:
    """按单字符类型返回字幕权重。"""

    if character in {"，", "、", "；", ",", ";", "：", ":"}:
        return _SOFT_PUNCTUATION_UNIT
    if character in {"。", "！", "？", ".", "!", "?"}:
        return _HARD_PUNCTUATION_UNIT
    if character in {"…", "—"}:
        return _ELLIPSIS_UNIT
    if _is_cjk_character(character):
        return 1.1
    return 0.7


def _is_cjk_character(character: str) -> bool:
    """判断字符是否属于中日韩表意文字范围。"""

    return "CJK" in unicodedata.name(character, "")
