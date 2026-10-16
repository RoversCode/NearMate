from __future__ import annotations

import re

import pytest

from companion.audio.streamvox_models import resolve_streamvox_model_profile
from companion.realtime.sentence_queue import StreamingSentenceBuffer
from companion.text.http_urls import replace_http_urls_for_speech
from companion.text.normalization import normalize_tts_text


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("2010年", "二零一零年"),
        ("2010 年", "二零一零年"),
        ("2026-07-17", "二零二六年七月十七日"),
        ("2026-02-30", "2026-02-30"),
        ("07/17/2026", "二零二六年七月十七日"),
        ("08:05", "八点五分"),
        ("12:30~13:30", "十二点三十分至十三点三十分"),
        ("520", "五二零"),
        ("-520", "负五二零"),
        ("+520", "正五二零"),
        ("520元", "五百二十元"),
        ("360度", "三百六十度"),
        ("2个人", "两个人"),
        ("2 个人", "两个人"),
        ("+2个人", "正两个人"),
        ("000123", "零零零一二三"),
        ("123456789", "一二三四五六七八九"),
        ("¥1,299.99", "一千二百九十九点九九元"),
        ("HK$1,588.00", "一千五百八十八点零零港币"),
        ("1.5万元", "一点五万元"),
        ("95°C", "九十五摄氏度"),
        ("120W", "一百二十瓦"),
        ("1.35V~1.45V", "一点三五伏至一点四五伏"),
        ("2.5m³", "二点五立方米"),
        ("5-8mins", "五至八分钟"),
        ("3~8岁", "三至八岁"),
        ("3~5", "三至五"),
        ("22.15%", "百分之二十二点一五"),
        ("7.5折", "七点五折"),
        ("62.23:1", "六十二点二三比一"),
        ("2 * 3 = 6", "二乘以三等于六"),
        ("x+y=10", "x加y等于十"),
        ("2x-y≥5", "二x减y大于等于五"),
        ("f(x)=2", "f(x)等于二"),
        ("x=-5", "x等于负五"),
        ("-5+2", "负五加二"),
        ("(2+3)*4", "(二加三)乘以四"),
        ("x=(2+3)", "x等于(二加三)"),
        ("2--3", "二减负三"),
        ("(-2)+3", "(负二)加三"),
        ("f(x,y)=2", "f(x,y)等于二"),
        ("x/y=2", "x除以y等于二"),
        ("2/x=1", "二除以x等于一"),
        ("m/s=2", "m除以s等于二"),
        ("package-name", "package-name"),
        ("feature-flag", "feature-flag"),
        ("foo-bar=1", "foo减bar等于一"),
        ("误差±0.01", "误差正负零点零一"),
        ("x²+2³", "x平方加二立方"),
        ("3-5", "三-五"),
        ("Covid-19", "Covid一九"),
        ("Kimi K2.5", "Kimi K二点五"),
        ("v1.2.3", "v一点二点三"),
        ("版本 v1.2.3", "版本v一点二点三"),
        ("版本1.2.3", "版本一点二点三"),
        ("Qwen3-TTS 0.6B", "Qwen三-TTS 零点六B"),
        ("HTTP 404", "HTTP 四零四"),
        ("型号 RTX 4090", "型号RTX四零九零"),
        ("型号RTX4090", "型号RTX四零九零"),
        ("RTX4090", "RTX四零九零"),
        ("100万token", "一百万token"),
        ("2.8T", "二点八T"),
        ("电话010-12345678", "电话零一零一二三四五六七八"),
        ("手机13800138000", "手机一三八零零一三八零零零"),
        ("股票代码300616", "股票代码三零零六一六"),
        ("订单号2026-07-17", "订单号二零二六-零七-一七"),
        ("订单号2026/07/001", "订单号二零二六/零七/零零一"),
        ("编号123_456", "编号一二三_四五六"),
        ("序列号AB-123-CD", "序列号AB-一二三-CD"),
        ("股票代码000001.SZ", "股票代码零零零零零一.SZ"),
        ("电话08:05", "电话零八:零五"),
        ("比例1:23", "比例一比二十三"),
        ("102号房", "一零二号房"),
        ("第2章", "第二章"),
        ("零下-5°C", "零下五摄氏度"),
        ("2020年-2024年", "二零二零年至二零二四年"),
        ("3kg-5g", "三千克-五克"),
    ],
)
def test_deterministic_chinese_semantic_normalization(source: str, expected: str) -> None:
    """验证参考能力对应的确定性中文读法。

    Args:
        source: 待正则化的输入文本。
        expected: 期望得到的中文读法。
    """

    assert normalize_tts_text(source, force_remaining_digits=False) == expected


