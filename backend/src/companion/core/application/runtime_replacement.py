from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from companion.activities.runtime.service import ActivityService
from companion.core.assistant_process_supervisor import AssistantProcessSupervisor
from companion.core.realtime_input_runtime_owner import RealtimeInputRuntimeOwner
from companion.core.realtime_input_runtime_owner import RealtimeInputRuntimeReplacement
from companion.realtime.assistant_process_client import AssistantProcessClient
from companion.realtime.assistant_process_handle import AssistantProcessReplacement
from companion.realtime.realtime_input_runtime import RealtimeInputRuntime
from companion.observability.logging_config import get_backend_logger


RuntimeReplacementState = Literal["prepared", "committing", "committed", "rolled_back"]
logger = get_backend_logger(__name__)


async def _finish_resource_work(work: Awaitable[Any]) -> Any:
    """取消请求时仍等待资源工作交回所有权，再把取消传给调用方。

    Args:
        work: 已经启动或即将启动的资源线程、提交后清理协程。
    Returns:
        工作结果；重复取消不会终止仍持有资源的后台任务。
    """
    task = asyncio.ensure_future(work)
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            cancelled = True
        except Exception:
            if cancelled:
                raise asyncio.CancelledError from None
            raise
    if cancelled:
        raise asyncio.CancelledError
    return result


@dataclass(frozen=True, slots=True)
class RuntimeReplacementResult:
    """描述一次运行时替换实际提交的资源与世代。

    Args:
        realtime_input_replaced: 是否发布了新的实时输入运行时。
        assistant_process_replaced: 是否发布了新的助手子进程客户端。
        previous_runtime_epoch: 被替换的助手子进程世代；首次发布时为空。
        current_runtime_epoch: 提交后的助手子进程世代；未替换助手进程时为空。
    """

    realtime_input_replaced: bool
    assistant_process_replaced: bool
    previous_runtime_epoch: int | None
    current_runtime_epoch: int | None


