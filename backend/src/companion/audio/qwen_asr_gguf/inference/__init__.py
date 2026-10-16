# coding=utf-8
"""Qwen ASR 推理包轻量入口。"""

from companion.observability.logging_config import get_backend_logger

# 本地推理模块沿用 `from . import logger` 的写法，因此在入口集中提供包级 logger。
logger = get_backend_logger(__name__)
