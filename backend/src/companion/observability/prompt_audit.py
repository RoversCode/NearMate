"""管理 LLM Prompt 审查配置、运行目录和时序索引。"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import time
from collections.abc import Awaitable, Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, BinaryIO
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from companion.observability.logging_config import get_backend_logger
from companion.observability.prompt_audit_html import (
    PromptAuditIndexEntry,
    render_prompt_audit_process_index,
    render_prompt_audit_run_index,
)
from companion.observability.prompt_audit_markdown import (
    render_prompt_audit_model_result,
    render_prompt_audit_markdown,
    replace_prompt_audit_model_result,
)
from companion.observability.structured_logging import log_event
from companion.core.runtime_paths import DATA_ROOT, DEVELOPMENT_ROOT


logger = get_backend_logger(__name__)
DEFAULT_PROMPT_AUDIT_OUTPUT_DIR = DATA_ROOT / "prompt-audits"
_SAFE_FILENAME_PART = re.compile(r"[^A-Za-z0-9._-]+")
_RUN_PROCESS_ID = re.compile(r"__pid-(\d+)__")
_PROCESS_LOCK_TIMEOUT_S = 0.1
_PROCESS_LOCK_RETRY_INTERVAL_S = 0.01


class PromptAuditSettings(BaseModel):
    """保存本地 Prompt 审查文件的启动配置。

    Args:
        enabled: 是否记录业务 LLM 请求；代码默认关闭，避免意外保存用户材料。
        output_dir: Prompt Audit 根目录；相对路径按仓库根目录解析。
        max_runs: 每个进程角色最多保留的完整运行次数。
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    output_dir: str = str(DEFAULT_PROMPT_AUDIT_OUTPUT_DIR)
    max_runs: int = Field(default=20, gt=0)

    @field_validator("output_dir")
    @classmethod
    def validate_output_dir(cls, value: str) -> str:
        """拒绝空白目录，避免写入不可预测的当前工作目录。

        Args:
            value: 配置文件传入的目录文本。

        Returns:
            去除首尾空白后的目录文本。

        Raises:
            ValueError: 输出目录为空时抛出。
        """

        normalized_value = value.strip()
        if not normalized_value:
            raise ValueError("prompt_audit.output_dir 不能为空")
        return normalized_value

    def resolved_output_dir(self) -> Path:
        """返回不受进程工作目录影响的绝对输出目录。

        Returns:
            绝对 Prompt Audit 根目录。
        """

        configured_path = Path(self.output_dir).expanduser()
        if configured_path.is_absolute():
            return configured_path.resolve()
        root = DATA_ROOT if os.environ.get('NEARMATE_PACKAGED') == '1' else DEVELOPMENT_ROOT
        return (root / configured_path).resolve()


