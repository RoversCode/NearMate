"""清理 Prompt 审查请求中的凭据和不可读二进制内容。"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import mimetypes
import re
from collections.abc import Mapping
from urllib.parse import urlsplit


_SENSITIVE_KEY_NAMES = frozenset(
    {
        "apikey",
        "authorization",
        "cookie",
        "password",
        "refreshtoken",
        "secret",
        "setcookie",
        "token",
    }
)
_NON_SECRET_TOKEN_KEYS = frozenset(
    {
        "maxtokens",
        "maxoutputtokens",
        "mintokens",
        "outputtokens",
        "reasoningtokens",
    }
)
_IMAGE_REFERENCE_KEYS = frozenset({"imageurl", "imageurls"})


def sanitize_chat_messages(messages: object) -> object:
    """按 Chat 消息语义复制并脱敏，不破坏普通正文的可读性。

    Args:
        messages: 最终 Chat 请求中的消息序列。

    Returns:
        脱敏后的消息序列；普通用户和助手正文保持原样。
    """

    if not isinstance(messages, list):
        return sanitize_prompt_value(messages, redact_credentials=True)
    return [_sanitize_chat_message(message) for message in messages]


def sanitize_prompt_value(
    value: object,
    *,
    redact_credentials: bool,
    preserve_mapping_keys: bool = False,
    preserve_schema_property_names: bool = False,
    _schema_context: bool = False,
) -> object:
    """复制请求值，并折叠二进制数据或屏蔽请求凭据。

    Args:
        value: 最终请求中的任意 JSON 友好值。
        redact_credentials: 是否根据字段名屏蔽凭据。
        preserve_mapping_keys: 当前映射的键是否属于 JSON Schema 属性名。
        preserve_schema_property_names: 是否只在工具参数 Schema 下保留属性名。
        _schema_context: 当前值是否已经位于工具参数 Schema 内部。

    Returns:
        不修改原值的审查安全副本。
    """

    if isinstance(value, Mapping):
        sanitized: dict[str, object] = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            if _is_image_reference_key(key):
                sanitized[key] = _sanitize_image_reference(
                    item,
                    redact_credentials=redact_credentials,
                    preserve_schema_property_names=preserve_schema_property_names,
                    schema_context=_schema_context,
                )
                continue
            if redact_credentials and not preserve_mapping_keys and _is_sensitive_key(key):
                sanitized[key] = "[已屏蔽凭据]"
                continue
            sanitized[key] = sanitize_prompt_value(
                item,
                redact_credentials=redact_credentials,
                preserve_mapping_keys=(
                    _schema_context and key == "properties" and isinstance(item, Mapping)
                ),
                preserve_schema_property_names=preserve_schema_property_names,
                _schema_context=(
                    _schema_context
                    or preserve_schema_property_names
                    and key == "parameters"
                    and isinstance(item, Mapping)
                ),
            )
        return sanitized
    if isinstance(value, (list, tuple)):
        return [
            sanitize_prompt_value(
                item,
                redact_credentials=redact_credentials,
                preserve_schema_property_names=preserve_schema_property_names,
                _schema_context=_schema_context,
            )
            for item in value
        ]
    if isinstance(value, bytes):
        return _binary_summary(value, media_type="application/octet-stream")
    if isinstance(value, str):
        return _fold_data_url(value)
    return value


def _sanitize_chat_message(message: object) -> object:
    """处理一条消息中的正文、工具调用和附加结构化字段。

    Args:
        message: 一条 Chat 消息。

    Returns:
        不修改原消息的脱敏副本。
    """

    if not isinstance(message, Mapping):
        return sanitize_prompt_value(message, redact_credentials=True)

    role = str(message.get("role", "")).lower()
    sanitized: dict[str, object] = {}
    for raw_key, item in message.items():
        key = str(raw_key)
        if key == "content":
            if role in {"tool", "function"}:
                sanitized[key] = _sanitize_tool_result_content(item)
            else:
                sanitized[key] = _sanitize_chat_content(item)
        elif key == "tool_calls":
            sanitized[key] = _sanitize_tool_calls(item)
        else:
            sanitized[key] = sanitize_prompt_value(item, redact_credentials=True)
    return sanitized


def _sanitize_chat_content(content: object) -> object:
    """保留自然语言正文，只对多模态对象做结构化处理。

    Args:
        content: 消息的正文字符串、内容块或其它兼容值。

    Returns:
        普通正文原文或脱敏后的内容块副本。
    """

    if isinstance(content, list):
        return [_sanitize_content_block(block) for block in content]
    if isinstance(content, Mapping):
        return _sanitize_content_block(content)
    if isinstance(content, str):
        return content
    return sanitize_prompt_value(content, redact_credentials=True)


def _sanitize_content_block(block: object) -> object:
    """复制单个内容块，并避免把文本块误当成图片或凭据字段。

    Args:
        block: 多模态消息中的单个内容块。

    Returns:
        文本保持原样、图片引用已折叠的内容块副本。
    """

    if not isinstance(block, Mapping):
        return sanitize_prompt_value(block, redact_credentials=True)

    sanitized: dict[str, object] = {}
    for raw_key, item in block.items():
        key = str(raw_key)
        if key == "text":
            sanitized[key] = item
        elif _is_image_reference_key(key):
            sanitized[key] = _sanitize_image_reference(
                item,
                redact_credentials=True,
                preserve_schema_property_names=False,
                schema_context=False,
            )
        else:
            sanitized[key] = sanitize_prompt_value(
                item,
                redact_credentials=True,
            )
    return sanitized


def _sanitize_tool_calls(tool_calls: object) -> object:
    """递归处理工具调用，并特别解析 function.arguments JSON。

    Args:
        tool_calls: assistant 消息携带的工具调用数组。

    Returns:
        工具参数已递归脱敏的调用数组副本。
    """

    if not isinstance(tool_calls, list):
        return sanitize_prompt_value(tool_calls, redact_credentials=True)

    sanitized_calls: list[object] = []
    for tool_call in tool_calls:
        if not isinstance(tool_call, Mapping):
            sanitized_calls.append(
                sanitize_prompt_value(tool_call, redact_credentials=True)
            )
            continue
        sanitized_call: dict[str, object] = {}
        for raw_key, item in tool_call.items():
            key = str(raw_key)
            if key != "function" or not isinstance(item, Mapping):
                sanitized_call[key] = sanitize_prompt_value(
                    item,
                    redact_credentials=True,
                )
                continue
            sanitized_function: dict[str, object] = {}
            for raw_function_key, function_item in item.items():
                function_key = str(raw_function_key)
                if function_key == "arguments" and isinstance(function_item, str):
                    sanitized_function[function_key] = _sanitize_tool_arguments(
                        function_item
                    )
                else:
                    sanitized_function[function_key] = sanitize_prompt_value(
                        function_item,
                        redact_credentials=True,
                    )
            sanitized_call[key] = sanitized_function
        sanitized_calls.append(sanitized_call)
    return sanitized_calls


def _sanitize_tool_arguments(arguments: str) -> str:
    """解析工具参数 JSON；解析失败时只留下不可逆摘要。

    Args:
        arguments: SDK 消息中的工具参数 JSON 字符串。

    Returns:
        重新序列化的脱敏 JSON，或字符数和 SHA-256 摘要。
    """

    try:
        parsed_arguments = json.loads(arguments)
    except (TypeError, ValueError, json.JSONDecodeError):
        return _text_summary(arguments, label="工具参数未解析")
    sanitized_arguments = sanitize_prompt_value(
        parsed_arguments,
        redact_credentials=True,
    )
    return json.dumps(
        sanitized_arguments,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _sanitize_tool_result_content(content: object) -> object:
    """对 JSON 形式的工具结果递归脱敏，普通工具文本保持原样。

    Args:
        content: tool 或 function 消息的结果正文。

    Returns:
        结构化 JSON 的脱敏字符串，或原始自然语言结果。
    """

    if not isinstance(content, str):
        return sanitize_prompt_value(content, redact_credentials=True)
    try:
        parsed_content = json.loads(content)
    except (TypeError, ValueError, json.JSONDecodeError):
        return content
    if not isinstance(parsed_content, (Mapping, list)):
        return content
    return json.dumps(
        sanitize_prompt_value(parsed_content, redact_credentials=True),
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _sanitize_image_reference(
    value: object,
    *,
    redact_credentials: bool,
    preserve_schema_property_names: bool,
    schema_context: bool,
) -> object:
    """只折叠图片 URL，不下载远程内容。

    Args:
        value: 图片字符串、图片对象或图片数组。
        redact_credentials: 是否屏蔽其它结构化凭据字段。
        preserve_schema_property_names: 是否保留工具 Schema 属性名。
        schema_context: 当前值是否位于工具参数 Schema 内。

    Returns:
        图片引用已折叠的安全副本。
    """

    if isinstance(value, str):
        return _fold_image_reference(value)
    if isinstance(value, list):
        return [
            _sanitize_image_reference(
                item,
                redact_credentials=redact_credentials,
                preserve_schema_property_names=preserve_schema_property_names,
                schema_context=schema_context,
            )
            for item in value
        ]
    if not isinstance(value, Mapping):
        return sanitize_prompt_value(
            value,
            redact_credentials=redact_credentials,
            preserve_schema_property_names=preserve_schema_property_names,
            _schema_context=schema_context,
        )

    if "url" not in value:
        return sanitize_prompt_value(
            value,
            redact_credentials=redact_credentials,
            preserve_schema_property_names=preserve_schema_property_names,
            _schema_context=schema_context,
        )
    sanitized: dict[str, object] = {}
    for raw_key, item in value.items():
        key = str(raw_key)
        if key == "url" and isinstance(item, str):
            sanitized[key] = _fold_image_reference(item)
        else:
            sanitized[key] = sanitize_prompt_value(
                item,
                redact_credentials=redact_credentials,
                preserve_schema_property_names=preserve_schema_property_names,
                _schema_context=schema_context,
            )
    return sanitized


def _is_image_reference_key(key: str) -> bool:
    """判断字段是否承载单张或多张图片引用。

    Args:
        key: 结构化对象中的字段名。

    Returns:
        字段名归一后为 image_url 或 image_urls 时返回真。
    """

    return re.sub(r"[^a-z0-9]", "", key.lower()) in _IMAGE_REFERENCE_KEYS


def _fold_image_reference(value: str) -> str:
    """折叠内联或远程图片引用，永远不把远程查询参数写入文件。

    Args:
        value: 图片 data URL 或远程 URL。

    Returns:
        图片摘要；无法识别为图片引用时返回原字符串。
    """

    if value.lower().startswith("data:"):
        return _fold_data_url(value)
    try:
        parsed_url = urlsplit(value)
    except ValueError:
        parsed_url = None
    if parsed_url is None or parsed_url.scheme.lower() not in {"http", "https"}:
        return value
    media_type = mimetypes.guess_type(parsed_url.path)[0] or "unknown"
    return (
        f"[远程图片已折叠：media_type={media_type}, bytes=unknown, "
        f"url_sha256={hashlib.sha256(value.encode('utf-8')).hexdigest()}]"
    )


def _text_summary(value: str, *, label: str) -> str:
    """返回不包含原文的文本长度与 SHA-256 摘要。

    Args:
        value: 不能安全保留的原始字符串。
        label: 摘要占位文本的用途名称。

    Returns:
        只含字符数与 SHA-256 的稳定文本。
    """

    encoded_value = value.encode("utf-8")
    return (
        f"[{label}：chars={len(value)}, "
        f"sha256={hashlib.sha256(encoded_value).hexdigest()}]"
    )


def _fold_data_url(value: str) -> str:
    """把 Base64 data URL 换成人类可核对的摘要。

    Args:
        value: 请求中的字符串值。

    Returns:
        普通字符串原样返回；Base64 data URL 返回类型、字节数和哈希摘要。
    """

    header, separator, encoded_payload = value.partition(",")
    if (
        not separator
        or not header.lower().startswith("data:")
        or ";base64" not in header.lower()
    ):
        return value

    media_type = header[5:].split(";", 1)[0].strip() or "application/octet-stream"
    try:
        decoded_payload = base64.b64decode(encoded_payload, validate=True)
    except (binascii.Error, ValueError):
        encoded_bytes = encoded_payload.encode("utf-8")
        return (
            f"[内联数据已折叠：media_type={media_type}, "
            f"base64_chars={len(encoded_payload)}, "
            f"sha256_of_encoded={hashlib.sha256(encoded_bytes).hexdigest()}]"
        )
    return _binary_summary(decoded_payload, media_type=media_type)


def _binary_summary(value: bytes, *, media_type: str) -> str:
    """返回二进制内容的稳定摘要。

    Args:
        value: 原始二进制内容。
        media_type: 内容媒体类型。

    Returns:
        包含媒体类型、字节数和 SHA-256 的占位文本。
    """

    return (
        f"[内联数据已折叠：media_type={media_type}, bytes={len(value)}, "
        f"sha256={hashlib.sha256(value).hexdigest()}]"
    )


def _is_sensitive_key(key: str) -> bool:
    """判断请求参数名是否代表认证凭据。

    Args:
        key: 请求参数字段名。

    Returns:
        命中常见认证字段时返回真。
    """

    normalized_key = re.sub(r"[^a-z0-9]", "", key.lower())
    if normalized_key in _NON_SECRET_TOKEN_KEYS:
        return False
    if normalized_key in _SENSITIVE_KEY_NAMES:
        return True
    if normalized_key.endswith("token"):
        return True
    return any(
        normalized_key.endswith(part)
        for part in ("apikey", "clientsecret")
    ) or any(
        part in normalized_key
        for part in ("authorization", "cookie", "password", "secret")
    )
