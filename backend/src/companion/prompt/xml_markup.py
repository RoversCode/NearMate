from __future__ import annotations

from html import escape

INDENT_UNIT = "  "


def render_xml_block(tag_name: str, children: list[str]) -> str:
    """渲染包含子节点的 XML 块。

    Args:
        tag_name: 外层标签名。
        children: 已渲染完成的子节点文本。

    Returns:
        包含所有非空子节点的 XML 块；没有子节点时返回空标签块。
    """

    body = "\n".join(_indent_lines(child.strip(), level=1) for child in children if child.strip())
    if not body:
        return f"<{tag_name}></{tag_name}>"
    return f"<{tag_name}>\n{body}\n</{tag_name}>"


def render_xml_cdata_leaf(tag_name: str, content: str) -> str:
    """渲染 CDATA 叶子节点，保护提示词里的字面协议标记。

    Args:
        tag_name: 叶子节点标签名。
        content: 需要原样保留的提示词正文。

    Returns:
        使用 CDATA 包裹正文的 XML 叶子节点。
    """

    return f"<{tag_name}>\n{_indent_lines(_cdata(content.strip()), level=1)}\n</{tag_name}>"


def render_xml_text_leaf(tag_name: str, content: str) -> str:
    """渲染普通文本叶子节点。

    Args:
        tag_name: 叶子节点标签名。
        content: 可作为 XML 文本节点展示的短文本。

    Returns:
        已完成 XML 转义的单行叶子节点。
    """

    return f"<{tag_name}>{escape(content.strip())}</{tag_name}>"


def render_xml_text_block(tag_name: str, content: str) -> str:
    """渲染支持多行缩进的普通文本节点。

    Args:
        tag_name: 叶子节点标签名。
        content: 需要作为 XML 文本节点展示的正文。

    Returns:
        单行正文返回单行叶子节点；多行正文返回带层级缩进的文本节点。
    """

    escaped_content = escape(content)
    if "\n" not in escaped_content and "\r" not in escaped_content:
        return f"<{tag_name}>{escaped_content}</{tag_name}>"
    return f"<{tag_name}>\n{_indent_lines(escaped_content, level=1)}\n</{tag_name}>"


def _indent_lines(content: str, *, level: int) -> str:
    """按 XML 层级缩进多行文本。

    Args:
        content: 已渲染的 XML 片段或文本。
        level: 需要增加的缩进层数。

    Returns:
        每一行都带指定缩进的文本。
    """

    prefix = INDENT_UNIT * level
    return "\n".join(f"{prefix}{line}" if line else prefix.rstrip() for line in content.splitlines())


def _cdata(content: str) -> str:
    """把正文转换为安全 CDATA。

    Args:
        content: 原始正文。

    Returns:
        可安全放入 XML 的 CDATA 字符串。
    """

    return "<![CDATA[" + content.replace("]]>", "]]]]><![CDATA[>") + "]]>"