@pytest.mark.parametrize(
    "source",
    [
        "日期2026-07-17，时间08:05。",
        "价格¥1,299.99，增长22.15%。",
        "模型Kimi K2.5，参数2.8T。",
        "公式（2 * 3 = 6），范围3~8岁。",
        "电话010-12345678，房间102号房。",
    ],
)
def test_normalization_is_idempotent(source: str) -> None:
    """验证同一句文本重复正则化不会继续改变结果。

    Args:
        source: 包含多种语义类型的代表性文本。
    """

    first = normalize_tts_text(source, force_remaining_digits=False)
    second = normalize_tts_text(first, force_remaining_digits=False)

    assert second == first


def test_semantic_punctuation_and_literal_content_are_never_deleted() -> None:
    """括号、集合、URL、邮箱、路径和公式必须保留原有语义内容。"""

    source = (
        "价格（含税）100元，集合{a,b}，公式f(x)=2，"
        "网址https://a.com/v1.2，邮箱a1@example.com，路径C:\\tmp\\v1.txt。"
    )

    normalized = normalize_tts_text(source, force_remaining_digits=False)

    assert "（含税）" in normalized
    assert "{a,b}" in normalized
    assert "f(x)等于二" in normalized
    assert "https://a.com/v1.2" in normalized
    assert "a1@example.com" in normalized
    assert "C:\\tmp\\v1.txt" in normalized
    assert normalize_tts_text(
        "路径src/v1.2/file.py",
        force_remaining_digits=False,
    ) == "路径src/v1.2/file.py"


def test_model_residual_digit_policies_only_diverge_after_semantic_rules() -> None:
    """所有模型共享确定性规则，仅对受保护字面量中的残留数字采用不同策略。"""

    source = "日期2026-07-17，详见https://a.com/v1.2。"

    preserved = normalize_tts_text(source, force_remaining_digits=False)
    forced = normalize_tts_text(source, force_remaining_digits=True)

    assert preserved == "日期二零二六年七月十七日，详见https://a.com/v1.2。"
    assert forced == "日期二零二六年七月十七日，详见https://a.com/v一点二。"
    assert re.search(r"\d", forced) is None


def test_forced_residual_fallback_handles_numbers_beyond_cn2an_range() -> None:
    """超长字面量数字必须退化为逐位读法，不能残留阿拉伯数字。"""

    normalized = normalize_tts_text(
        "详见https://a.com/123456789012345678901234567890。",
        force_remaining_digits=True,
    )

    assert re.search(r"\d", normalized) is None
    assert "https://a.com/一二三四五六七八九零" in normalized


@pytest.mark.parametrize(
    "source",
    ["١٢٣", "版本v١.٢", "年份٢٠٢٦年", "१२३"],
)
def test_unsupported_unicode_digit_scripts_are_preserved(source: str) -> None:
    """非 ASCII 数字脚本不属于本地规则范围，必须原样交给语音模型。

    Args:
        source: 包含 Unicode 数字脚本的文本。
    """

    assert normalize_tts_text(source, force_remaining_digits=False) == source
    assert normalize_tts_text(source, force_remaining_digits=True) == source


def test_streamvox_control_tag_is_preserved_during_normalization() -> None:
    """合法控制标签不得被数字和符号规则改写。"""

    assert normalize_tts_text(
        "[laughing] 价格是520元。",
        force_remaining_digits=True,
    ) == "[laughing] 价格是五百二十元。"

    assert normalize_tts_text(
        "[emotion2] 价格是520元。",
        force_remaining_digits=True,
    ) == "[emotion2] 价格是五百二十元。"


def test_inline_and_fenced_code_preserve_spacing_before_model_fallback() -> None:
    """代码片段只能按模型策略转换数字，空白和语法字符必须保持原样。"""

    source = "`x  =  2`\n```python\nif x:\n\treturn  2\n```"

    assert normalize_tts_text(source, force_remaining_digits=False) == source
    assert normalize_tts_text(source, force_remaining_digits=True) == (
        "`x  =  二`\n```python\nif x:\n\treturn  二\n```"
    )


@pytest.mark.parametrize(
    ("model", "family", "force_remaining_digits"),
    [
        ("qwen3-tts-clone-0.6b-gguf", "qwen3", True),
        ("qwen3-tts-clone-1.7b-gguf", "qwen3", True),
        ("moss-tts-nano-onnx", "moss", True),
        ("s2-pro-4b-gguf", "s2", False),
        ("voxcpm2-gguf", "voxcpm2", False),
        ("future-model", "unknown", False),
    ],
)
def test_streamvox_model_profile_drives_residual_digit_policy(
    model: str,
    family: str,
    force_remaining_digits: bool,
) -> None:
    """模型画像必须集中给出模型族和保守的残留数字策略。

    Args:
        model: 待识别的 StreamVox 模型名称。
        family: 期望得到的模型族。
        force_remaining_digits: 期望采用的残留数字策略。
    """

    profile = resolve_streamvox_model_profile(model)

    assert profile.family == family
    assert profile.force_remaining_digits is force_remaining_digits