class PromptAuditRecorder:
    """把一次最终 LLM 请求写成 Markdown，并维护运行时序索引。

    Args:
        enabled: 是否实际生成审查文件。
        output_dir: 全部进程角色共享的审查根目录。
        max_runs: 当前进程角色最多保留的完整运行次数。
        process_role: 当前记录来自 Backend 主进程还是助手子进程。
        now: 可选 UTC 时间来源，主要用于测试固定运行和调用时间。
        id_factory: 可选唯一编号来源，主要用于测试。
        process_id: 可选进程编号，主要用于测试固定运行目录名。
        process_is_alive: 可选进程存活判断，主要用于测试运行保留策略。
    """

    def __init__(
        self,
        *,
        enabled: bool,
        output_dir: Path | str,
        max_runs: int,
        process_role: str,
        now: Callable[[], datetime] | None = None,
        id_factory: Callable[[], str] | None = None,
        process_id: int | None = None,
        process_is_alive: Callable[[int], bool] | None = None,
    ) -> None:
        """保存不可变配置；关闭态和无调用运行都不访问文件系统。

        Args:
            enabled: 是否实际生成审查文件。
            output_dir: 全部进程角色共享的审查根目录。
            max_runs: 当前进程角色最多保留的完整运行次数。
            process_role: 当前记录来自 Backend 主进程还是助手子进程。
            now: 可选 UTC 时间来源。
            id_factory: 可选唯一编号来源。
            process_id: 可选进程编号。
            process_is_alive: 可选进程存活判断。

        Raises:
            ValueError: 运行保留数或进程角色无效时抛出。
        """

        if max_runs <= 0:
            raise ValueError("max_runs 必须大于零")
        normalized_process_role = process_role.strip()
        if not normalized_process_role:
            raise ValueError("process_role 不能为空")

        self.enabled = enabled
        self.output_dir = Path(output_dir)
        self.max_runs = max_runs
        self.process_role = normalized_process_role
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._id_factory = id_factory or (lambda: f"llm-{uuid4().hex}")
        self._process_id = process_id if process_id is not None else os.getpid()
        self._process_is_alive = process_is_alive or _process_is_alive
        self._lock = Lock()
        self._sequence = 0
        self._entries: list[PromptAuditIndexEntry] = []
        self._initialized = False

        started_at = self._normalized_utc_now()
        run_suffix = _safe_filename_part(self._id_factory())[-8:]
        self.run_id = (
            f"{started_at.strftime('%Y%m%dT%H%M%S.%fZ')}"
            f"__pid-{self._process_id}__{run_suffix}"
        )
        process_directory = self.output_dir / _safe_filename_part(self.process_role)
        self._runs_directory = process_directory / "runs"
        self._run_directory = self._runs_directory / self.run_id
        self._process_index_path = process_directory / "index.html"
        self._run_index_path = self._run_directory / "index.html"

    def record_request(
        self,
        *,
        role: str,
        source: str,
        protocol: str,
        model: str,
        operation: str,
        request: Mapping[str, Any],
        trace_fields: Mapping[str, object] | None = None,
    ) -> Path | None:
        """记录一次即将发给 SDK 的最终请求。

        审查能力是诊断旁路。目录、脱敏、渲染、写入或清理失败只记技术日志，
        绝不能让真实 LLM 请求失败。

        Args:
            role: 当前 LLM 职责或内部用途。
            source: 触发本次调用的业务来源。
            protocol: 当前使用的 SDK 协议。
            model: 本次请求使用的真实模型名。
            operation: initial、tool_followup、execute 等调用阶段。
            request: 已合并默认配置和调用覆盖的最终 SDK 请求参数。
            trace_fields: 可选请求编号、Activity 编号和 LLM 调用编号。

        Returns:
            成功写入时返回 Markdown 路径；关闭或写入失败时返回空。
        """

        if not self.enabled:
            return None

        normalized_trace = dict(trace_fields or {})
        llm_call_id = _clean_metadata_text(normalized_trace.get("llm_call_id"))
        try:
            if not llm_call_id:
                llm_call_id = self._id_factory()
                normalized_trace["llm_call_id"] = llm_call_id

            with self._lock:
                with _prompt_audit_process_lock(self._process_index_path.parent):
                    final_path, sequence = self._record_request_locked(
                        role=role,
                        source=source,
                        protocol=protocol,
                        model=model,
                        operation=operation,
                        request=request,
                        trace_fields=normalized_trace,
                        llm_call_id=llm_call_id,
                    )

            log_event(
                logger,
                logging.INFO,
                "llm_prompt_audited",
                audit_path=str(final_path),
                run_id=self.run_id,
                sequence=sequence,
                llm_call_id=llm_call_id,
                role=role,
                source=source,
                operation=operation,
            )
            return final_path
        except Exception:
            # Prompt 正文不得进入普通日志；这里只保留稳定调用身份和异常堆栈。
            logger.warning(
                "LLM Prompt 审查文件写入失败 role=%s source=%s operation=%s "
                "llm_call_id=%s",
                role,
                source,
                operation,
                llm_call_id or "unknown",
                exc_info=True,
            )
            return None

    def record_request_and_call(
        self,
        *,
        role: str,
        source: str,
        protocol: str,
        model: str,
        operation: str,
        request: Mapping[str, Any],
        trace_fields: Mapping[str, object] | None,
        sdk_call: Callable[[], Any],
        audit_messages_factory: Callable[[], list[dict[str, Any]]] | None = None,
    ) -> tuple[Any, Path | None]:
        """在审计临界区内写入请求并立即执行一次 SDK 调用。

        Args:
            role: 当前 LLM 职责或内部用途。
            source: 触发本次调用的业务来源。
            protocol: 当前使用的 SDK 协议。
            model: 本次请求使用的真实模型名。
            operation: 当前调用阶段。
            request: 已编译完成的最终 SDK 请求参数。
            trace_fields: 可选请求、Activity 和调用身份。
            sdk_call: 真正打开 SDK 请求的无参回调。
            audit_messages_factory: 需要替换审计消息时的惰性构造函数。

        Returns:
            SDK 回调返回的对象和同一调用的 Markdown 路径；审计未落盘时路径为空。

        Raises:
            BaseException: SDK 回调本身抛出的异常会在释放审计锁后原样抛出。
        """

        # 关闭态必须直接走真实调用，连进程锁和请求副本都不能触碰。
        if not self.enabled:
            return sdk_call(), None

        normalized_trace = dict(trace_fields or {})
        llm_call_id = _clean_metadata_text(normalized_trace.get("llm_call_id"))
        try:
            if not llm_call_id:
                llm_call_id = self._id_factory()
                normalized_trace["llm_call_id"] = llm_call_id
        except Exception:
            self._log_record_failure(
                role=role,
                source=source,
                operation=operation,
                llm_call_id=llm_call_id,
            )
            return sdk_call(), None

        sdk_started = False
        sdk_result: Any = None
        sdk_error: BaseException | None = None
        final_path: Path | None = None
        sequence = 0
        try:
            with self._lock:
                try:
                    with _prompt_audit_process_lock(self._process_index_path.parent):
                        try:
                            final_path, sequence = self._record_request_locked(
                                role=role,
                                source=source,
                                protocol=protocol,
                                model=model,
                                operation=operation,
                                request=request,
                                trace_fields=normalized_trace,
                                llm_call_id=llm_call_id,
                                audit_messages_factory=audit_messages_factory,
                            )
                        except Exception:
                            self._log_record_failure(
                                role=role,
                                source=source,
                                operation=operation,
                                llm_call_id=llm_call_id,
                            )
                        else:
                            # 记录成功后保持两把锁，直到 SDK create 已经进入。
                            sdk_started = True
                            try:
                                sdk_result = sdk_call()
                            except BaseException as exception:
                                sdk_error = exception
                except Exception:
                    if not sdk_started:
                        self._log_record_failure(
                            role=role,
                            source=source,
                            operation=operation,
                            llm_call_id=llm_call_id,
                        )
        except Exception:
            if not sdk_started:
                self._log_record_failure(
                    role=role,
                    source=source,
                    operation=operation,
                    llm_call_id=llm_call_id,
                )

        if sdk_started:
            if final_path is not None:
                try:
                    self._log_record_success(
                        final_path=final_path,
                        sequence=sequence,
                        role=role,
                        source=source,
                        operation=operation,
                        llm_call_id=llm_call_id,
                    )
                except Exception:
                    # 技术日志失败也属于审计旁路，不能覆盖真实 SDK 结果或异常。
                    logger.warning(
                        "LLM Prompt 审查成功日志写入失败 llm_call_id=%s",
                        llm_call_id,
                        exc_info=True,
                    )
            if sdk_error is not None:
                self.record_model_result(
                    audit_path=final_path,
                    content_text="",
                    tool_calls=(),
                    error=sdk_error,
                )
                raise sdk_error.with_traceback(sdk_error.__traceback__)
            return sdk_result, final_path
        return sdk_call(), None

    async def record_request_and_await(
        self,
        *,
        role: str,
        source: str,
        protocol: str,
        model: str,
        operation: str,
        request: Mapping[str, Any],
        trace_fields: Mapping[str, object] | None,
        sdk_call: Callable[[], Awaitable[Any]],
        audit_messages_factory: Callable[[], list[dict[str, Any]]] | None = None,
    ) -> tuple[Any, Path | None]:
        """在审计临界区内写入请求并等待一次异步 SDK 调用。

        Args:
            role: 当前 LLM 职责或内部用途。
            source: 触发本次调用的业务来源。
            protocol: 当前使用的 SDK 协议。
            model: 本次请求使用的真实模型名。
            operation: 当前调用阶段。
            request: 已编译完成的最终 SDK 请求参数。
            trace_fields: 可选请求、Activity 和调用身份。
            sdk_call: 真正把请求写入 SDK transport 的异步无参回调。
            audit_messages_factory: 需要替换审计消息时的惰性构造函数。

        Returns:
            SDK 回调结果和同一调用的 Markdown 路径；审计未落盘时路径为空。

        Raises:
            BaseException: SDK 异常或协程取消会在释放全部锁后原样抛出。
        """

        # 关闭态不能为了审计复制请求、创建目录或等待任何锁。
        if not self.enabled:
            return await sdk_call(), None

        normalized_trace = dict(trace_fields or {})
        llm_call_id = _clean_metadata_text(normalized_trace.get("llm_call_id"))
        try:
            if not llm_call_id:
                llm_call_id = self._id_factory()
                normalized_trace["llm_call_id"] = llm_call_id
        except Exception:
            self._log_record_failure(
                role=role,
                source=source,
                operation=operation,
                llm_call_id=llm_call_id,
            )
            return await sdk_call(), None

        try:
            await _acquire_thread_lock_without_leak(self._lock)
        except Exception:
            self._log_record_failure(
                role=role,
                source=source,
                operation=operation,
                llm_call_id=llm_call_id,
            )
            return await sdk_call(), None

        process_lock = None
        process_lock_entered = False
        sdk_started = False
        sdk_result: Any = None
        sdk_error: BaseException | None = None
        final_path: Path | None = None
        sequence = 0
        try:
            try:
                process_lock = _prompt_audit_process_lock(
                    self._process_index_path.parent
                )
                process_lock.__enter__()
                process_lock_entered = True
            except Exception:
                self._log_record_failure(
                    role=role,
                    source=source,
                    operation=operation,
                    llm_call_id=llm_call_id,
                )

            if process_lock_entered:
                try:
                    final_path, sequence = self._record_request_locked(
                        role=role,
                        source=source,
                        protocol=protocol,
                        model=model,
                        operation=operation,
                        request=request,
                        trace_fields=normalized_trace,
                        llm_call_id=llm_call_id,
                        audit_messages_factory=audit_messages_factory,
                    )
                except Exception:
                    self._log_record_failure(
                        role=role,
                        source=source,
                        operation=operation,
                        llm_call_id=llm_call_id,
                    )
                else:
                    # Markdown 已经成为提交事实后，保持两把锁直到 transport 写入完成。
                    sdk_started = True
                    try:
                        sdk_result = await _await_committed_sdk_call(sdk_call)
                    except BaseException as exception:
                        sdk_error = exception
        finally:
            if process_lock_entered and process_lock is not None:
                try:
                    process_lock.__exit__(None, None, None)
                except Exception:
                    self._log_record_failure(
                        role=role,
                        source=source,
                        operation=operation,
                        llm_call_id=llm_call_id,
                    )
            self._lock.release()

        if not sdk_started:
            return await sdk_call(), None

        if final_path is not None:
            try:
                self._log_record_success(
                    final_path=final_path,
                    sequence=sequence,
                    role=role,
                    source=source,
                    operation=operation,
                    llm_call_id=llm_call_id,
                )
            except Exception:
                # 成功日志同样属于诊断旁路，不能覆盖 transport 的真实结果。
                logger.warning(
                    "LLM Prompt 审查成功日志写入失败 llm_call_id=%s",
                    llm_call_id,
                    exc_info=True,
                )
        if sdk_error is not None:
            raise sdk_error.with_traceback(sdk_error.__traceback__)
        return sdk_result, final_path

    def record_model_result(
        self,
        *,
        audit_path: Path | None,
        content_text: str,
        tool_calls: object,
        error: BaseException | None = None,
    ) -> None:
        """把模型结果补写到本次请求已有的 Markdown 文件中。

        Args:
            audit_path: `record_request_and_call` 返回的同一调用文件路径。
            content_text: 模型产生的普通正文；流错误时可以是不完整正文。
            tool_calls: 模型产生的原始结构化工具调用。
            error: SDK 打开、流消费或业务门禁错误；正常完成时为空。

        Returns:
            无。审计旁路失败只写技术日志，不影响业务调用。
        """

        if not self.enabled or audit_path is None:
            return
        error_payload = None
        if error is not None:
            error_payload = {
                "type": type(error).__name__,
                "message": str(error),
            }
        try:
            result_markdown = render_prompt_audit_model_result(
                content_text=content_text,
                tool_calls=tool_calls,
                error=error_payload,
            )
            with self._lock:
                current_markdown = audit_path.read_text(encoding="utf-8")
                updated_markdown = replace_prompt_audit_model_result(
                    current_markdown,
                    result_markdown,
                )
                _atomic_write_text(audit_path, updated_markdown)
        except Exception:
            logger.warning(
                "LLM Prompt 审查结果写入失败 audit_path=%s",
                audit_path,
                exc_info=True,
            )

    def _record_request_locked(
        self,
        *,
        role: str,
        source: str,
        protocol: str,
        model: str,
        operation: str,
        request: Mapping[str, Any],
        trace_fields: Mapping[str, object],
        llm_call_id: str,
        audit_messages_factory: Callable[[], list[dict[str, Any]]] | None = None,
    ) -> tuple[Path, int]:
        """在两把审计锁已持有时写入单条请求。

        Args:
            role: 当前 LLM 职责或内部用途。
            source: 触发本次调用的业务来源。
            protocol: 当前使用的 SDK 协议。
            model: 本次请求使用的真实模型名。
            operation: 当前调用阶段。
            request: Adapter 已编译完成的最终 SDK 请求。
            trace_fields: 请求、Activity 和调用身份。
            llm_call_id: 本次调用的稳定审查编号。
            audit_messages_factory: 可选的审查消息惰性工厂。

        Returns:
            已写入 Markdown 的路径和本次运行序号。
        """

        self._ensure_run_initialized()
        self._sequence += 1
        sequence = self._sequence
        captured_at = self._normalized_utc_now()
        filename = _build_filename(
            sequence=sequence,
            role=role,
            source=source,
            operation=operation,
            llm_call_id=llm_call_id,
        )
        final_path = self._run_directory / filename
        if audit_messages_factory is None:
            audit_request: Mapping[str, Any] = request
        else:
            mutable_audit_request = dict(request)
            mutable_audit_request["messages"] = audit_messages_factory()
            audit_request = mutable_audit_request
        markdown = render_prompt_audit_markdown(
            captured_at=captured_at,
            process_role=self.process_role,
            run_id=self.run_id,
            sequence=sequence,
            role=role,
            source=source,
            protocol=protocol,
            model=model,
            operation=operation,
            request=audit_request,
            trace_fields=trace_fields,
        )
        _atomic_write_text(final_path, markdown)

        entry = PromptAuditIndexEntry(
            sequence=sequence,
            captured_at=captured_at.isoformat().replace("+00:00", "Z"),
            role=role,
            source=source,
            operation=operation,
            model=model,
            llm_call_id=llm_call_id,
            request_id=_clean_metadata_text(trace_fields.get("request_id")),
            generation_id=_clean_metadata_text(trace_fields.get("generation_id")),
            activity_id=_clean_metadata_text(trace_fields.get("activity_id")),
            filename=filename,
        )
        self._entries.append(entry)
        self._write_run_index_best_effort()
        self._write_process_index_best_effort()
        return final_path, sequence

    def _log_record_success(
        self,
        *,
        final_path: Path,
        sequence: int,
        role: str,
        source: str,
        operation: str,
        llm_call_id: str,
    ) -> None:
        """记录不含 Prompt 正文的审计成功诊断。

        Args:
            final_path: 本次写入的 Markdown 路径。
            sequence: 本次运行内的调用序号。
            role: 当前 LLM 职责或内部用途。
            source: 触发本次调用的业务来源。
            operation: 当前调用阶段。
            llm_call_id: 本次调用的稳定审查编号。
        """

        log_event(
            logger,
            logging.INFO,
            "llm_prompt_audited",
            audit_path=str(final_path),
            run_id=self.run_id,
            sequence=sequence,
            llm_call_id=llm_call_id,
            role=role,
            source=source,
            operation=operation,
        )

    def _log_record_failure(
        self,
        *,
        role: str,
        source: str,
        operation: str,
        llm_call_id: str,
    ) -> None:
        """记录审计旁路失败，不把异常传播到真实模型调用。

        Args:
            role: 当前 LLM 职责或内部用途。
            source: 触发本次调用的业务来源。
            operation: 当前调用阶段。
            llm_call_id: 本次调用的稳定审查编号。
        """

        logger.warning(
            "LLM Prompt 审查文件写入失败 role=%s source=%s operation=%s "
            "llm_call_id=%s",
            role,
            source,
            operation,
            llm_call_id or "unknown",
            exc_info=True,
        )

    def _ensure_run_initialized(self) -> None:
        """首次真实调用时创建运行目录、初始索引并执行整运行清理。"""

        if self._initialized:
            return

        self._run_directory.mkdir(parents=True, exist_ok=True)
        self._prune_old_runs()
        self._write_process_index_best_effort()
        _atomic_write_text(
            self._run_index_path,
            render_prompt_audit_run_index(
                process_role=self.process_role,
                run_id=self.run_id,
                entries=(),
            ),
        )
        self._initialized = True

    def _write_run_index_best_effort(self) -> None:
        """更新当前运行索引；失败不否认已经成功写入的 Markdown。"""

        try:
            _atomic_write_text(
                self._run_index_path,
                render_prompt_audit_run_index(
                    process_role=self.process_role,
                    run_id=self.run_id,
                    entries=tuple(self._entries),
                ),
            )
        except Exception:
            logger.warning(
                "Prompt Audit 运行索引更新失败 run_id=%s",
                self.run_id,
                exc_info=True,
            )

    def _write_process_index(self) -> None:
        """按新到旧顺序写入当前进程角色的运行入口页。"""

        run_ids = [path.name for path in self._existing_run_directories()]
        _atomic_write_text(
            self._process_index_path,
            render_prompt_audit_process_index(
                process_role=self.process_role,
                run_ids=run_ids,
            ),
        )

    def _write_process_index_best_effort(self) -> None:
        """更新进程角色入口页；失败不能否认已经写入的请求文件。"""

        try:
            self._write_process_index()
        except Exception:
            logger.warning(
                "Prompt Audit 进程索引更新失败 process_role=%s",
                self.process_role,
                exc_info=True,
            )

    def _prune_old_runs(self) -> None:
        """按完整运行目录删除超过保留数量的最旧记录。"""

        completed_runs = [
            path
            for path in self._existing_run_directories()
            if not self._run_directory_is_active(path)
        ]
        for path in completed_runs[self.max_runs :]:
            try:
                shutil.rmtree(path)
            except FileNotFoundError:
                continue
            except OSError:
                logger.warning(
                    "Prompt Audit 旧运行目录清理失败 path=%s",
                    path,
                    exc_info=True,
                )

    def _existing_run_directories(self) -> list[Path]:
        """返回按目录名从新到旧排序的全部运行目录。"""

        if not self._runs_directory.exists():
            return []
        return sorted(
            (path for path in self._runs_directory.iterdir() if path.is_dir()),
            key=lambda path: path.name,
            reverse=True,
        )

    def _run_directory_is_active(self, path: Path) -> bool:
        """判断一个运行目录是否仍由存活进程写入。

        Args:
            path: 需要判断的运行目录。

        Returns:
            当前记录器自身或目录 PID 仍存活时返回真。
        """

        if path == self._run_directory:
            return True
        matched_process_id = _RUN_PROCESS_ID.search(path.name)
        if matched_process_id is None:
            return False
        return self._process_is_alive(int(matched_process_id.group(1)))

    def _normalized_utc_now(self) -> datetime:
        """把时间来源归一为带时区的 UTC 时间。"""

        captured_at = self._now()
        if captured_at.tzinfo is None:
            captured_at = captured_at.replace(tzinfo=timezone.utc)
        return captured_at.astimezone(timezone.utc)


