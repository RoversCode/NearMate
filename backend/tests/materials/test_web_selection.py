import json
from pathlib import Path

import pytest

from companion.materials.web_selection import WebSelectionError, read_web_selection


def test_web_selection_v2_keeps_plain_text_links_and_images_in_order(
    tmp_path: Path,
) -> None:
    """v2 必须同时保留权威原文和按 DOM 顺序排列的增强片段。"""

    path = _write_selection(
        tmp_path,
        {
            "version": 2,
            "plain_text": "查看产品说明",
            "parts": [
                {"type": "text", "text": "查看"},
                {
                    "type": "link",
                    "text": "产品说明",
                    "url": "https://example.com/manual?q=1#part",
                },
                {"type": "image_url", "url": "https://example.com/cover.png"},
            ],
        },
    )

    selection = read_web_selection(path)

    assert selection.plain_text == "查看产品说明"
    assert [part.kind for part in selection.parts] == ["text", "link", "image_url"]
    assert selection.parts[1].text == "产品说明"
    assert selection.parts[1].url == "https://example.com/manual?q=1#part"


@pytest.mark.parametrize(
    "url",
    (
        "http://localhost/error.png",
        "https://127.0.0.1/error.png",
        "https://user:pass@example.com/error.png",
        "http:example.com",
        "http:/example.com",
        r"https:\\example.com\error.png",
        "/relative/error.png",
    ),
)
def test_web_selection_keeps_nonempty_url_metadata_without_semantic_parsing(
    tmp_path: Path,
    url: str,
) -> None:
    """URL 是交给 Background 的不可信资料，Backend 不负责补全或解释。"""

    path = _write_selection(
        tmp_path,
        {
            "version": 2,
            "plain_text": None,
            "parts": [{"type": "image_url", "url": url}],
        },
    )

    selection = read_web_selection(path)

    assert len(selection.parts) == 1
    assert selection.parts[0].url == url


@pytest.mark.parametrize(
    ("part", "expected_url"),
    (
        (
            {"type": "link", "text": "说明", "url": "  http:example.com\t"},
            "http:example.com",
        ),
        (
            {"type": "image_url", "url": "\r\n/relative/error.png  "},
            "/relative/error.png",
        ),
    ),
)
def test_web_selection_only_trims_url_metadata_boundaries(
    tmp_path: Path,
    part: dict[str, object],
    expected_url: str,
) -> None:
    """Backend 只清理 URL 首尾空白，不补全或规范化地址。"""

    path = _write_selection(
        tmp_path,
        {
            "version": 2,
            "plain_text": None,
            "parts": [part],
        },
    )

    selection = read_web_selection(path)

    assert selection.parts[0].url == expected_url


def test_web_selection_accepts_image_only_link_with_null_text(tmp_path: Path) -> None:
    """图片链接没有可见文字时仍必须保留独立链接目标。"""

    path = _write_selection(
        tmp_path,
        {
            "version": 2,
            "plain_text": None,
            "parts": [
                {"type": "link", "text": None, "url": "https://example.com/article"},
                {"type": "image_url", "url": "https://example.com/cover.png"},
            ],
        },
    )

    selection = read_web_selection(path)

    assert selection.parts[0].kind == "link"
    assert selection.parts[0].text is None


def test_web_selection_rejects_v1_without_compatibility_branch(tmp_path: Path) -> None:
    """Desktop 与 Backend 同版本发布，旧协议必须明确拒绝而不是静默猜测。"""

    path = _write_selection(
        tmp_path,
        {
            "version": 1,
            "parts": [{"type": "image_url", "url": "https://example.com/cover.png"}],
        },
    )

    with pytest.raises(WebSelectionError, match="顶层字段不合法|版本不受支持"):
        read_web_selection(path)


@pytest.mark.parametrize(
    "payload",
    (
        {"version": 2, "plain_text": 123, "parts": [{"type": "image_url", "url": "https://e.com/a"}]},
        {"version": 2, "plain_text": "  ", "parts": [{"type": "image_url", "url": "https://e.com/a"}]},
        {"version": 2, "plain_text": None, "parts": [{"type": "text", "text": "只有文字"}]},
        {"version": 2, "plain_text": None, "parts": [{"type": "link", "text": "  ", "url": "https://e.com"}]},
        {"version": 2, "plain_text": None, "parts": [{"type": "link", "text": "说明", "url": None}]},
        {"version": 2, "plain_text": None, "parts": [{"type": "link", "text": "说明", "url": 123}]},
        {"version": 2, "plain_text": None, "parts": [{"type": "link", "text": "说明", "url": "  "}]},
        {"version": 2, "plain_text": None, "parts": [{"type": "image_url", "url": ""}]},
        {"version": 2, "plain_text": None, "parts": [{"type": "image_url", "url": "  "}]},
    ),
)
def test_web_selection_rejects_invalid_v2_structures(
    tmp_path: Path,
    payload: dict[str, object],
) -> None:
    """v2 必须保持严格字段、合法文字和至少一项链接或图片增强。"""

    path = _write_selection(tmp_path, payload)

    with pytest.raises(WebSelectionError):
        read_web_selection(path)


def _write_selection(tmp_path: Path, payload: dict[str, object]) -> Path:
    """写入一份网页选区测试文件。

    Args:
        tmp_path: pytest 提供的临时目录。
        payload: 需要写入并交给严格解析器的 JSON 对象。

    Returns:
        已写入的选区文件路径。
    """

    path = tmp_path / "selection.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path
