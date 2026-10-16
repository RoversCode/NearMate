from __future__ import annotations


_FAILURE_REASON_TEXT = {
    "unsupported_extension": "文件格式不支持",
    "missing_path": "文件路径不存在",
    "metadata_unreadable": "无法读取文件元数据",
    "permission_denied": "没有文件读取权限",
    "invalid_text_encoding": "文本不是有效的 UTF-8 编码",
    "io_error": "文件读写发生错误",
    "symlink_not_allowed": "不允许使用软链接",
    "not_regular_file": "路径不是普通文件",
    "empty_file": "文件内容为空",
    "missing_or_symlink": "文件已不存在或是软链接",
    "invalid_web_selection": "网页选区数据无效",
    "image_too_large": "图片像素数量超限",
    "invalid_image": "图片内容无效",
    "invalid_or_encrypted_pdf": "PDF 损坏或已加密",
    "invalid_or_encrypted_docx": "DOCX 损坏或已加密",
    "empty_text": "文本没有有效内容",
    "empty_folder": "文件夹没有可处理文件",
    "unreadable_file": "文件无法读取",
}


def describe_material_failure(reason: str) -> str:
    """把稳定材料失败原因转换为日志和摘要共用的中文说明。

    Args:
        reason: Backend 内部稳定失败原因码。

    Returns:
        用户和开发者均可理解的简短中文说明。
    """

    return _FAILURE_REASON_TEXT.get(reason, "文件无法读取")