def _atomic_write_text(path: Path, content: str) -> None:
    """使用同目录临时文件和原子替换写入 UTF-8 文本。

    Args:
        path: 最终文件路径。
        content: 需要完整写入的文本。
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary_path.write_text(content, encoding="utf-8")
        os.replace(temporary_path, path)
    finally:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            logger.warning(
                "Prompt Audit 临时文件清理失败 path=%s",
                temporary_path,
                exc_info=True,
            )


async def _acquire_thread_lock_without_leak(lock: Any) -> None:
    """异步等待线程锁，并保证等待协程取消后不会遗留幽灵持锁。

    Args:
        lock: 同步调用和异步调用共同使用的进程内线程锁。

    Returns:
        取得锁后返回；调用方负责在最终清理中释放。

    Raises:
        BaseException: 等待期间收到的取消或底层异常原样传播。
    """

    # threading.Lock 同时服务同步 Chat 调用和异步 Background query。非阻塞轮询
    # 不占住事件循环；取消只能发生在 sleep，因而不会出现后台线程迟到持锁。
    while not lock.acquire(blocking=False):
        await asyncio.sleep(_PROCESS_LOCK_RETRY_INTERVAL_S)


async def _await_committed_sdk_call(
    sdk_call: Callable[[], Awaitable[Any]],
) -> Any:
    """让已经落盘的审计一定等到对应 transport 写入结束。

    Args:
        sdk_call: 真正写入 SDK transport 的异步回调。

    Returns:
        SDK 回调返回值。

    Raises:
        BaseException: SDK 自身异常原样传播；外层取消在 transport 收口后传播。
    """

    sdk_task = asyncio.ensure_future(sdk_call())
    try:
        return await asyncio.shield(sdk_task)
    except asyncio.CancelledError as cancellation:
        current_task = asyncio.current_task()
        if current_task is None or current_task.cancelling() == 0:
            # 子任务自己取消时，直接读取它的真实终态，不把它误认成外层取消。
            return sdk_task.result()

        # 审计已经写入后，外层取消属于提交后取消。query 写入很短，必须等它真实
        # 收口再释放 sequence 锁，随后才把同一个取消交给 AgentSession 处理。
        while not sdk_task.done():
            try:
                await asyncio.shield(sdk_task)
            except asyncio.CancelledError:
                continue
            except BaseException:
                break
        try:
            sdk_task.result()
        except BaseException:
            # 外层取消是本次控制流的权威结果；这里只消费子任务异常，避免泄漏告警。
            pass
        raise cancellation


@contextmanager
def _prompt_audit_process_lock(directory: Path) -> Iterator[None]:
    """在同一进程角色的多个进程之间串行化目录维护。

    Args:
        directory: 放置入口索引和互斥锁文件的进程角色目录。

    Yields:
        持有跨进程锁期间的空上下文。
    """

    directory.mkdir(parents=True, exist_ok=True)
    lock_path = directory / ".prompt-audit.lock"
    with lock_path.open("a+b") as lock_file:
        _lock_file(lock_file)
        try:
            yield
        finally:
            _unlock_file(lock_file)


def _lock_file(lock_file: BinaryIO) -> None:
    """在有限时间内取得一个跨进程排他的单字节文件锁。

    Args:
        lock_file: 已打开的二进制锁文件。
    """

    if os.name == "nt":
        import msvcrt

        lock_file.seek(0, os.SEEK_END)
        if lock_file.tell() == 0:
            lock_file.write(b"0")
            lock_file.flush()
        lock_file.seek(0)
        deadline = time.monotonic() + _PROCESS_LOCK_TIMEOUT_S
        while True:
            try:
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                return
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Prompt Audit 跨进程锁等待超时")
                time.sleep(_PROCESS_LOCK_RETRY_INTERVAL_S)

    import fcntl

    deadline = time.monotonic() + _PROCESS_LOCK_TIMEOUT_S
    while True:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise TimeoutError("Prompt Audit 跨进程锁等待超时")
            time.sleep(_PROCESS_LOCK_RETRY_INTERVAL_S)


def _unlock_file(lock_file: BinaryIO) -> None:
    """释放由 `_lock_file` 获取的跨进程文件锁。

    Args:
        lock_file: 已打开的二进制锁文件。
    """

    if os.name == "nt":
        import msvcrt

        lock_file.seek(0)
        msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _process_is_alive(process_id: int) -> bool:
    """判断一个运行目录所属的进程是否仍然存在。

    Args:
        process_id: 写入运行目录名称的系统进程编号。

    Returns:
        当前进程或系统确认仍存在的进程返回真。
    """

    if process_id == os.getpid():
        return True
    try:
        os.kill(process_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _build_filename(
    *,
    sequence: int,
    role: str,
    source: str,
    operation: str,
    llm_call_id: str,
) -> str:
    """构造可按运行时序排序且一眼可辨用途的 Markdown 文件名。"""

    parts = (
        f"{sequence:06d}",
        _safe_filename_part(role),
        _safe_filename_part(source),
        _safe_filename_part(operation),
        _safe_filename_part(llm_call_id),
    )
    return "__".join(parts) + ".md"


def _safe_filename_part(value: object) -> str:
    """把业务标识转换成跨平台安全的短文件名片段。"""

    normalized = _SAFE_FILENAME_PART.sub("-", str(value).strip()).strip("-.")
    return (normalized or "unknown")[:120]


def _clean_metadata_text(value: object) -> str:
    """把元数据值转换成无首尾空白的字符串。"""

    if value is None:
        return ""
    return str(value).strip()
