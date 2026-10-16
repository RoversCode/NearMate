#!/usr/bin/env python
# -*- encoding: utf-8 -*-
'''
@File    :   format.py
@Time    :   2026/06/12 14:20:57
@Author  :   RoversCode
@Version :   1.0
@Contact :   liujunjie199810@gmail.com
@Desc    :   None
'''

# here put the import lib
import re


def format_text(text: str) -> str:
    """清理分句后文本中的冗余空白并保留语义符号。

    Args:
        text: 已完成流式切分的文本。

    Returns:
        空白规范且未改写连字符等语义符号的文本。
    """

    if not text:
        return text

    text = re.sub(r" +", " ", text)
    text = re.sub(r"(?<=[\u4e00-\u9fa5])\s+(?=[\u4e00-\u9fa5])", "", text)
    return text.strip()
