"""通过非阻塞单写队列隔离后台工作用量 SQLite I/O。"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date
from functools import partial

from companion.activities.runtime.identity import canonical_activity_uuid
from companion.background_agent.usage_models import AgentTaskUsageSnapshot
from companion.background_agent.usage_reporting import (
    BackgroundAgentUsageReport,
    build_usage_report,
)
from companion.background_agent.usage_repository import (
    BackgroundAgentUsageRepository,
)
from companion.observability.logging_config import get_backend_logger


logger = get_backend_logger(__name__)

DEFAULT_QUEUE_CAPACITY = 256
DEFAULT_SHUTDOWN_TIMEOUT_SECONDS = 2.0


class BackgroundAgentUsageUnavailableError(RuntimeError):
    """表示本次 Backend 进程无法使用后台工作用量仓储。"""


@dataclass(frozen=True, slots=True)
class _RecordUsageCommand:
    """表示一项已经完成解析、等待写入的终态用量。"""

    activity_id: str
    terminal_at_ms: int
    usage: AgentTaskUsageSnapshot


@dataclass(frozen=True, slots=True)
class _QueryUsageCommand:
    """表示一个等待 Repository 和 Reporting 完成的日期查询。"""

    from_date: date
    to_date: date
    result: asyncio.Future[BackgroundAgentUsageReport]


_UsageCommand = _RecordUsageCommand | _QueryUsageCommand
_STOP = object()


class BackgroundAgentUsageService:
    """串行调度用量读写，并保证磁盘故障不反向改变任务终态。"""

    def __init__(
        self,
        repository: BackgroundAgentUsageRepository,
        *,
        queue_capacity: int = DEFAULT_QUEUE_CAPACITY,
        shutdown_timeout_seconds: float = DEFAULT_SHUTDOWN_TIMEOUT_SECONDS,
    ) -> None:
        """保存 Repository 和队列策略，等待应用生命周期启动。

        Args:
            repository: 独立的后台工作用量 SQLite Repository。
            queue_capacity: 等待处理的写入和查询命令总上限。
            shutdown_timeout_seconds: 正常关闭时等待队列排空的最长秒数。
        """

        if queue_capacity <= 0:
            raise ValueError("后台工作用量队列容量必须大于零")
        if shutdown_timeout_seconds <= 0:
            raise ValueError("后台工作用量关闭超时必须大于零")
        self._repository = repository
        self._queue_capacity = queue_capacity
        self._shutdown_timeout_seconds = shutdown_timeout_seconds
        self._queue: asyncio.Queue[_UsageCommand | object] | None = None
        self._worker_task: asyncio.Task[None] | None = None
        self._executor: ThreadPoolExecutor | None = None
        self._initialization_error: Exception | None = None
        self._accepting_commands = False

    async def startup(self) -> None:
        """在线程外初始化数据库，然后启动唯一命令消费者。"""

        if self._worker_task is not None or self._initialization_error is not None:
            return
        executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="background-agent-usage",
        )
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(executor, self._repository.initialize)
        except Exception as exception:
            self._initialization_error = exception
            executor.shutdown(wait=False, cancel_futures=True)
            logger.exception("初始化后台工作用量仓储失败")
            return

        self._executor = executor
        self._queue = asyncio.Queue(maxsize=self._queue_capacity)
        self._accepting_commands = True
        self._worker_task = asyncio.create_task(
            self._run_worker(),
            name="background agent usage worker",
        )

    def record_terminal_event(
        self,
        event_type: str,
        payload: dict[str, object],
    ) -> None:
        """解析内部终态用量并立即放入非阻塞队列。

        Args:
            event_type: 完整的后台工作终态事件名称。
            payload: 尚未向公共 Activity 投影的内部事件载荷。
        """

        if event_type not in {
            "background_agent.completed",
            "background_agent.failed",
            "background_agent.cancelled",
        }:
            return
        try:
            command = self._parse_record_command(payload)
        except Exception:
            logger.exception("解析后台工作终态用量失败 event_type=%s", event_type)
            return
        if not self._accepting_commands or self._queue is None:
            logger.warning("后台工作用量服务未就绪，本次终态用量未保存")
            return
        try:
            self._queue.put_nowait(command)
        except asyncio.QueueFull:
            # 用量是旁路观测数据；队列异常堆积时宁可丢弃指标，也不能拖住公开终态。
            logger.error("后台工作用量队列已满，本次终态用量未保存")

    async def query(
        self,
        *,
        from_date: date,
        to_date: date,
    ) -> BackgroundAgentUsageReport:
        """把日期查询排到既有写入之后并异步等待结果。

        Args:
            from_date: 查询包含的首日。
            to_date: 查询包含的末日。

        Returns:
            总览、按模型和按固化日期汇总的类型化报告。

        Raises:
            ValueError: 结束日期早于开始日期。
            BackgroundAgentUsageUnavailableError: 服务未就绪、队列已满或查询失败。
        """

        if to_date < from_date:
            raise ValueError("用量查询结束日期不能早于开始日期")
        if (
            self._initialization_error is not None
            or not self._accepting_commands
            or self._queue is None
        ):
            raise BackgroundAgentUsageUnavailableError("后台工作用量仓储当前不可用")

        result = asyncio.get_running_loop().create_future()
        try:
            self._queue.put_nowait(
                _QueryUsageCommand(
                    from_date=from_date,
                    to_date=to_date,
                    result=result,
                )
            )
        except asyncio.QueueFull as exception:
            raise BackgroundAgentUsageUnavailableError(
                "后台工作用量仓储当前不可用"
            ) from exception
        return await result

    async def shutdown(self) -> None:
        """停止接收新命令，并在预算内排空已经接收的读写。"""

        self._accepting_commands = False
        queue = self._queue
        worker_task = self._worker_task
        executor = self._executor
        if queue is None or worker_task is None or executor is None:
            return

        can_wait_for_executor = True
        try:
            await asyncio.wait_for(
                queue.join(),
                timeout=self._shutdown_timeout_seconds,
            )
        except TimeoutError:
            logger.warning("后台工作用量队列未能在关闭预算内排空")
            can_wait_for_executor = self._interrupt_repository_operations()
            self._fail_queued_queries(queue)
            worker_task.cancel()
        else:
            queue.put_nowait(_STOP)
        try:
            await worker_task
        except asyncio.CancelledError:
            pass
        finally:
            # 正常路径没有在途任务；超时路径只有 Repository 确认接受中断后才等待线程，
            # 避免未知测试替身或第三方实现让应用关闭无限阻塞。
            executor.shutdown(wait=can_wait_for_executor, cancel_futures=True)
            self._worker_task = None
            self._queue = None
            self._executor = None

    def _interrupt_repository_operations(self) -> bool:
        """请求 Repository 中断在途 SQLite，并报告是否可安全等待线程退出。

        Returns:
            Repository 已接受中断请求时返回真；缺少端口或调用失败时返回假。
        """

        interrupt_operations = getattr(
            self._repository,
            "interrupt_active_operations",
            None,
        )
        if not callable(interrupt_operations):
            logger.error("后台工作用量 Repository 不支持关闭中断")
            return False
        try:
            interrupt_operations()
        except Exception:
            logger.exception("中断后台工作用量 Repository 失败")
            return False
        return True

    async def _run_worker(self) -> None:
        """按队列顺序在专用单线程执行器中完成所有 SQLite I/O。"""

        queue = self._queue
        executor = self._executor
        if queue is None or executor is None:
            return
        loop = asyncio.get_running_loop()
        while True:
            command = await queue.get()
            if command is _STOP:
                queue.task_done()
                return
            try:
                if isinstance(command, _RecordUsageCommand):
                    await loop.run_in_executor(
                        executor,
                        partial(
                            self._repository.record_terminal_usage,
                            activity_id=command.activity_id,
                            terminal_at_ms=command.terminal_at_ms,
                            usage=command.usage,
                        ),
                    )
                    continue
                if isinstance(command, _QueryUsageCommand):
                    # 页面切换会取消旧 HTTP 请求；对应 Future 已取消时不再执行昂贵查询，
                    # 避免无消费者的读负载占住单线程并反向挤压终态写入。
                    if command.result.cancelled():
                        continue
                    await self._execute_query(command, loop, executor)
            except asyncio.CancelledError:
                # 关闭超时只会取消消费者；已经出队的查询必须同步失败，否则 HTTP 调用方
                # 仍在等待一个再也没有消费者会填写的 Future。
                if isinstance(command, _QueryUsageCommand) and not command.result.done():
                    command.result.set_exception(
                        BackgroundAgentUsageUnavailableError(
                            "后台工作用量仓储当前不可用"
                        )
                    )
                raise
            except Exception:
                if isinstance(command, _QueryUsageCommand):
                    logger.exception("查询后台工作用量失败")
                    if not command.result.done():
                        command.result.set_exception(
                            BackgroundAgentUsageUnavailableError(
                                "后台工作用量仓储当前不可用"
                            )
                        )
                else:
                    logger.exception("保存后台工作终态用量失败")
            finally:
                queue.task_done()

    async def _execute_query(
        self,
        command: _QueryUsageCommand,
        loop: asyncio.AbstractEventLoop,
        executor: ThreadPoolExecutor,
    ) -> None:
        """在专用线程中查询事实并构造报告。

        Args:
            command: 日期范围和等待结果的 Future。
            loop: 当前 Backend 事件循环。
            executor: 用量服务独占的单线程执行器。
        """

        report = await loop.run_in_executor(
            executor,
            partial(
                self._query_report,
                from_date=command.from_date,
                to_date=command.to_date,
            ),
        )
        if not command.result.done():
            command.result.set_result(report)

    def _query_report(
        self,
        *,
        from_date: date,
        to_date: date,
    ) -> BackgroundAgentUsageReport:
        """同步读取 Repository 并聚合报告，供专用线程调用。"""

        tasks = self._repository.query_tasks(
            from_date=from_date,
            to_date=to_date,
        )
        return build_usage_report(
            tasks=tasks,
            from_date=from_date,
            to_date=to_date,
        )

    @staticmethod
    def _parse_record_command(payload: dict[str, object]) -> _RecordUsageCommand:
        """校验内部终态字段并生成不可变写入命令。"""

        activity_id = canonical_activity_uuid(payload.get("activity_id"))
        if activity_id is None:
            raise ValueError("后台工作用量缺少合法 Activity 编号")
        terminal_at = payload.get("created_at")
        if isinstance(terminal_at, bool) or not isinstance(terminal_at, (int, float)):
            raise ValueError("后台工作用量缺少终态时间")
        usage = AgentTaskUsageSnapshot.from_payload(payload.get("internal_usage"))
        return _RecordUsageCommand(
            activity_id=activity_id,
            terminal_at_ms=int(terminal_at),
            usage=usage,
        )

    @staticmethod
    def _fail_queued_queries(
        queue: asyncio.Queue[_UsageCommand | object],
    ) -> None:
        """关闭超时时让仍在队列中的 HTTP 查询明确失败。"""

        while True:
            try:
                command = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            if isinstance(command, _QueryUsageCommand) and not command.result.done():
                command.result.set_exception(
                    BackgroundAgentUsageUnavailableError(
                        "后台工作用量仓储当前不可用"
                    )
                )
            queue.task_done()
