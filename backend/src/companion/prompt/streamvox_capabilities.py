from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from companion.core.runtime_paths import BACKEND_ROOT


STREAMVOX_MODEL_CAPABILITY_REGISTRY: dict[str, str] = {
    "s2-pro-4b-gguf": "s2-pro-4b-gguf.md",
    "voxcpm2-gguf": "voxcpm2-gguf.md",
}

STREAMVOX_MODEL_CAPABILITY_DIR = BACKEND_ROOT / "data" / "streamvox_model"
STREAMVOX_TAG_PATTERN = re.compile(r"\[[^\[\]\r\n]{1,80}\]")
FRONTMATTER_DELIMITER = "---"


@dataclass(frozen=True)
class StreamVoxCapability:
    """描述当前 StreamVox 模型可注入提示词的副语言能力。"""

    model: str
    document_name: str
    document_path: Path
    prompt_document: str
    explicit_tags: frozenset[str]


@dataclass(frozen=True)
class StreamVoxCapabilityDocument:
    """描述从模型能力文档中拆出的机器元数据和提示词正文。"""

    prompt_document: str
    allowed_tags: frozenset[str]


class StreamVoxTagSanitizer:
    """把 TTS 句段中的 StreamVox 标签清洗为可合成文本。"""

    def __init__(self, capability: StreamVoxCapability | None) -> None:
        """初始化标签清洗器。

        Args:
            capability: 当前模型已加载的 StreamVox 能力；为空时剥离所有标签。
        """

        self._capability = capability

    def sanitize_sentence(self, sentence: str) -> str:
        """清洗一个已经切分完成的 TTS 句段。

        Args:
            sentence: 已经由句子缓冲切出的待合成句段。

        Returns:
            只保留当前模型允许标签的 TTS 句段。
        """

        if not sentence:
            return ""

        buffer = sentence
        text_parts: list[str] = []

        while buffer:
            tag_start = buffer.find("[")
            if tag_start < 0:
                text_parts.append(buffer)
                break

            if tag_start > 0:
                text_parts.append(buffer[:tag_start])
                buffer = buffer[tag_start:]
                continue

            tag_end = buffer.find("]")
            if tag_end < 0:
                text_parts.append(self._body_after_unfinished_tag(buffer))
                break

            candidate_tag = buffer[: tag_end + 1]
            remainder = buffer[tag_end + 1 :]
            skipped_whitespace, remainder = self._consume_leading_inline_whitespace(remainder)
            if self._is_allowed_tag(candidate_tag):
                text_parts.append(f"{candidate_tag} ")
            elif skipped_whitespace:
                # 非法标签被剥离后，紧随标签的分隔空白也不应泄漏到展示文本开头。
                pass

            buffer = remainder

        return "".join(text_parts)

    def _is_allowed_tag(self, tag: str) -> bool:
        """判断标签是否符合当前模型能力。"""

        if self._capability is None:
            return False

        # 代码层只做稳定兜底：任何模型都只放行能力文档中明确写出的完整标签。
        return tag in self._capability.explicit_tags

    def _body_after_unfinished_tag(self, text: str) -> str:
        """从未闭合标签中尽量恢复标签后的正文。"""

        stripped_text = text.lstrip()
        if not stripped_text.startswith("["):
            return text

        for index, character in enumerate(stripped_text):
            if character.isspace():
                return stripped_text[index + 1 :].lstrip()
        return ""

    def _consume_leading_inline_whitespace(self, text: str) -> tuple[bool, str]:
        """消费标签后的行内空白，避免展示文本残留标签分隔符。"""

        index = 0
        while index < len(text) and text[index].isspace():
            index += 1
        return index > 0, text[index:]


def resolve_streamvox_capability(model: str) -> StreamVoxCapability | None:
    """读取当前 StreamVox 模型对应的能力文档。

    Args:
        model: 当前 StreamVox 运行模型名。

    Returns:
        能力文档存在时返回能力对象；未登记或文档缺失时返回 None。
    """

    normalized_model = model.strip().lower()
    document_name = STREAMVOX_MODEL_CAPABILITY_REGISTRY.get(normalized_model, "").strip()
    if not document_name:
        return None

    document_path = STREAMVOX_MODEL_CAPABILITY_DIR / document_name
    if not document_path.is_file():
        return None

    capability_document = _parse_capability_document(document_path.read_text(encoding="utf-8"))
    return StreamVoxCapability(
        model=normalized_model,
        document_name=document_name,
        document_path=document_path,
        prompt_document=capability_document.prompt_document,
        explicit_tags=capability_document.allowed_tags,
    )


def create_streamvox_tag_sanitizer(model: str) -> StreamVoxTagSanitizer:
    """创建当前 StreamVox 模型对应的标签清洗器。

    Args:
        model: 当前 StreamVox 运行模型名。

    Returns:
        可清洗 TTS 句段标签的清洗器。
    """

    return StreamVoxTagSanitizer(resolve_streamvox_capability(model))


def _parse_capability_document(raw_document: str) -> StreamVoxCapabilityDocument:
    """解析模型能力文档。

    Args:
        raw_document: 从 markdown 文件读取的原始正文。

    Returns:
        拆除 frontmatter 后的提示词正文，以及代码层允许进入 TTS 的标签白名单。
    """

    normalized_document = raw_document.strip()
    if not normalized_document.startswith(f"{FRONTMATTER_DELIMITER}\n"):
        return StreamVoxCapabilityDocument(
            prompt_document=normalized_document,
            allowed_tags=frozenset(),
        )

    _, frontmatter_text, prompt_document = normalized_document.split(FRONTMATTER_DELIMITER, maxsplit=2)
    metadata = yaml.safe_load(frontmatter_text.strip()) or {}
    raw_allowed_tags = metadata.get("allowed_tags", [])
    if not isinstance(raw_allowed_tags, list):
        raw_allowed_tags = []

    allowed_tags = {
        tag
        for item in raw_allowed_tags
        if isinstance(item, str)
        for tag in _extract_complete_tags(item)
    }
    return StreamVoxCapabilityDocument(
        prompt_document=prompt_document.strip(),
        allowed_tags=frozenset(allowed_tags),
    )


def _extract_complete_tags(text: str) -> set[str]:
    """从单段文本中抽取完整标签。"""

    return {match.group(0) for match in STREAMVOX_TAG_PATTERN.finditer(text)}
