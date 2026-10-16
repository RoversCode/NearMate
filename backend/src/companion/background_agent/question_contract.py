"""统一清洗 Claude Code AskUserQuestion 的公开问题契约。"""

from __future__ import annotations


MAX_AGENT_QUESTIONS = 10
MAX_AGENT_QUESTION_CHARS = 1000
MAX_AGENT_QUESTION_HEADER_CHARS = 200
MAX_AGENT_OPTION_COUNT = 20
MAX_AGENT_OPTION_LABEL_CHARS = 200
MAX_AGENT_OPTION_DESCRIPTION_CHARS = 500


def normalize_agent_questions(
    value: object,
) -> tuple[dict[str, object], ...] | None:
    """把 SDK 问题列表解析成所有消费者都能使用的严格快照。

    Args:
        value: AskUserQuestion 的原始 questions 字段。

    Returns:
        字段完整、顺序稳定且问题原文唯一的不可变问题快照；输入不符合契约时返回空值。
    """

    if not isinstance(value, list) or not value or len(value) > MAX_AGENT_QUESTIONS:
        return None

    normalized: list[dict[str, object]] = []
    question_texts: set[str] = set()
    for raw_question in value:
        if not isinstance(raw_question, dict):
            return None
        question = _required_text(raw_question.get("question"), MAX_AGENT_QUESTION_CHARS)
        if not question or question in question_texts:
            return None
        question_texts.add(question)

        raw_options = raw_question.get("options", [])
        if not isinstance(raw_options, list) or len(raw_options) > MAX_AGENT_OPTION_COUNT:
            return None
        options: list[dict[str, str]] = []
        for raw_option in raw_options:
            if not isinstance(raw_option, dict):
                return None
            label = _required_text(
                raw_option.get("label"),
                MAX_AGENT_OPTION_LABEL_CHARS,
            )
            if not label:
                return None
            description = _optional_text(
                raw_option.get("description"),
                MAX_AGENT_OPTION_DESCRIPTION_CHARS,
            )
            options.append({"label": label, "description": description})

        normalized.append(
            {
                "question": question,
                "header": _optional_text(
                    raw_question.get("header"),
                    MAX_AGENT_QUESTION_HEADER_CHARS,
                ),
                "options": options,
                "multiSelect": raw_question.get("multiSelect") is True,
            }
        )
    return tuple(normalized)


def normalize_agent_question_answers(
    expected_questions: tuple[str, ...],
    answers: object,
) -> dict[str, str] | None:
    """校验资料回答是否完整对应当前问题集合。

    Args:
        expected_questions: 当前待答请求中按顺序保存的问题原文。
        answers: Foreground 工具或 HTTP API 提交的原始答案映射。

    Returns:
        按问题顺序排列且去掉首尾空白的答案；缺失、多余、空白或重复时返回空值。
    """

    if (
        not expected_questions
        or len(set(expected_questions)) != len(expected_questions)
        or not isinstance(answers, dict)
    ):
        return None

    normalized_answers: dict[str, str] = {}
    for raw_question, raw_answer in answers.items():
        if not isinstance(raw_question, str) or not isinstance(raw_answer, str):
            return None
        question = raw_question.strip()
        answer = raw_answer.strip()
        if not question or not answer or question in normalized_answers:
            return None
        normalized_answers[question] = answer

    if set(normalized_answers) != set(expected_questions):
        return None
    return {
        question: normalized_answers[question]
        for question in expected_questions
    }


def _required_text(value: object, limit: int) -> str:
    """读取必须存在且长度受控的文本。"""

    text = _optional_text(value, limit)
    return text if text and len(text) <= limit else ""


def _optional_text(value: object, limit: int) -> str:
    """读取允许为空但不能超长的文本。"""

    if not isinstance(value, str):
        return ""
    text = value.strip()
    return text if len(text) <= limit else ""
