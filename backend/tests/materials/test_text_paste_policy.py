from companion.materials.text_paste_policy import (
    MAX_INLINE_TEXT_BYTES,
    MAX_INLINE_TEXT_TOKENS,
    classify_text_paste,
)


def test_inline_text_token_boundary_is_inclusive() -> None:
    """500 token 保留为正文，501 token 升级为正式长粘贴材料。"""

    at_limit = classify_text_paste(
        "正文",
        token_counter=lambda _value: MAX_INLINE_TEXT_TOKENS,
    )
    over_limit = classify_text_paste(
        "正文",
        token_counter=lambda _value: MAX_INLINE_TEXT_TOKENS + 1,
    )

    assert at_limit.disposition == "inline"
    assert over_limit.disposition == "material"


def test_inline_text_byte_boundary_is_inclusive() -> None:
    """16 KiB 保留为正文，超过一个字节即升级为正式长粘贴材料。"""

    at_limit = classify_text_paste(
        "a" * MAX_INLINE_TEXT_BYTES,
        token_counter=lambda _value: 1,
    )
    over_limit = classify_text_paste(
        "a" * (MAX_INLINE_TEXT_BYTES + 1),
        token_counter=lambda _value: 1,
    )

    assert at_limit.disposition == "inline"
    assert over_limit.disposition == "material"