class RuntimeReplacementTransaction:
    """持有运行时候选，并原子表达提交或回滚后的所有权转移。

    Args:
        realtime_input_runtime_owner: 实时输入运行时所有者。
        assistant_process_supervisor: 助手子进程所有者。
        activity_service: 负责把旧世代 Activity 收口为中断终态的服务。
        interrupt_runtime_activities: 回收旧世代 Notice 与材料租约的同步端口。
        interrupt_background_agent_interactions: 作废旧世代待答请求并生成卡片收口事件的端口。
        release_runtime_memory_compaction: 释放旧助手世代拥有的滚动压缩计划。
        broadcast_ephemeral: 广播材料释放等瞬时控制事件的端口。
        restart_livekit_media_session: 使用当前输入 Runtime 重建活跃媒体会话的端口。
        realtime_input_candidate: 尚未发布的实时输入候选。
        assistant_process_candidate: 尚未发布的助手子进程候选。
    """

    def __init__(
        self,
        *,
        realtime_input_runtime_owner: RealtimeInputRuntimeOwner,
        assistant_process_supervisor: AssistantProcessSupervisor,
        activity_service: ActivityService,
        interrupt_runtime_activities: Callable[[int], list[dict[str, object]]],
        interrupt_background_agent_interactions: Callable[
            [int], list[dict[str, object]]
        ],
        release_runtime_memory_compaction: Callable[[int], bool],
        broadcast_ephemeral: Callable[[dict[str, object]], Awaitable[None]],
        restart_livekit_media_session: Callable[[], Awaitable[bool]],
        terminate_self_reflection_runtime: Callable[[int, int], object] | None,
        publish_assistant_candidate: Callable[
            [
                AssistantProcessClient,
                Callable[[AssistantProcessReplacement], None],
            ],
            AssistantProcessReplacement,
        ] | None,
        realtime_input_candidate: RealtimeInputRuntime | None,
        assistant_process_candidate: AssistantProcessClient | None,
    ) -> None:
        self.realtime_input_runtime_owner = realtime_input_runtime_owner
        self.assistant_process_supervisor = assistant_process_supervisor
        self.activity_service = activity_service
        self.interrupt_runtime_activities = interrupt_runtime_activities
        self.interrupt_background_agent_interactions = (
            interrupt_background_agent_interactions
        )
        self.release_runtime_memory_compaction = release_runtime_memory_compaction
        self.broadcast_ephemeral = broadcast_ephemeral
        self.restart_livekit_media_session = restart_livekit_media_session
        self.terminate_self_reflection_runtime = terminate_self_reflection_runtime
        self.publish_assistant_candidate = publish_assistant_candidate
        self.realtime_input_candidate = realtime_input_candidate
        self.assistant_process_candidate = assistant_process_candidate
        self.state: RuntimeReplacementState = "prepared"
        self._assistant_process_published = False
        self._realtime_input_published = False

    @classmethod
    def prepare(
        cls,
        *,
        realtime_input_runtime_owner: RealtimeInputRuntimeOwner,
        assistant_process_supervisor: AssistantProcessSupervisor,
        activity_service: ActivityService,
        interrupt_runtime_activities: Callable[[int], list[dict[str, object]]],
        interrupt_background_agent_interactions: Callable[
            [int], list[dict[str, object]]
        ],
        release_runtime_memory_compaction: Callable[[int], bool],
        broadcast_ephemeral: Callable[[dict[str, object]], Awaitable[None]],
        restart_livekit_media_session: Callable[[], Awaitable[bool]],
        terminate_self_reflection_runtime: Callable[[int, int], object] | None,
        publish_assistant_candidate: Callable[
            [
                AssistantProcessClient,
                Callable[[AssistantProcessReplacement], None],
            ],
            AssistantProcessReplacement,
        ] | None = None,
        include_realtime_input: bool = True,
        include_assistant_process: bool = True,
    ) -> RuntimeReplacementTransaction:
        """构造并接管本次替换所需的候选资源。

        Args:
            realtime_input_runtime_owner: 实时输入运行时所有者。
            assistant_process_supervisor: 助手子进程所有者。
            activity_service: 旧世代 Activity 中断服务。
            interrupt_runtime_activities: 旧世代 Notice 与材料租约回收端口。
            interrupt_background_agent_interactions: 旧世代待答请求回收端口。
            release_runtime_memory_compaction: 旧世代滚动压缩计划释放端口。
            broadcast_ephemeral: 瞬时控制事件广播端口。
            restart_livekit_media_session: 使用当前输入 Runtime 重建活跃媒体会话的端口。
            include_realtime_input: 是否为本次设置变更构造输入运行时候选。
            include_assistant_process: 是否为本次设置变更构造助手子进程候选。

        Returns:
            已接管全部候选资源的待提交事务。
        """

        realtime_input_candidate = (
            realtime_input_runtime_owner.build_candidate()
            if include_realtime_input
            else None
        )
        try:
            assistant_process_candidate = (
                assistant_process_supervisor.build_candidate()
                if include_assistant_process
                else None
            )
        except Exception:
            realtime_input_runtime_owner.shutdown_runtime(realtime_input_candidate)
            raise

        return cls(
            realtime_input_runtime_owner=realtime_input_runtime_owner,
            assistant_process_supervisor=assistant_process_supervisor,
            activity_service=activity_service,
            interrupt_runtime_activities=interrupt_runtime_activities,
            interrupt_background_agent_interactions=(
                interrupt_background_agent_interactions
            ),
            release_runtime_memory_compaction=release_runtime_memory_compaction,
            broadcast_ephemeral=broadcast_ephemeral,
            restart_livekit_media_session=restart_livekit_media_session,
            terminate_self_reflection_runtime=terminate_self_reflection_runtime,
            publish_assistant_candidate=publish_assistant_candidate,
            realtime_input_candidate=realtime_input_candidate,
            assistant_process_candidate=assistant_process_candidate,
        )

    async def commit(
        self, *, report_phase: Callable[[str], Awaitable[None]] | None = None,
    ) -> RuntimeReplacementResult:
        """发布候选资源并完成旧助手世代的补偿与关闭。

        Args:
            report_phase: 授权恢复的真实阶段与世代检查端口；普通配置事务可省略。

        Returns:
            本次实际替换的资源和助手世代结果。

        Raises:
            RuntimeError: 事务已提交、正在提交或已经回滚。
        """

        if self.state != "prepared":
            raise RuntimeError(f"runtime replacement transaction cannot commit from {self.state}")

        self.state = "committing"
        assistant_replacement: AssistantProcessReplacement | None = None
        input_replacement: RealtimeInputRuntimeReplacement | None = None

        def mark_assistant_published(
            replacement: AssistantProcessReplacement,
        ) -> None:
            """在稳定句柄交换完成的同一调用栈内标记不可回滚提交点。

            Args:
                replacement: Supervisor 刚完成的助手世代交接事实。

            Returns:
                无返回值。
            """

            nonlocal assistant_replacement
            if not isinstance(replacement, AssistantProcessReplacement):
                raise TypeError("助手候选发布必须返回 AssistantProcessReplacement")
            if not self.assistant_process_supervisor.is_current_target(
                self.assistant_process_candidate
            ):
                raise RuntimeError("助手候选尚未成为稳定句柄的现役目标")
            assistant_replacement = (
                self.assistant_process_supervisor.take_published_replacement(
                    self.assistant_process_candidate
                )
                or replacement
            )
            self._assistant_process_published = True
            self.state = "committed"

        try:
            if self.assistant_process_candidate is not None:
                if report_phase is not None:
                    await report_phase("assistant")
                # Windows 进程启动也会访问磁盘；取消后要先等线程交回候选才能关闭它。
                await _finish_resource_work(asyncio.to_thread(
                    self.assistant_process_supervisor.start_candidate,
                    self.assistant_process_candidate,
                ))
                # Process.start() 只代表子进程句柄已创建；候选在完成模型、TTS
                # 和常驻 worker 装配前不能发布，否则管理 RPC 会在启动竞态中超时。
                wait_until_ready = getattr(
                    self.assistant_process_supervisor,
                    "wait_until_ready",
                    None,
                )
                if callable(wait_until_ready):
                    await _finish_resource_work(asyncio.to_thread(
                        wait_until_ready,
                        self.assistant_process_candidate,
                    ))

            if self.realtime_input_candidate is not None:
                if report_phase is not None:
                    await report_phase("media")
                input_replacement = self.realtime_input_runtime_owner.publish_candidate(
                    self.realtime_input_candidate
                )
                if input_replacement is None:
                    raise RuntimeError("输入 Runtime 候选存在但 Owner 未返回交接结果")
                self._realtime_input_published = True
                await self.restart_livekit_media_session()

            # 媒体启动会让出事件循环；期间换卡或到期必须阻止候选进入稳定句柄。
            if report_phase is not None:
                await report_phase("media")

            # 助手稳定句柄只在输入 Runtime 和活跃媒体消费者都准备完成后切换；
            # 此后不再执行会影响提交结果的操作，发布即是事务提交点。
            if self.assistant_process_candidate is not None:
                publish_candidate = self.publish_assistant_candidate
                reported_replacement = (
                    publish_candidate(
                        self.assistant_process_candidate,
                        mark_assistant_published,
                    )
                    if publish_candidate is not None
                    else self.assistant_process_supervisor.publish_candidate(
                        self.assistant_process_candidate
                    )
                )
                # 自定义发布器必须在句柄交换后立刻调用标记回调；这里保留防御，
                # 避免违反内部契约的实现让事务停留在 committing。
                if not self._assistant_process_published:
                    mark_assistant_published(reported_replacement)
        except BaseException:
            # 发布回调可能已经完成稳定句柄交换，随后才在旧世代记账时抛错。
            # 一旦候选成为现役目标，输入 Runtime 与助手进程都已经越过提交点，
            # 不能再恢复旧配置；否则 Settings/SQLite 会和实际助手世代分叉。
            assistant_process_was_published = (
                self._assistant_process_published
                or self.assistant_process_supervisor.is_current_target(
                    self.assistant_process_candidate
                )
            )
            if assistant_process_was_published:
                self._assistant_process_published = True
                self.state = "committed"
                assistant_replacement = assistant_replacement or (
                    self.assistant_process_supervisor.take_published_replacement(
                        self.assistant_process_candidate
                    )
                )
                # 句柄交换已经是不可回滚提交点。包装器违反契约也只能记诊断，
                # 不能把已生效的配置报告为失败；Supervisor 留存的交接事实
                # 让事务仍可继续回收旧进程和旧世代资源。
                logger.exception("助手稳定句柄交换后的发布包装器失败，按已提交继续收口")
            else:
                if input_replacement is not None:
                    await self._restore_realtime_input_replacement(input_replacement)
                try:
                    await _finish_resource_work(asyncio.to_thread(self._dispose_unpublished_candidates))
                finally:
                    self.state = "rolled_back"
                raise

        # 新资源已经成为唯一权威所有者。旧资源回收和领域投影属于提交后清理，
        # 失败只能记录，不能让调用方回滚已经生效的持久化配置。
        self.state = "committed"
        await _finish_resource_work(self._retire_replaced_resources(input_replacement, assistant_replacement))
        return RuntimeReplacementResult(
            realtime_input_replaced=self._realtime_input_published,
            assistant_process_replaced=self._assistant_process_published,
            previous_runtime_epoch=(
                assistant_replacement.old_epoch
                if assistant_replacement is not None
                else None
            ),
            current_runtime_epoch=(
                assistant_replacement.current_epoch
                if assistant_replacement is not None
                else None
            ),
        )

    async def _retire_replaced_resources(
        self,
        input_replacement: RealtimeInputRuntimeReplacement | None,
        assistant_replacement: AssistantProcessReplacement | None,
    ) -> None:
        """交接完成后关闭旧资源，调用请求取消不能跳过任何旧 Owner。

        Args:
            input_replacement: 已提交的输入资源交接，没有替换时为空。
            assistant_replacement: 已提交的助手世代交接，没有替换时为空。
        Returns:
            无；关闭失败由对应 Owner 保留引用，后续重试不会恢复旧世代。
        """
        if input_replacement is not None:
            try:
                await self.realtime_input_runtime_owner.broadcast_current_runtime_identity()
            except Exception:
                logger.exception("输入 Runtime 身份变化广播失败")
            try:
                await asyncio.to_thread(self.realtime_input_runtime_owner.shutdown_runtime,
                    input_replacement.old_runtime)
            except Exception:
                logger.exception("旧实时输入 Runtime 关闭失败")
        if assistant_replacement is not None:
            await self._retire_previous_assistant_process(assistant_replacement)

    async def _restore_realtime_input_replacement(
        self,
        replacement: RealtimeInputRuntimeReplacement,
    ) -> None:
        """恢复提交点之前已经发布的输入 Runtime 和媒体消费者。

        Args:
            replacement: 等待回滚的新旧输入资源交接结果。
        """

        try:
            self.realtime_input_runtime_owner.restore_replacement(replacement)
            self._realtime_input_published = False
        except Exception:
            logger.exception("恢复实时输入 Runtime 所有权失败")
            return

        try:
            await self.restart_livekit_media_session()
        except BaseException:
            # 原始提交失败仍是对外主异常；恢复媒体链失败记录完整诊断，
            # Owner 已恢复为旧 Runtime，后续会话请求仍可重新建立消费者。
            logger.exception("恢复旧实时媒体会话失败")

    def rollback(self) -> bool:
        """回收所有尚未发布的候选资源。

        Returns:
            本次调用是否完成了从 prepared 到 rolled_back 的状态转换。
        """

        if self.state != "prepared":
            return False
        self._dispose_unpublished_candidates()
        self.state = "rolled_back"
        return True

    async def _retire_previous_assistant_process(
        self,
        replacement: AssistantProcessReplacement,
    ) -> None:
        """中断旧世代的领域状态并关闭旧助手进程。

        Args:
            replacement: 稳定句柄发布新客户端后返回的世代交接结果。
        """

        if replacement.old_runtime is None or replacement.old_epoch is None:
            return

        # 新 Runtime 已经发布后，先终止主进程 Attention。旧 Recovery 不恢复，
        # 迟到的 outcome、工具提交和 episode 输入只能被新世代门禁吸收。
        if self.terminate_self_reflection_runtime is not None:
            try:
                self.terminate_self_reflection_runtime(
                    replacement.old_epoch,
                    replacement.current_epoch,
                )
            except Exception:
                logger.exception(
                    "旧助手世代 Self-Reflection 收口失败 runtime_epoch=%s",
                    replacement.old_epoch,
                )

        try:
            self.release_runtime_memory_compaction(replacement.old_epoch)
        except Exception:
            logger.exception(
                "旧助手世代运行期记忆压缩计划释放失败 runtime_epoch=%s",
                replacement.old_epoch,
            )

        try:
            await self.activity_service.interrupt_active(
                replacement.old_epoch,
                "runtime_reconfigured",
            )
        except Exception:
            logger.exception(
                "旧助手世代 Activity 收口失败 runtime_epoch=%s",
                replacement.old_epoch,
            )

        release_events: list[dict[str, object]] = []
        try:
            release_events = self.interrupt_runtime_activities(
                replacement.old_epoch
            )
        except Exception:
            logger.exception(
                "旧助手世代 Notice 与材料租约收口失败 runtime_epoch=%s",
                replacement.old_epoch,
            )
        try:
            release_events.extend(
                self.interrupt_background_agent_interactions(
                    replacement.old_epoch
                )
            )
        except Exception:
            logger.exception(
                "旧助手世代后台交互请求收口失败 runtime_epoch=%s",
                replacement.old_epoch,
            )
        finally:
            try:
                await asyncio.to_thread(self.assistant_process_supervisor.shutdown_process,
                    replacement.old_runtime,
                    runtime_epoch=replacement.old_epoch,
                )
            except Exception:
                logger.exception(
                    "旧助手子进程关闭失败 runtime_epoch=%s",
                    replacement.old_epoch,
                )

        for event in release_events:
            try:
                await self.broadcast_ephemeral(event)
            except Exception:
                logger.exception(
                    "旧助手世代材料释放事件广播失败 runtime_epoch=%s event_type=%s",
                    replacement.old_epoch,
                    event.get("type"),
                )

    def _dispose_unpublished_candidates(self) -> None:
        """关闭仍由事务持有、尚未发布的候选资源。"""

        # 发布包装器可能在句柄交换完成后抛错，此时候选已经是现役助手进程；
        # 把它当作未发布资源销毁，会让稳定句柄继续向死进程派发请求。
        try:
            if (
                not self._assistant_process_published
                and not self.assistant_process_supervisor.is_current_target(
                    self.assistant_process_candidate
                )
            ):
                self.assistant_process_supervisor.shutdown_process(
                    self.assistant_process_candidate
                )
        finally:
            # 两类候选独立拥有资源；助手关闭异常也不能跳过输入模型的交回。
            if not self._realtime_input_published:
                self.realtime_input_runtime_owner.shutdown_runtime(
                    self.realtime_input_candidate
                )
