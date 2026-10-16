from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Literal
from companion.core.runtime_paths import DATA_ROOT

from companion.observability.structured_logging import (
    JsonSequenceFilter,
    ProcessRoleFilter,
    StructuredJsonFormatter,
)


BACKEND_LOGGER_NAME = "companion"
BACKEND_LOG_FORMAT = (
    "%(asctime)s %(levelname)s [%(process_role)s] [%(name)s] [%(threadName)s] %(message)s"
)
BACKEND_LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
BackendProcessRole = Literal["backend-main", "assistant-runtime"]
DEFAULT_LOG_DIR = DATA_ROOT / "logs"
DEFAULT_LOG_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_LOG_BACKUP_COUNT = 5


def get_backend_logger(module_name: str) -> logging.Logger:
    """获取后端统一日志入口下的模块 logger。"""

    # 模块名本身就是最好的来源标识；统一入口的价值是让业务代码不直接依赖 logging.getLogger。
    return logging.getLogger(module_name)


def configure_backend_logging(
    *,
    process_role: BackendProcessRole,
    level: int = logging.INFO,
    log_dir: Path | None = None,
    max_bytes: int = DEFAULT_LOG_MAX_BYTES,
    backup_count: int = DEFAULT_LOG_BACKUP_COUNT,
) -> None:
    """配置终端与进程独立 JSONL 双通道日志。

    Args:
        process_role: 当前调用方是后端主进程还是助手运行时。
        level: 两个日志通道共同使用的最低级别。
        log_dir: JSONL 日志目录；为空时写入仓库 backend/data/logs。
        max_bytes: 单个 JSONL 文件触发滚动的字节数。
        backup_count: 每个进程日志保留的历史文件数量。
    """

    if max_bytes <= 0:
        raise ValueError("max_bytes 必须大于零")
    if backup_count <= 0:
        raise ValueError("backup_count 必须大于零")

    resolved_log_dir = (log_dir or DEFAULT_LOG_DIR).resolve()
    json_path = resolved_log_dir / f"{process_role}.jsonl"

    # 只配置本项目业务 logger，uvicorn 的访问日志和错误日志继续由 uvicorn 自己管理。
    backend_logger = logging.getLogger(BACKEND_LOGGER_NAME)
    json_handler = _ensure_backend_json_handler(
        backend_logger,
        path=json_path,
        process_role=process_role,
        max_bytes=max_bytes,
        backup_count=backup_count,
    )
    backend_logger.setLevel(level)
    backend_logger.propagate = False

    formatter = logging.Formatter(BACKEND_LOG_FORMAT, datefmt=BACKEND_LOG_DATE_FORMAT)
    stream_handler = _ensure_backend_stream_handler(backend_logger)
    stream_handler.setLevel(level)
    stream_handler.setFormatter(formatter)
    _ensure_runtime_filter(stream_handler, ProcessRoleFilter(process_role))

    json_handler.setLevel(level)
    json_handler.setFormatter(StructuredJsonFormatter())
    _ensure_runtime_filter(json_handler, JsonSequenceFilter(process_role))


def shutdown_backend_logging() -> None:
    """刷新、关闭并移除当前进程创建的全部业务日志 handler。"""

    backend_logger = logging.getLogger(BACKEND_LOGGER_NAME)
    managed_handlers = [
        handler
        for handler in backend_logger.handlers
        if getattr(handler, "_companion_backend_handler", False)
    ]
    for handler in managed_handlers:
        backend_logger.removeHandler(handler)
        try:
            handler.flush()
        except Exception:
            # 关闭路径不能因单个输出端故障阻塞其它 handler 释放文件句柄。
            pass
        try:
            handler.close()
        except Exception:
            pass


def _ensure_backend_stream_handler(logger: logging.Logger) -> logging.StreamHandler:
    """确保后端业务 logger 只有一个由本模块管理的 stdout handler。"""

    # 用私有标记识别自己创建的 handler，重复配置时复用它而不是不断追加重复输出。
    for handler in logger.handlers:
        handler_kind = getattr(handler, "_companion_handler_kind", None)
        if handler_kind == "stream" or (
            handler_kind is None
            and getattr(handler, "_companion_backend_handler", False)
            and not isinstance(handler, RotatingFileHandler)
        ):
            setattr(handler, "_companion_handler_kind", "stream")
            return handler

    handler = logging.StreamHandler(sys.stdout)
    setattr(handler, "_companion_backend_handler", True)
    setattr(handler, "_companion_handler_kind", "stream")
    logger.addHandler(handler)
    return handler


def _ensure_backend_json_handler(
    logger: logging.Logger,
    *,
    path: Path,
    process_role: BackendProcessRole,
    max_bytes: int,
    backup_count: int,
) -> RotatingFileHandler:
    """确保当前进程只有一个权威 JSONL handler。

    Args:
        logger: companion 命名空间根 logger。
        path: 当前进程独占的 JSONL 文件。
        process_role: 当前 handler 绑定的不可变进程角色。
        max_bytes: 单文件滚动阈值。
        backup_count: 历史文件保留数量。

    Returns:
        已存在或新建的滚动文件 handler。
    """

    for handler in logger.handlers:
        if getattr(handler, "_companion_handler_kind", None) == "jsonl":
            configured_path = Path(handler.baseFilename).resolve()
            configured_role = getattr(handler, "_companion_process_role", None)
            if (
                configured_path != path
                or configured_role != process_role
                or handler.maxBytes != max_bytes
                or handler.backupCount != backup_count
            ):
                raise ValueError(
                    "日志配置与当前进程已经打开的 JSONL handler 冲突；请重启进程后再变更"
                )
            return handler

    path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        path,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    setattr(handler, "_companion_backend_handler", True)
    setattr(handler, "_companion_handler_kind", "jsonl")
    setattr(handler, "_companion_process_role", process_role)
    logger.addHandler(handler)
    return handler


def _ensure_runtime_filter(handler: logging.Handler, log_filter: ProcessRoleFilter) -> None:
    """复用同角色过滤器，避免重复配置重置 JSONL 单调序号。

    Args:
        handler: 需要更新进程角色或序号生成器的日志输出端。
        log_filter: 本次配置期望使用的进程角色过滤器。
    """

    for current_filter in handler.filters:
        if (
            type(current_filter) is type(log_filter)
            and isinstance(current_filter, ProcessRoleFilter)
            and current_filter.process_role == log_filter.process_role
        ):
            return

    handler.filters[:] = [
        current_filter
        for current_filter in handler.filters
        if not isinstance(current_filter, ProcessRoleFilter)
    ]
    handler.addFilter(log_filter)
