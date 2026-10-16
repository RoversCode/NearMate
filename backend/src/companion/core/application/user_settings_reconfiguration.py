"""协调所有可持久化用户设置与受管 Skill 的原子提交。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from copy import deepcopy
from typing import Literal, TypeVar

from starlette.concurrency import run_in_threadpool

from companion.background_agent.config_commit_lock import (
    BackgroundAgentConfigCommitLock,
)
from companion.background_agent.settings import BackgroundAgentSettings
from companion.background_agent.skill_registry import (
    SkillRegistry,
    SkillRevisionConflictError,
)
from companion.core.application.errors import StateConflict
from companion.core.application.general_settings import (
    GeneralSettingsUpdate,
    general_settings_field_updates,
)
from companion.core.audio_devices import audio_device_selection_payload
from companion.core.broadcast import BroadcastHub
from companion.core.settings import Settings
from companion.core.shortcut_status import ShortcutStatusItem
from companion.core.user_settings_store import (
    MAX_SAFE_SETTINGS_REVISION,
    USER_SETTINGS_FIELDS,
)
from companion.observability.logging_config import get_backend_logger
from companion.realtime.shell_projector import ShellProjector


logger = get_backend_logger(__name__)


CallbackResult = TypeVar("CallbackResult")


async def _run_sync_callback_to_completion(
    callback: Callable[..., CallbackResult],
    *args: object,
) -> CallbackResult:
    """取消请求时也等同步事务回调收口，再把取消交给调用方。

    ``run_in_threadpool`` 的 worker 线程无法被 asyncio 直接取消。若请求在回调
    执行期间被取消，直接等待它会让调用方立刻进入回滚，而原 worker 仍可能稍后
    写入角色卡、切换运行时或创建资产，最终把补偿结果覆盖掉。这里显式保留
    worker 任务并等待它结束；回调完成后仍按原语义向上抛出取消。

    Args:
        callback: 需要在线程池中运行的同步事务回调。
        *args: 传给回调的位置参数。

    Returns:
        回调的返回值。

    Raises:
        BaseException: 回调异常或调用方取消请求；取消时保证 worker 已收口。
    """

    worker = asyncio.create_task(run_in_threadpool(callback, *args))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        # asyncio.Task.cancel() 会绕过 anyio worker 的默认 shield；重复取消也
        # 不能让仍持有外部副作用的线程变成无主任务，因此一直等到它结束。
        while True:
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                if worker.cancelled():
                    break
                continue
            except BaseException:
                # 回调异常已被 await 消费；当前请求的取消优先于该异常继续传播。
                break
            else:
                break
        raise


class UserSettingsCoordinator:
    """串行提交用户设置、后台配置和受管 Skill 偏好。

    Args:
        settings: 当前进程使用的可变设置对象。
        hub: 设置持久化、完整快照和控制广播中心。
        background_agent_config_commit_lock: 与助手子进程配置读取共用的文件锁。
        skill_registry: 只读取 NearMate 产品资产的受管 Skill 服务。
        configuration_mutation_lock: 所有设置和运行时配置变更共用的进程内锁。
    """

    def __init__(
        self,
        *,
        settings: Settings,
        hub: BroadcastHub,
        background_agent_config_commit_lock: BackgroundAgentConfigCommitLock,
        skill_registry: SkillRegistry | None = None,
        configuration_mutation_lock: asyncio.Lock | None = None,
    ) -> None:
        """创建一个绑定当前设置与持久化边界的协调器。

        Args:
            settings: 当前进程使用的可变设置对象。
            hub: 设置持久化、完整快照和控制广播中心。
            background_agent_config_commit_lock: 与助手子进程配置读取共用的文件锁。
            skill_registry: 只读取 NearMate 产品资产的受管 Skill 服务。
            configuration_mutation_lock: 所有设置和运行时配置变更共用的进程内锁。
        """

        self.settings = settings
        self.hub = hub
        self.background_agent_config_commit_lock = background_agent_config_commit_lock
        self.skill_registry = skill_registry
        self._configuration_mutation_lock = configuration_mutation_lock or asyncio.Lock()

    async def commit_user_settings(
        self,
        *,
        prepare_update: Callable[[Settings], Mapping[str, object]],
        rollback_external: Callable[[Settings], None] | None = None,
        after_commit: Callable[[dict[str, object]], Awaitable[None]] | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, object]:
        """在统一提交锁内准备并提交一组用户设置字段。

        `prepare_update` 会在拿到最新设置后执行。它可以先准备运行期副作用，
        但不能直接修改共享 `Settings`，只能返回本次调用方拥有的字段和值。
        两个同步回调都会在线程池中运行，允许执行硬阻塞 RPC 或磁盘补偿；
        串行性由配置变更锁保证，回调体本身必须线程安全且不依赖事件循环。

        Args:
            prepare_update: 基于最新设置准备运行期状态并返回显式字段 patch 的函数。
            rollback_external: 提交失败时恢复运行期、资产或其他外部状态的函数。
            after_commit: 提交后仍需按 revision 顺序投递的异步动作。
            expected_revision: 可选的客户端已知版本；过期时不执行任何准备动作。

        Returns:
            已提交的新设置快照。

        Raises:
            ValueError: patch 包含不属于用户设置快照的字段时抛出。
            StateConflict: expected_revision 与当前版本不一致时抛出。
        """

        async with self._configuration_mutation_lock:
            snapshot = await self._commit_user_settings_locked(
                prepare_update=prepare_update,
                rollback_external=rollback_external,
                expected_revision=expected_revision,
            )
            # 这类动作不参与持久化回滚，但必须留在门禁内保持提交顺序。
            # 例如两个音频设备请求不能让较旧的应用事件在新 revision 之后到达。
            if after_commit is not None:
                try:
                    await after_commit(snapshot)
                except asyncio.CancelledError:
                    # 请求取消仍然向上层传播；持久化提交已经完成，不能在这里回滚
                    # revision，只能让重连或下一次读取重新取得权威快照。
                    raise
                except Exception:
                    # 提交后的设备应用/通知失败不能把已经落盘的设置伪装成保存失败，
                    # 否则 Console 会继续携带旧 revision，下一次普通设置就会收到 409。
                    # 具体运行期结果由对应的异步回执或超时提示负责呈现。
                    logger.exception(
                        "用户设置已提交，但提交后动作失败 revision=%s",
                        snapshot.get("settings_revision"),
                    )
            return snapshot

    async def _commit_user_settings_locked(
        self,
        *,
        prepare_update: Callable[[Settings], Mapping[str, object]],
        rollback_external: Callable[[Settings], None] | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, object]:
        """在调用方持有提交锁时完成用户设置事务。

        Args:
            prepare_update: 基于最新设置准备运行期状态并返回显式字段 patch 的函数；
                在线程池中执行，允许硬阻塞但不能操作 asyncio 对象。
            rollback_external: 失败时恢复本次事务外部副作用的函数；同样在线程池中执行。
            expected_revision: 可选的客户端已知版本；必须在准备动作前比较。

        Returns:
            已保存并广播的完整设置快照。

        Raises:
            ValueError: patch 尝试修改非用户设置字段时抛出。
            StateConflict: expected_revision 与当前版本不一致时抛出。
        """

        previous_settings = self.settings.model_copy(deep=True)
        previous_settings_revision = self.hub.settings_revision
        field_updates: dict[str, object] = {}
        prepare_started = False
        settings_apply_started = False
        persistence_started = False
        durable_commit_completed = False
        try:
            self._raise_if_settings_revision_exhausted(
                previous_settings_revision
            )
            if (
                expected_revision is not None
                and expected_revision != previous_settings_revision
            ):
                raise StateConflict(
                    {
                        "code": "settings_revision_conflict",
                        "expected_revision": expected_revision,
                        "actual_revision": previous_settings_revision,
                    }
                )

            prepare_started = True
            # 回调里允许执行跨进程 RPC、SQLite 写入或模型重建等硬阻塞动作（如
            # StreamVox 切换），必须经线程池运行；串行性仍由配置变更锁保证。
            field_updates = dict(
                await _run_sync_callback_to_completion(
                    prepare_update,
                    self.settings,
                )
            )
            unsupported_fields = sorted(
                str(field_name)
                for field_name in field_updates
                if field_name not in USER_SETTINGS_FIELDS
            )
            if unsupported_fields:
                raise ValueError(
                    "设置事务包含无权修改的字段：" + ", ".join(unsupported_fields)
                )

            # patch 只写调用方明确拥有的字段；Settings 根对象保持原身份，其他领域
            # 在本事务拿锁前已经提交的值会原样保留。
            settings_apply_started = True
            for field_name, value in field_updates.items():
                setattr(self.settings, field_name, deepcopy(value))

            next_revision = previous_settings_revision + 1
            snapshot = self.hub.current_settings_payload(
                settings_revision=next_revision
            )
            persistence_started = True
            self.hub.user_settings_store.save(snapshot)
            # 磁盘与内存 revision 对齐就是提交点。广播可能已经送达部分连接，
            # 此后绝不能回退版本，否则不同客户端会永久持有互相矛盾的事实。
            self.hub.settings_revision = next_revision
            durable_commit_completed = True
            try:
                await self.hub.publisher("settings").publish(
                    {"type": "settings.changed", "payload": snapshot}
                )
            except asyncio.CancelledError:
                # 请求取消不会撤销已经落盘的提交；客户端重试时会由 revision 冲突
                # 引导重新读取权威快照。
                raise
            except Exception:
                # 控制连接重连时会收到当前完整快照。常规投递故障只记诊断，
                # 不能把一个可能已经部分送达的 revision 回滚。
                logger.exception("用户设置已提交，但 settings.changed 广播失败")
            return snapshot
        except BaseException:
            if durable_commit_completed:
                raise
            if settings_apply_started:
                for field_name in field_updates:
                    setattr(
                        self.settings,
                        field_name,
                        deepcopy(getattr(previous_settings, field_name)),
                    )
            self.hub.settings_revision = previous_settings_revision

            # 先恢复运行期或资产，再重写旧快照。角色语音校准等操作会改变
            # current_settings_payload 的派生字段，顺序反过来会把失败结果写进磁盘。
            # 回滚同样可能执行硬阻塞补偿，必须与 prepare 一样离开事件循环线程。
            if prepare_started and rollback_external is not None:
                try:
                    # 取消发生在补偿期间时也必须等 worker 收口；否则回滚本身
                    # 可能在请求返回后继续写入外部状态，重新制造半提交配置。
                    await _run_sync_callback_to_completion(
                        rollback_external,
                        previous_settings,
                    )
                except Exception:
                    logger.exception("用户设置事务的外部状态回滚失败")

            if persistence_started:
                try:
                    self.hub.user_settings_store.save(
                        self.hub.current_settings_payload()
                    )
                except Exception:
                    logger.exception("用户设置事务的磁盘快照回滚失败")
            raise

    async def current_settings_payload(self) -> dict[str, object]:
        """读取已经越过持久化提交点的完整用户设置。

        Returns:
            当前已提交 revision 对应的完整公开设置快照。
        """

        async with self._configuration_mutation_lock:
            return self.hub.current_settings_payload()

    @staticmethod
    def _raise_if_settings_revision_exhausted(settings_revision: int) -> None:
        """在任何设置准备动作前拒绝已经耗尽的版本轴。

        Args:
            settings_revision: 当前已经提交并持久化的设置版本。

        Returns:
            版本仍可安全递增时无返回值。

        Raises:
            StateConflict: 当前版本已经达到 JavaScript 安全整数上限。
        """

        if settings_revision < MAX_SAFE_SETTINGS_REVISION:
            return
        raise StateConflict(
            {
                "code": "settings_revision_exhausted",
                "message": "设置版本已达到安全上限，请修复本地设置后重试",
                "actual_revision": settings_revision,
            }
        )

    async def current_shortcut_status_payload(self) -> dict[str, object]:
        """读取与已提交设置 revision 对齐的快捷键状态。

        Returns:
            Desktop 最近上报的三类快捷键注册状态。
        """

        async with self._configuration_mutation_lock:
            return self._current_shortcut_status_payload_locked()

    async def report_shortcut_status(
        self,
        *,
        items: list[ShortcutStatusItem],
        settings_revision: int | None = None,
    ) -> None:
        """保存并广播当前设置版本对应的 Desktop 快捷键状态。

        Args:
            items: Desktop 按某个设置 revision 检查得到的快捷键状态。
            settings_revision: Desktop 批量上报所依据的设置版本。

        Returns:
            无返回值；版本过期或超前的回报会被忽略。
        """

        async with self._configuration_mutation_lock:
            current_revision = self.hub.settings_revision
            # 设置提交和 Desktop 注册是两条异步链路。只接受当前版本，
            # 让迟到的旧注册结果不能覆盖新快捷键的待检测状态。
            if (
                settings_revision is not None
                and settings_revision != current_revision
            ):
                return
            status_changed = self.hub.shortcut_status_store.replace_items(
                items,
                settings_revision=current_revision,
            )
            if not status_changed:
                # 重连或重复上报可能携带同一 revision 的旧检查结果；缓存没有
                # 发生变化时无需再次广播，避免 Console 收到无意义的状态抖动。
                return
            status_payload = self._current_shortcut_status_payload_locked()
            await self.hub.publisher("settings").publish(
                {
                    "type": "settings.shortcut_status_changed",
                    "payload": status_payload,
                }
            )

    def _current_shortcut_status_payload_locked(self) -> dict[str, object]:
        """在调用方持有配置门禁时构造快捷键状态。

        Returns:
            与当前设置对象和 revision 一致的快捷键状态。
        """

        return self.hub.shortcut_status_store.current_payload(
            settings=self.settings,
            settings_revision=self.hub.settings_revision,
        )

    async def current_audio_device_selection_payload(self) -> dict[str, object]:
        """冻结已经提交的音频设备选择。

        Returns:
            音频设备枚举和应用结果需要使用的稳定选择快照。
        """

        async with self._configuration_mutation_lock:
            return audio_device_selection_payload(self.settings)

    async def current_background_agent_config_payload(self) -> dict[str, object]:
        """返回不泄露 Claude Code 凭据的后台智能体 Console 配置。

        Returns:
            非密钥设置、当前凭据类型、凭据配置状态，以及与本次读取同一提交
            门禁下冻结的完整公开设置快照。
        """

        async with self._configuration_mutation_lock:
            return self._current_background_agent_config_payload_locked()

    def _current_background_agent_config_payload_locked(self) -> dict[str, object]:
        """在调用方持有配置门禁时构造后台工作安全配置。

        Returns:
            非密钥设置、当前凭据类型、凭据配置状态，以及权威设置快照。
        """

        credentials = self.hub.bootstrap_secret_store.background_agent_credentials()
        auth_token_configured = bool(credentials["ANTHROPIC_AUTH_TOKEN"])
        api_key_configured = bool(credentials["ANTHROPIC_API_KEY"])
        return {
            **self.settings.background_agent.model_dump(mode="json", exclude={"skills"}),
            # 后台配置提交也会推进共享 settings_revision。返回提交点冻结的完整
            # 快照，Console 才能同时更新版本和其它并发修改过的普通字段。
            "settings": self.hub.current_settings_payload(),
            "credential_type": "auth_token" if auth_token_configured else "api_key",
            "credential_configured": auth_token_configured or api_key_configured,
        }

    async def current_skill_library_payload(self) -> dict[str, object]:
        """返回受管 Skill 列表和当前依赖状态。

        Returns:
            Skill 列表、汇总、清单问题和乐观锁版本。

        Raises:
            RuntimeError: 应用组合根没有装配 Skill Registry 时抛出。
        """

        registry = self._require_skill_registry()
        # 只在设置事务完整提交或回滚后读取偏好，不能把持久化提交点前的临时值
        # 暴露给 Console。依赖检查不占用提交锁，后续并发修改由 revision 仲裁。
        async with self._configuration_mutation_lock:
            preferences = dict(self.settings.background_agent.skills)
        snapshot = await asyncio.to_thread(registry.status_snapshot, preferences)
        return snapshot.to_payload()

    async def current_skill_detail_payload(self, skill_id: str) -> dict[str, object]:
        """返回一项受管 Skill 的安全详情。

        Args:
            skill_id: Skill 清单声明的稳定编号。

        Returns:
            Skill 状态、说明原文、相对文件列表和设置版本。

        Raises:
            RuntimeError: 应用组合根没有装配 Skill Registry 时抛出。
            SkillNotFoundError: Skill 不存在或清单无效时抛出。
        """

        registry = self._require_skill_registry()
        async with self._configuration_mutation_lock:
            preferences = dict(self.settings.background_agent.skills)
        return await asyncio.to_thread(registry.detail, skill_id, preferences)

    async def update_skill_enabled(
        self,
        *,
        skill_id: str,
        enabled: bool,
        expected_revision: int,
    ) -> dict[str, object]:
        """原子保存一项受管 Skill 对未来后台工作的启用偏好。

        Args:
            skill_id: Skill 清单声明的稳定编号。
            enabled: 用户希望保存的新开关值。
            expected_revision: Console 最近读取到的 Skill 设置版本。

        Returns:
            更新后的完整 Skill 状态和新版本。

        Raises:
            RuntimeError: 应用组合根没有装配 Skill Registry 时抛出。
            SkillNotFoundError: Skill 不存在时由 Registry 原样抛出。
            SkillRevisionConflictError: Console 版本过期时由 Registry 原样抛出。
            ValueError: enabled 不是布尔值时由 Registry 原样抛出。
        """

        registry = self._require_skill_registry()
        # 依赖探测可能执行最长两秒的外部命令。先在短锁内冻结偏好，再到线程池
        # 做无副作用预检，不能让它长期占用所有设置入口共用的提交锁。
        async with self._configuration_mutation_lock:
            self._raise_if_settings_revision_exhausted(
                self.hub.settings_revision
            )
            preference_snapshot = dict(self.settings.background_agent.skills)
        updated_preferences, updated_status, updated_revision = await asyncio.to_thread(
            registry.update_enabled,
            preferences=preference_snapshot,
            skill_id=skill_id,
            enabled=enabled,
            expected_revision=expected_revision,
        )

        # 预检期间另一项 Skill PATCH 可能已经提交。重新入锁后必须用同一 revision
        # 仲裁；普通设置和后台配置只要没有改 Skill 偏好，就可以安全继续提交。
        async with self._configuration_mutation_lock:
            current_preferences = dict(self.settings.background_agent.skills)
            actual_revision = registry.revision_for_preferences(current_preferences)
            if actual_revision != expected_revision:
                raise SkillRevisionConflictError(expected_revision, actual_revision)

            def prepare_skill_update(
                current_settings: Settings,
            ) -> Mapping[str, object]:
                """只替换技能库拥有的启用偏好。

                Args:
                    current_settings: 拿到提交锁后的最新设置。

                Returns:
                    保留后台模型配置、仅更新 Skill 偏好的字段 patch。
                """

                return {
                    "background_agent": current_settings.background_agent.model_copy(
                        update={"skills": updated_preferences},
                        deep=True,
                    )
                }

            # 依赖预检可能需要启动外部版本命令，但这时尚未修改任何设置。等预检
            # 通过后再拿跨进程锁，避免助手子进程为了读取旧配置被无谓阻塞。
            async with self.background_agent_config_commit_lock.hold_async():
                committed_snapshot = await self._commit_user_settings_locked(
                    prepare_update=prepare_skill_update,
                )
                response_payload: dict[str, object] = {
                    "skill": updated_status.to_payload(),
                    "revision": updated_revision,
                }
                if committed_snapshot is not None:
                    # Skill revision 与普通设置 revision 是两条不同的版本轴。
                    # 把统一事务提交点的完整快照一并返回，Console 即使随后
                    # 的 GET 失败，也能继续携带正确的全局版本提交其它设置。
                    response_payload["settings"] = committed_snapshot
                return response_payload

    async def update_general_settings(
        self,
        *,
        update: GeneralSettingsUpdate,
        expected_revision: int,
    ) -> dict[str, object]:
        """原子提交普通设置页字段和 Shell 投影。

        Args:
            update: 已由 HTTP schema 完成类型和格式校验的普通设置命令。
            expected_revision: Console 最近读取到的完整设置版本。

        Returns:
            已持久化并广播的完整普通设置快照。

        Raises:
            StateConflict: Console 提交的设置版本已经过期时抛出。
        """

        previous_projector: ShellProjector | None = None
        projector_replaced = False

        def prepare_general_update(
            current_settings: Settings,
        ) -> Mapping[str, object]:
            """基于最新设置准备普通字段和 Shell 投影。

            Args:
                current_settings: 拿到提交锁后的最新设置。

            Returns:
                普通设置页拥有的窄字段 patch。
            """

            nonlocal previous_projector, projector_replaced
            candidate_settings = update.build_candidate(current_settings)
            previous_projector = self.hub.projector
            self.hub.projector = ShellProjector(
                show_speech_bubbles=candidate_settings.show_speech_bubbles
            )
            projector_replaced = True
            return general_settings_field_updates(candidate_settings)

        def rollback_general_update(previous_settings: Settings) -> None:
            """提交点前失败时恢复普通设置的 Shell 投影。

            Args:
                previous_settings: 本事务开始前的完整设置副本。

            Returns:
                无返回值。
            """

            if projector_replaced and previous_projector is not None:
                self.hub.projector = previous_projector

        return await self.commit_user_settings(
            prepare_update=prepare_general_update,
            rollback_external=rollback_general_update,
            expected_revision=expected_revision,
        )

    async def update_background_agent_config(
        self,
        *,
        settings: BackgroundAgentSettings,
        credential_type: Literal["api_key", "auth_token"],
        credential_value: str,
        clear_credential: bool,
    ) -> dict[str, object]:
        """保存后台智能体非密钥设置与独立 Claude Code 凭据。

        Args:
            settings: 已由 API schema 校验的非密钥设置。
            credential_type: 当前选择的官方凭据类型。
            credential_value: 新凭据；留空表示保留同类型旧值。
            clear_credential: 是否明确清除后台智能体凭据。

        Returns:
            保存后的安全配置视图；运行中任务不受影响，新任务会重新读取文件和 SQLite。
        """

        # 一个请求会跨内存、SQLite、JSON、revision 和异步广播。整段串行后，失败
        # 请求的旧快照才不会覆盖另一项已经成功的后台智能体配置。
        async with self._configuration_mutation_lock:
            # 进程内锁先串行所有 Console 设置写入；文件锁再阻止助手子进程
            # 在 SQLite、JSON、revision 和提交点前补偿之间冻结任务配置；
            # 提交后的广播失败不会回滚已经持久化的版本。
            async with self.background_agent_config_commit_lock.hold_async():
                persisted_secrets = self.hub.bootstrap_secret_store.snapshot_service_secrets()

                def prepare_background_agent_update(
                    current_settings: Settings,
                ) -> Mapping[str, object]:
                    """保存凭据并构造后台工作字段 patch。

                    Args:
                        current_settings: 拿到提交锁后的最新设置。

                    Returns:
                        保留 Skill 偏好的后台工作设置字段。
                    """

                    self.hub.bootstrap_secret_store.save_background_agent_credential(
                        credential_type=credential_type,
                        credential_value=credential_value,
                        clear_credential=clear_credential,
                    )
                    # Claude 配置页不拥有 Skill 开关。保存模型或凭据时必须继承
                    # 技能库已提交的偏好，不能用请求模型的默认空字典清空它们。
                    return {
                        "background_agent": settings.model_copy(
                            update={
                                "skills": dict(
                                    current_settings.background_agent.skills
                                )
                            },
                            deep=True,
                        )
                    }

                def rollback_background_agent_update(
                    _previous_settings: Settings,
                ) -> None:
                    """恢复本次后台配置事务修改的独立凭据。

                    Args:
                        _previous_settings: 统一事务提供的旧设置副本；凭据恢复不需要读取它。

                    Returns:
                        无返回值。
                    """

                    self.hub.bootstrap_secret_store.restore_service_secrets(
                        persisted_secrets,
                        keys=(
                            "background_agent_api_key",
                            "background_agent_auth_token",
                        ),
                    )

                await self._commit_user_settings_locked(
                    prepare_update=prepare_background_agent_update,
                    rollback_external=rollback_background_agent_update,
                )
                return self._current_background_agent_config_payload_locked()

    def _require_skill_registry(self) -> SkillRegistry:
        """读取生产组合根装配的受管 Skill Registry。

        Returns:
            当前进程唯一的受管 Skill Registry。

        Raises:
            RuntimeError: 测试或错误组合根没有装配服务时抛出。
        """

        if self.skill_registry is None:
            raise RuntimeError("Skill Registry 尚未装配")
        return self.skill_registry
