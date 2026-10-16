from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from companion.observability.logging_config import (
    BACKEND_LOGGER_NAME,
    configure_backend_logging,
    shutdown_backend_logging,
)
from companion.observability.structured_logging import log_event


def test_backend_logging_writes_process_scoped_jsonl_without_duplicate_handlers(
    tmp_path: Path,
) -> None:
    """重复配置应复用 handler，并写出可准确计数的进程级 JSONL。"""

    backend_logger = logging.getLogger(BACKEND_LOGGER_NAME)
    original_handlers = list(backend_logger.handlers)
    original_level = backend_logger.level
    original_propagate = backend_logger.propagate
    try:
        backend_logger.handlers.clear()

        configure_backend_logging(
            process_role="assistant-runtime",
            log_dir=tmp_path,
            max_bytes=1024,
            backup_count=2,
        )
        log_event(
            logging.getLogger("companion.test"),
            logging.INFO,
            "runtime_configured",
        )
        configure_backend_logging(
            process_role="assistant-runtime",
            level=logging.DEBUG,
            log_dir=tmp_path,
            max_bytes=1024,
            backup_count=2,
        )

        managed_handlers = [
            handler
            for handler in backend_logger.handlers
            if getattr(handler, "_companion_backend_handler", False)
        ]
        assert len(managed_handlers) == 2
        assert all(handler.level == logging.DEBUG for handler in managed_handlers)

        log_event(
            logging.getLogger("companion.test"),
            logging.INFO,
            "llm_call_started",
            request_id="request-a",
            generation_id="generation-a",
            llm_call_id="generation-a:initial",
            round="initial",
        )
        for handler in backend_logger.handlers:
            handler.flush()

        payload = json.loads(
            (tmp_path / "assistant-runtime.jsonl").read_text(encoding="utf-8").splitlines()[-1]
        )
        assert payload["event"] == "llm_call_started"
        assert payload["process_role"] == "assistant-runtime"
        assert payload["sequence"] == 2
        assert payload["request_id"] == "request-a"
        assert payload["generation_id"] == "generation-a"
        assert payload["llm_call_id"] == "generation-a:initial"
        assert payload["round"] == "initial"
    finally:
        for handler in backend_logger.handlers:
            handler.close()
        backend_logger.handlers[:] = original_handlers
        backend_logger.setLevel(original_level)
        backend_logger.propagate = original_propagate


def test_backend_jsonl_rotates_with_fixed_retention(tmp_path: Path) -> None:
    """权威日志达到容量后必须滚动，不能无限占用磁盘。"""

    backend_logger = logging.getLogger(BACKEND_LOGGER_NAME)
    original_handlers = list(backend_logger.handlers)
    original_level = backend_logger.level
    original_propagate = backend_logger.propagate
    try:
        backend_logger.handlers.clear()
        configure_backend_logging(
            process_role="backend-main",
            log_dir=tmp_path,
            max_bytes=300,
            backup_count=2,
        )

        logger = logging.getLogger("companion.rotation")
        for index in range(20):
            log_event(logger, logging.INFO, "rotation_probe", index=index, text="x" * 80)
        for handler in backend_logger.handlers:
            handler.flush()

        files = sorted(tmp_path.glob("backend-main.jsonl*"))
        assert 1 < len(files) <= 3
    finally:
        for handler in backend_logger.handlers:
            handler.close()
        backend_logger.handlers[:] = original_handlers
        backend_logger.setLevel(original_level)
        backend_logger.propagate = original_propagate


@pytest.mark.parametrize(
    ("changed_field", "changed_value"),
    [
        ("process_role", "assistant-runtime"),
        ("log_dir", "other"),
        ("max_bytes", 2048),
        ("backup_count", 3),
    ],
)
def test_backend_logging_rejects_conflicting_reconfiguration(
    tmp_path: Path,
    changed_field: str,
    changed_value: object,
) -> None:
    """进程日志目标和滚动策略建立后不得被静默替换。"""

    backend_logger = logging.getLogger(BACKEND_LOGGER_NAME)
    original_handlers = list(backend_logger.handlers)
    original_level = backend_logger.level
    original_propagate = backend_logger.propagate
    try:
        backend_logger.handlers.clear()
        configure_backend_logging(
            process_role="backend-main",
            log_dir=tmp_path,
            max_bytes=1024,
            backup_count=2,
        )
        arguments: dict[str, object] = {
            "process_role": "backend-main",
            "log_dir": tmp_path,
            "max_bytes": 1024,
            "backup_count": 2,
        }
        arguments[changed_field] = (
            tmp_path / str(changed_value)
            if changed_field == "log_dir"
            else changed_value
        )

        with pytest.raises(ValueError, match="日志配置"):
            configure_backend_logging(**arguments)  # type: ignore[arg-type]

        if changed_field == "log_dir":
            assert not (tmp_path / str(changed_value)).exists()

        json_handler = next(
            handler
            for handler in backend_logger.handlers
            if getattr(handler, "_companion_handler_kind", None) == "jsonl"
        )
        assert Path(json_handler.baseFilename).parent == tmp_path.resolve()  # type: ignore[attr-defined]
        assert json_handler.maxBytes == 1024  # type: ignore[attr-defined]
        assert json_handler.backupCount == 2  # type: ignore[attr-defined]
    finally:
        for handler in backend_logger.handlers:
            handler.flush()
            handler.close()
        backend_logger.handlers.clear()
        backend_logger.handlers[:] = original_handlers
        backend_logger.setLevel(original_level)
        backend_logger.propagate = original_propagate


def test_backend_logging_shutdown_releases_windows_file_handle(tmp_path: Path) -> None:
    """日志关闭后应移除自建 handler，并允许立即删除 Windows 日志文件。"""

    backend_logger = logging.getLogger(BACKEND_LOGGER_NAME)
    original_handlers = list(backend_logger.handlers)
    original_level = backend_logger.level
    original_propagate = backend_logger.propagate
    log_path = tmp_path / "backend-main.jsonl"
    try:
        backend_logger.handlers.clear()
        configure_backend_logging(
            process_role="backend-main",
            log_dir=tmp_path,
        )
        log_event(logging.getLogger("companion.shutdown"), logging.INFO, "shutdown_probe")

        shutdown_backend_logging()

        assert not any(
            getattr(handler, "_companion_backend_handler", False)
            for handler in backend_logger.handlers
        )
        log_path.unlink()
        assert log_path.exists() is False
    finally:
        shutdown_backend_logging()
        backend_logger.handlers[:] = original_handlers
        backend_logger.setLevel(original_level)
        backend_logger.propagate = original_propagate
