from __future__ import annotations


def project_speakable_text(text: str) -> str:
    """投影出参与实时分句门槛判断的可朗读文本。

    Args:
        text: LLM 流式输出的原始文本，可能夹带方括号控制标签。

    Returns:
        移除完整方括号控制块后的文本；如果末尾存在未闭合控制块，则只保留控制块之前的文本。
    """

    if not text:
        return text

    parts: list[str] = []
    index = 0
    while index < len(text):
        character = text[index]
        if character != "[":
            parts.append(character)
            index += 1
            continue

        tag_end = text.find("]", index + 1)
        if tag_end < 0:
            # 流式文本末尾的半截方括号块仍可能在后续 delta 中闭合，先不让它参与可朗读门槛。
            break

        # 完整方括号块统一视为 TTS 控制语义，不参与最低长度和可播语义判断。
        index = tag_end + 1

    return "".join(parts)