def test_streaming_sentence_buffer_preserves_semantic_hyphens() -> None:
    """分句阶段不得在 TTS 正则化之前破坏范围、型号或路径中的短横线。"""

    sentence_buffer = StreamingSentenceBuffer(min_segment_tokens=1, token_counter=len)

    assert sentence_buffer.push_text_delta("Covid-19已经发布。下一句") == [
        "Covid-19已经发布。"
    ]


@pytest.mark.parametrize(
    "source",
    [
        "价格1299.99元。下一句",
        "模型Kimi K2.5已发布。下一句",
        "版本v1.2.3已发布。下一句",
        "网址https://a.com/v1.2。下一句",
        ".env配置完成。下一句",
        "文件.gitignore已加载。下一句",
        "值为.5秒。下一句",
        "标识foo_.bar有效。下一句",
    ],
)
def test_streaming_sentence_buffer_preserves_inline_ascii_dots(source: str) -> None:
    """分句器不得在小数、版本、域名和路径的 ASCII 点号处切断语义。

    Args:
        source: 包含 token 内点号和真实中文句号的流式文本。
    """

    sentence_buffer = StreamingSentenceBuffer(min_segment_tokens=1, token_counter=len)

    assert sentence_buffer.push_text_delta(source) == [source.removesuffix("下一句")]


def test_streaming_sentence_buffer_does_not_split_inside_chunked_url() -> None:
    """跨增量 URL 中的问号、分号和点号都不能成为流式句界。"""

    sentence_buffer = StreamingSentenceBuffer(
        min_segment_tokens=1,
        token_counter=len,
    )

    assert sentence_buffer.push_text_delta(
        "请看 https://example.com/search?q=a"
    ) == []
    assert sentence_buffer.push_text_delta(";b.c&x=1。下一句") == [
        "请看 https://example.com/search?q=a;b.c&x=1。"
    ]
    assert sentence_buffer.flush() == ["下一句"]


@pytest.mark.parametrize("first_url_chunk", ["h", "http", "https:", "https:/", "https://"])
def test_streaming_sentence_buffer_holds_incomplete_url_past_long_buffer_fallback(
    first_url_chunk: str,
) -> None:
    """超过生产默认长缓冲门槛时，未完成 URL 也不得作为分句结果泄漏。"""

    sentence_buffer = StreamingSentenceBuffer(
        min_segment_tokens=1,
        max_segment_tokens=80,
        token_counter=len,
    )
    full_url = "https://example.com/private/path?token=secret;a.b"
    prefix = f"前置信息，{'内容' * 50}，地址是 "

    early_segments = sentence_buffer.push_text_delta(prefix + first_url_chunk)
    assert all("http" not in segment.casefold() for segment in early_segments)

    remaining = full_url[len(first_url_chunk):]
    all_segments = [
        *early_segments,
        *sentence_buffer.push_text_delta(f"{remaining}，已经结束。下一句"),
        *sentence_buffer.flush(),
    ]
    assert full_url in "".join(all_segments)
    assert all(
        "http" not in segment.casefold() or full_url in segment
        for segment in all_segments
    )


def test_streaming_sentence_buffer_final_flush_keeps_complete_chunked_url_for_replacement() -> None:
    """流结束时无句末标点的完整 URL 应整体进入唯一语音替换出口。"""

    sentence_buffer = StreamingSentenceBuffer(
        min_segment_tokens=1,
        token_counter=len,
    )
    assert sentence_buffer.push_text_delta("请查看 h") == []
    assert sentence_buffer.push_text_delta(
        "ttps://example.com/private?token=secret"
    ) == []

    flushed = sentence_buffer.flush()

    assert flushed == ["请查看 https://example.com/private?token=secret"]
    assert [replace_http_urls_for_speech(segment) for segment in flushed] == [
        "请查看 链接"
    ]


def test_streaming_sentence_buffer_final_flush_hides_incomplete_url_from_speech() -> None:
    """生成直接结束时，未补全的 URL 尾巴也必须在语音出口被替换。"""

    sentence_buffer = StreamingSentenceBuffer(
        min_segment_tokens=1,
        token_counter=len,
    )
    assert sentence_buffer.push_text_delta("请查看 https:") == []

    flushed = sentence_buffer.flush()

    assert flushed == ["请查看 https:"]
    assert [replace_http_urls_for_speech(segment) for segment in flushed] == [
        "请查看 链接"
    ]


def test_streaming_sentence_buffer_hides_incomplete_url_before_sentence_mark() -> None:
    """明显未完成的 scheme 即使后接句末标点也不能穿透 TTS 投影。"""

    sentence_buffer = StreamingSentenceBuffer(
        min_segment_tokens=1,
        token_counter=len,
    )

    segments = sentence_buffer.push_text_delta("请查看 https:。下一句")

    assert segments == ["请查看 https:。"]
    assert [replace_http_urls_for_speech(segment) for segment in segments] == [
        "请查看 链接。"
    ]
