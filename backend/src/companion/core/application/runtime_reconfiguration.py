from __future__ import annotations

import asyncio
import os
from inspect import isawaitable
from functools import partial
from collections.abc import Awaitable, Callable
from starlette.concurrency import run_in_threadpool

from companion.activities.runtime.service import ActivityService
from companion.background_agent.interaction_registry import BackgroundAgentInteractionRegistry
from companion.capabilities.weather.config import normalize_qweather_api_host
from companion.capabilities.weather.validation import (
    QWeatherValidationError,
    validate_qweather_api_key,
)
from companion.core.assistant_process_supervisor import AssistantProcessSupervisor
from companion.core.application.errors import InvalidRequest, ServiceUnavailable, StateConflict
from companion.core.application.runtime_replacement import RuntimeReplacementTransaction
from companion.core.application.runtime_initialization import (
    ASSET_PREPARATION_FAILURE_MESSAGE,
    RuntimeAssetPreparationFailed,
)
from companion.core.broadcast import BroadcastHub
from companion.core.realtime_input_runtime_owner import RealtimeInputRuntimeOwner
from companion.core.settings import Settings
from companion.core.execution_config import ExecutionConfiguration
from companion.core.tts_setup_service import TtsSetupService
from companion.observability.logging_config import get_backend_logger
from companion.llm.config import (
    LlmSettings,
    ProviderCredential,
    normalize_provider_credentials,
    validate_llm_settings_ready,
)
from companion.llm.account_balance import fetch_provider_account_statuses
from companion.llm.connectivity import (
    ProviderConnectivityError,
    check_configured_provider_connectivity,
)
from companion.realtime.assistant_process_client import AssistantProcessClient
from companion.realtime.assistant_process_handle import AssistantProcessReplacement


logger = get_backend_logger(__name__)


class RuntimeReconfigurationCoordinator:
    """协调启动密钥、LLM 配置与运行时世代热替换。

    Args:
        settings: 当前进程使用的可变设置对象。
        hub: 启动密钥持久化和控制广播中心。
        realtime_input_runtime_owner: 可构造并发布实时输入候选的所有者。
        assistant_process_supervisor: 可构造并发布助手子进程候选的所有者。
        activity_service: 负责中断旧运行时所属 Activity 的应用服务。
        background_agent_interactions: 后台智能体待答请求的主进程账本。
        interrupt_runtime_activities: 关闭旧世代提醒与 TaskResult 材料租约的同步端口。
        release_runtime_memory_compaction: 释放旧助手世代拥有的滚动压缩计划。
        terminate_self_reflection_runtime: 终止旧助手世代自我反思状态和租约的端口。
        restart_livekit_media_session: 使用当前输入 Runtime 重建活跃媒体会话的端口。
        provider_connectivity_checker: 候选 LLM 设置的厂商 Chat 连通性检查器。
        configuration_mutation_lock: 所有设置和运行时配置变更共用的进程内锁。
        resume_runtime_consumers: 运行时首次就绪后恢复准入、提醒等进程级消费者的端口。
    """

    def __init__(
        self,
        *,
        settings: Settings,
        hub: BroadcastHub,
        realtime_input_runtime_owner: RealtimeInputRuntimeOwner,
        assistant_process_supervisor: AssistantProcessSupervisor,
        activity_service: ActivityService,
        background_agent_interactions: BackgroundAgentInteractionRegistry,
        interrupt_runtime_activities: Callable[[int], list[dict[str, object]]],
        release_runtime_memory_compaction: Callable[[int], bool],
        terminate_self_reflection_runtime: Callable[[int, int], object] | None = None,
        publish_assistant_candidate: Callable[
            [
                AssistantProcessClient,
                Callable[[AssistantProcessReplacement], None],
            ],
            AssistantProcessReplacement,
        ] | None = None,
        restart_livekit_media_session: Callable[[], Awaitable[bool]],
        provider_connectivity_checker: Callable[
            [LlmSettings], Awaitable[None]
        ] = check_configured_provider_connectivity,
        configuration_mutation_lock: asyncio.Lock | None = None,
        interrupt_transition_materials: Callable[[int], list[dict[str, object]]] | None = None,
        interrupt_transition_notices: Callable[[int], object] | None = None,
        tts_setup: TtsSetupService | None = None,
        stop_voice_setup_consumers: Callable[[], Awaitable[None]] | None = None,
        resume_runtime_consumers: Callable[[], None] | None = None,
        publish_application_status: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        """创建负责运行时配置替换的协调器。

        Args:
            settings: 当前进程使用的可变设置对象。
            hub: 启动密钥持久化和控制广播中心。
            realtime_input_runtime_owner: 可构造并发布实时输入候选的所有者。
            assistant_process_supervisor: 可构造并发布助手子进程候选的所有者。
            activity_service: 负责中断旧运行时所属 Activity 的应用服务。
            background_agent_interactions: 后台智能体待答请求的主进程账本。
            interrupt_runtime_activities: 关闭旧世代提醒与材料租约的同步端口。
            release_runtime_memory_compaction: 释放旧助手世代拥有的滚动压缩计划。
            terminate_self_reflection_runtime: 终止旧助手世代自我反思状态和租约的端口。
            restart_livekit_media_session: 使用当前输入 Runtime 重建活跃媒体会话的端口。
            provider_connectivity_checker: 候选 LLM 设置的厂商 Chat 连通性检查器。
            configuration_mutation_lock: 所有设置和运行时配置变更共用的进程内锁。
            resume_runtime_consumers: 运行时首次就绪后恢复准入、提醒等进程级消费者的端口。
        """

        self.settings = settings
        self.interrupt_transition_materials = interrupt_transition_materials or interrupt_runtime_activities
        self.interrupt_transition_notices = interrupt_transition_notices
        self.hub = hub
        # 组合根之外的窄测试替身可能还没有该字段；正式 BroadcastHub 会在构造时
        # 初始化它。这里补齐一次，保证所有配置入口共享同一条代际轴。
        if not hasattr(self.hub, "runtime_configuration_revision"):
            self.hub.runtime_configuration_revision = 0
        self.realtime_input_runtime_owner = realtime_input_runtime_owner
        self.assistant_process_supervisor = assistant_process_supervisor
        self.activity_service = activity_service
        self.background_agent_interactions = background_agent_interactions
        self.interrupt_runtime_activities = interrupt_runtime_activities
        self.release_runtime_memory_compaction = release_runtime_memory_compaction
        self.terminate_self_reflection_runtime = terminate_self_reflection_runtime
        self.publish_assistant_candidate = publish_assistant_candidate
        self.restart_livekit_media_session = restart_livekit_media_session
        self.provider_connectivity_checker = provider_connectivity_checker
        self._configuration_mutation_lock = configuration_mutation_lock or asyncio.Lock()
        self.tts_setup = tts_setup
        self.stop_voice_setup_consumers = stop_voice_setup_consumers
        self.resume_runtime_consumers = resume_runtime_consumers
        self.publish_application_status = publish_application_status
        self._voice_cleanup_steps: dict[str, Callable] = {}
        self.runtime_initialization = None
        self._configuration_initialization_token = None

    def _resume_runtime_consumers_if_initialized(
        self,
        status: dict[str, object],
    ) -> None:
        """运行时刚刚就绪时恢复准入和提醒等进程级消费者。

        Args:
            status: 已提交配置对应的 Bootstrap 状态。

        Returns:
            无；状态未就绪或没有注入恢复端口时直接返回。
        """

        if status.get("is_initialized") is not True:
            return
        if self.resume_runtime_consumers is not None:
            self.resume_runtime_consumers()

    async def _publish_application_status(self) -> None:
        """状态广播失败时只记录诊断，不回滚已经提交的配置事务。"""

        if self.publish_application_status is None:
            return
        try:
            await self.publish_application_status()
        except Exception:
            logger.warning("应用状态事件广播失败")

    async def current_bootstrap_status_payload(self) -> dict[str, object]:
        """立即读取首次初始化的安全状态，模型加载期间也能报告阻断事实。

        Returns:
            不包含明文密钥的 Bootstrap 状态。
        """

        # 安全投影与运行时门禁在事件循环中同步读取；等待配置锁会把长加载误报为服务断线。
        return self.hub.bootstrap.status_payload()

    async def current_llm_config_payload(self) -> dict[str, object]:
        """返回控制台可展示的 LLM 配置安全视图。

        Returns:
            不包含明文 API Key 的 Provider、模型分配和就绪状态。
        """

        async with self._configuration_mutation_lock:
            return self._current_llm_config_payload_locked()

    async def current_llm_account_status_payload(self) -> dict[str, object]:
        """查询当前 LLM 厂商账户余额的脱敏快照。

        Returns:
            不包含 API Key 和厂商原始响应的余额状态。

        余额查询会在配置锁外执行，避免厂商网络延迟阻塞配置保存；调用前先
        复制一个完整的 LLM 设置快照，确保一次查询只对应一份配置。
        """

        async with self._configuration_mutation_lock:
            settings_snapshot = self.settings.llm.model_copy(deep=True)
        return await run_in_threadpool(fetch_provider_account_statuses, settings_snapshot)

    def _current_llm_config_payload_locked(self) -> dict[str, object]:
        """在调用方持有配置门禁时构造 LLM 安全视图。

        Returns:
            不包含明文 API Key 的 Provider、模型分配和就绪状态。
        """

        llm_settings = self.settings.llm
        return {
            "schema_version": llm_settings.schema_version,
            "credentials": {
                provider_id: {
                    "base_url": credential.base_url,
                    "api_key_configured": bool(credential.api_key.strip()),
                }
                for provider_id, credential in llm_settings.credentials.items()
            },
            "role_bindings": dict(llm_settings.role_bindings),
            "status": validate_llm_settings_ready(llm_settings).to_payload(),
        }

    async def initialize_bootstrap(
        self,
        *,
        qweather_api_key: str | None,
        qweather_api_host: str | None,
    ) -> dict[str, object]:
        """验证并提交首次启动密钥，同时原子替换自有运行时。

        Args:
            qweather_api_key: 可选的和风天气密钥。
            qweather_api_host: 可选的和风天气 API 地址。

        Returns:
            已提交的 Bootstrap 状态。

        Raises:
            InvalidRequest: 天气密钥校验失败时抛出。
            StateConflict: LLM 配置尚未就绪，或联网校验期间天气配置已经变化时抛出。
        """

        async with self._configuration_mutation_lock:
            if not validate_llm_settings_ready(self.settings.llm).is_ready:
                raise StateConflict("llm config is not ready")

            try:
                secrets = self.hub.bootstrap.normalize_secrets(
                    qweather_api_key=qweather_api_key,
                    qweather_api_host=qweather_api_host,
                )
            except ValueError as exception:
                # 与 update_qweather_api_key 对齐：密钥格式错误是请求问题，
                # 必须映射为 400 而不是 500。
                raise InvalidRequest(str(exception)) from exception

            # 联网校验会在锁外执行，先记住天气配置的基线；提交前必须确认
            # 这段时间没有别的请求成功写入天气密钥，否则旧校验结果不能覆盖新值。
            qweather_snapshot = self._capture_runtime_configuration_revision()

        if secrets.qweather_api_key is not None:
            try:
                await run_in_threadpool(
                    validate_qweather_api_key,
                    settings=self.settings,
                    api_key=secrets.qweather_api_key,
                    api_host=secrets.qweather_api_host or "",
                )
            except QWeatherValidationError as exception:
                raise self._map_qweather_validation_error(exception) from exception
            except ValueError as exception:
                raise InvalidRequest(str(exception)) from exception

        async with self._configuration_mutation_lock:
            if (
                secrets.qweather_api_key is not None
                or secrets.qweather_api_host is not None
            ):
                self._require_runtime_configuration_revision(
                    qweather_snapshot,
                    "和风天气配置已变化，请重新读取后保存",
                )
            previous_qweather_api_key = self.settings.qweather_api_key
            previous_qweather_api_host = self.settings.qweather_api_host
            persisted_secrets = self.hub.bootstrap.secret_store.snapshot_service_secrets()
            replacement_transaction = None
            if secrets.qweather_api_key is not None:
                self.settings.qweather_api_key = secrets.qweather_api_key
            if secrets.qweather_api_host is not None:
                self.settings.qweather_api_host = secrets.qweather_api_host
            try:
                replacement_transaction = await self._prepare_runtime_replacement()
                status = self.hub.bootstrap.initialize(
                    qweather_api_key=secrets.qweather_api_key,
                    qweather_api_host=secrets.qweather_api_host,
                )
                if replacement_transaction is not None:
                    await self._commit_configured_runtime(replacement_transaction)
                    status = self.hub.bootstrap.status_payload()
                self._advance_runtime_configuration_revision()
            except BaseException:
                # 新 Runtime 一旦越过发布提交点就不能再恢复旧配置；取消或诊断异常只向上游传播。
                if (
                    replacement_transaction is not None
                    and replacement_transaction.state == "committed"
                ):
                    raise
                if replacement_transaction is not None:
                    await run_in_threadpool(replacement_transaction.rollback)
                self.settings.qweather_api_key = previous_qweather_api_key
                self.settings.qweather_api_host = previous_qweather_api_host
                self.hub.bootstrap.secret_store.restore_service_secrets(
                    persisted_secrets,
                )
                await self._fail_prepared_configuration_initialization()
                raise

            await self._publish_application_status()
            # 首次初始化可能发生在进程启动很久之后，lifecycle 的消费者启动点早已错过；
            # 系统在此刻就绪时必须恢复准入和提醒，否则定时提醒会静默失效到下一次授权 reconcile。
            self._resume_runtime_consumers_if_initialized(status)
            return status

    async def update_qweather_api_key(self, *, api_key: str, api_host: str) -> dict[str, object]:
        """验证并保存和风天气密钥，然后刷新助手运行时。

        Args:
            api_key: 用户提交的和风天气密钥。
            api_host: 用户提交的和风天气 API 地址。

        Returns:
            已提交的 Bootstrap 状态。

        Raises:
            InvalidRequest: 天气密钥或地址校验失败时抛出。
            StateConflict: 联网校验期间天气配置已经被其他请求提交时抛出。
        """

        async with self._configuration_mutation_lock:
            # 只短暂持锁取快照，不能把全局配置门禁一直占用到网络请求结束。
            qweather_snapshot = self._capture_runtime_configuration_revision()

        try:
            await run_in_threadpool(
                validate_qweather_api_key,
                settings=self.settings,
                api_key=api_key,
                api_host=api_host,
            )
        except QWeatherValidationError as exception:
            raise self._map_qweather_validation_error(exception) from exception
        except ValueError as exception:
            raise InvalidRequest(str(exception)) from exception

        async with self._configuration_mutation_lock:
            self._require_runtime_configuration_revision(
                qweather_snapshot,
                "和风天气配置已变化，请重新读取后保存",
            )
            previous_qweather_api_key = self.settings.qweather_api_key
            previous_qweather_api_host = self.settings.qweather_api_host
            persisted_secrets = self.hub.bootstrap.secret_store.snapshot_service_secrets()
            replacement_transaction = None
            self.settings.qweather_api_key = api_key.strip()
            self.settings.qweather_api_host = normalize_qweather_api_host(api_host)
            try:
                replacement_transaction = await self._prepare_runtime_replacement(
                    include_realtime_input=False,
                )
                status = self.hub.bootstrap.save_qweather_api_key(
                    api_key=api_key,
                    api_host=api_host,
                )
                if replacement_transaction is not None:
                    await self._commit_configured_runtime(replacement_transaction)
                    status = self.hub.bootstrap.status_payload()
                self._advance_runtime_configuration_revision()
            except BaseException:
                # 防止提交后的请求取消把 Settings 与 SQLite 恢复成旧值，造成运行时和配置分裂。
                if (
                    replacement_transaction is not None
                    and replacement_transaction.state == "committed"
                ):
                    raise
                if replacement_transaction is not None:
                    await run_in_threadpool(replacement_transaction.rollback)
                self.settings.qweather_api_key = previous_qweather_api_key
                self.settings.qweather_api_host = previous_qweather_api_host
                self.hub.bootstrap.secret_store.restore_service_secrets(
                    persisted_secrets,
                    keys=("qweather_api_key", "qweather_api_host"),
                )
                await self._fail_prepared_configuration_initialization()
                raise
            await self._publish_application_status()
            return status

    @staticmethod
    def _map_qweather_validation_error(
        exception: QWeatherValidationError,
    ) -> InvalidRequest | ServiceUnavailable:
        """把天气联网验证结果映射成正确的控制面错误类别。

        Args:
            exception: 验证层保留的稳定原因和可重试标志。

        Returns:
            用户配置错误对应的 ``InvalidRequest``，或外部服务/网络故障对应的
            ``ServiceUnavailable``。两者都只携带脱敏的稳定详情。
        """

        # 上游返回了无法解析的响应时也不能让用户修改配置来“修复”；它属于
        # 外部依赖故障，和连接超时一样应保留重试语义。
        external_failure = exception.retryable or exception.reason == "invalid_response"
        safe_message = exception.safe_message
        validation_prefix = "和风天气 API Key 无法通过验证："
        if external_failure and safe_message.startswith(validation_prefix):
            safe_message = safe_message[len(validation_prefix):]

        if external_failure:
            return ServiceUnavailable(
                {
                    "code": f"qweather_{exception.reason}",
                    "message": f"和风天气验证暂时失败，密钥尚未保存：{safe_message}",
                    "retryable": True,
                }
            )

        return InvalidRequest(
            {
                "code": f"qweather_{exception.reason}",
                "message": safe_message,
                "retryable": False,
            }
        )

    def _capture_runtime_configuration_revision(self) -> int:
        """记录联网校验开始时的统一运行时配置代际。"""

        return self.hub.runtime_configuration_revision

    def _advance_runtime_configuration_revision(self) -> None:
        """在配置事务越过持久化和运行时发布提交点后推进代际。"""

        self.hub.runtime_configuration_revision += 1

    def _require_runtime_configuration_revision(
        self,
        expected_revision: int,
        conflict_message: str = "运行时配置已变化，请重新读取后保存",
    ) -> None:
        """拒绝基于过期配置代际的联网校验结果。

        Args:
            expected_revision: 校验开始时捕获的运行时配置代际。

        Raises:
            StateConflict: 校验期间已有其它配置事务提交，即使值后来又恢复原样。
        """

        if self._capture_runtime_configuration_revision() != expected_revision:
            raise StateConflict(conflict_message)

    async def update_llm_config(
        self,
        *,
        credentials: dict[str, ProviderCredential],
        role_bindings: dict[str, str | None],
    ) -> dict[str, object]:
        """验证并提交 LLM 配置，然后原子替换依赖该配置的运行时。

        Args:
            credentials: 按 Provider 保存的端点和 API Key。
            role_bindings: 每个 LLM 职责绑定的模型接入规格编号。

        Returns:
            已提交的 LLM 安全配置视图。

        Raises:
            InvalidRequest: 凭据、模型能力或连通性校验失败时抛出。
        """

        async with self._configuration_mutation_lock:
            normalized_credentials = {}
            for provider_id, credential in credentials.items():
                previous_credential = self.settings.llm.credentials.get(provider_id)
                normalized_credentials[provider_id] = credential.model_copy(
                    update={
                        "api_key": credential.api_key.strip()
                        or (previous_credential.api_key if previous_credential is not None else "")
                    }
                )
            try:
                runtime_credentials = normalize_provider_credentials(
                    normalized_credentials
                )
                candidate_settings = LlmSettings(
                    schema_version=2,
                    credentials=runtime_credentials,
                    role_bindings=role_bindings,
                )
            except ValueError as exception:
                raise InvalidRequest(str(exception)) from exception

            validation = validate_llm_settings_ready(candidate_settings)
            if not validation.is_ready:
                raise InvalidRequest(validation.to_payload())
            previous_llm_settings = self.settings.llm.model_copy(deep=True)
            llm_snapshot = self._capture_runtime_configuration_revision()

        # 联网验证不能阻塞到期收口；返回后确认没有其他设置请求改变候选的基础。
        try:
            await self.provider_connectivity_checker(candidate_settings)
        except ProviderConnectivityError as exception:
            raise InvalidRequest(str(exception)) from exception

        async with self._configuration_mutation_lock:
            self._require_runtime_configuration_revision(
                llm_snapshot,
                "LLM 配置已变化，请重新读取后保存",
            )
            persisted_secrets = self.hub.bootstrap.secret_store.snapshot_service_secrets()
            replacement_transaction = None
            self.settings.llm = candidate_settings
            try:
                replacement_transaction = await self._prepare_runtime_replacement()
                self.hub.bootstrap.save_llm_settings(candidate_settings)
                if replacement_transaction is not None:
                    await self._commit_configured_runtime(replacement_transaction)
                self._advance_runtime_configuration_revision()
            except BaseException:
                # 助手世代发布后配置已经生效；此后的异常不得伪装成可回滚的提交失败。
                if (
                    replacement_transaction is not None
                    and replacement_transaction.state == "committed"
                ):
                    raise
                if replacement_transaction is not None:
                    await run_in_threadpool(replacement_transaction.rollback)
                self.settings.llm = previous_llm_settings
                self.hub.bootstrap.secret_store.restore_service_secrets(
                    persisted_secrets,
                    keys=("llm_config_json",),
                )
                await self._fail_prepared_configuration_initialization()
                raise

            await self._publish_application_status()
            self._resume_runtime_consumers_if_initialized(
                self.hub.bootstrap.status_payload()
            )
            return self._current_llm_config_payload_locked()

    async def restore_runtime(
        self, *, report_phase: Callable[[str], Awaitable[None]] | None = None,
    ) -> bool:
        """清理后重建已配置运行时，加载期间仍让状态请求及时返回。

        Args:
            report_phase: 真实阶段开始时检查操作世代并发布状态的端口。
        Returns:
            已配置资源可用时为真；必需 LLM 或安装态语音尚未配置时为假。
        """
        async with self._configuration_mutation_lock:
            if not validate_llm_settings_ready(self.settings.llm).is_ready:
                # 源码资源与厂商凭据无关；首配事务回滚后，初始化重试仍须能继续下载。
                await self._prepare_input_runtime_assets()
                return False
            bootstrap = await run_in_threadpool(self.hub.bootstrap.status_payload)
            if not bootstrap["voice"]["is_ready"] and os.environ.get("NEARMATE_PACKAGED") == "1":
                return False
            input_already_loaded = self.realtime_input_runtime_owner.runtime is not None
            if input_already_loaded and (self.assistant_process_supervisor.is_ready
                                         or not self.assistant_process_supervisor.output_enabled):
                return True
            if report_phase is not None:
                await report_phase("assistant" if input_already_loaded else "input_models")
            # ONNX、唤醒和声纹模型构造是同步工作，留在主事件循环会让遮罩进度也冻结。
            transaction = await self._load_runtime_replacement(include_realtime_input=not input_already_loaded)
            try:
                await transaction.commit(report_phase=report_phase)
            except BaseException:
                if transaction.state != "committed":
                    await run_in_threadpool(transaction.rollback)
                raise
            await self._publish_application_status()
            return True

    async def _prepare_runtime_replacement(
        self,
        *,
        include_realtime_input: bool = True,
    ) -> RuntimeReplacementTransaction | None:
        """构造并接管本次配置变更所需的运行时候选。

        Args:
            include_realtime_input: 是否同时替换主进程实时输入运行时。

        Returns:
            持有候选资源且尚未提交的替换事务。
        """

        # 配置准备开始时冻结世代，避免迟到候选覆盖后来的初始化。
        observer = self.runtime_initialization
        generation = observer.initialization_generation if observer is not None else None
        # 安装态保存 LLM 是语音配置之前的一步，尚无音色时只持久化设置。
        bootstrap = await run_in_threadpool(self.hub.bootstrap.status_payload)
        if not bootstrap["voice"]["is_ready"] and os.environ.get("NEARMATE_PACKAGED") == "1":
            return None
        if observer is not None:
            self._configuration_initialization_token = await observer.begin_configuration_initialization(
                include_realtime_input=include_realtime_input, expected_generation=generation)
        try:
            # 配置事务仍持有变更锁；只有同步模型加载离开事件循环，避免控制面被模型构造冻结。
            return await self._load_runtime_replacement(include_realtime_input=include_realtime_input)
        except BaseException as error:
            token, self._configuration_initialization_token = self._configuration_initialization_token, None
            if observer is not None and token is not None:
                await observer.finish_configuration_initialization(token, success=False,
                    failure_message=(ASSET_PREPARATION_FAILURE_MESSAGE
                        if isinstance(error, RuntimeAssetPreparationFailed) else None))
            raise

    async def _prepare_input_runtime_assets(self) -> None:
        """通过真实 Owner 准备资源，首次配置和凭据未齐备的启动共用同一取消边界。"""
        owner = getattr(self, "realtime_input_runtime_owner", None)
        prepare = getattr(owner, "prepare_source_assets", None)
        # 只有真实 Owner 声明资源准备能力；外部 Runtime 和轻量测试替身不触发下载。
        if not callable(prepare):
            return
        observer = getattr(self, "runtime_initialization", None)
        if observer is not None:
            await observer.prepare_input_runtime_assets(prepare)
        else:
            await run_in_threadpool(prepare)

    async def _load_runtime_replacement(self, *, include_realtime_input: bool) -> RuntimeReplacementTransaction:
        """请求取消时仍等同步加载交回候选，再关闭资源，不能把线程留成无主任务。"""
        if include_realtime_input:
            await self._prepare_input_runtime_assets()
        loading = asyncio.create_task(run_in_threadpool(self._build_runtime_replacement,
            include_realtime_input=include_realtime_input), name="nearmate-runtime-candidate-loading")
        try:
            return await asyncio.shield(loading)
        except asyncio.CancelledError:
            # Python 无法安全杀掉模型加载线程；先收回它构造的候选，再传递取消。
            async def finish_worker(task: asyncio.Task):
                """重复取消也只影响请求，不能把仍持有资源的工作任务取消。"""
                while True:
                    try:
                        return await asyncio.shield(task)
                    except asyncio.CancelledError:
                        if task.cancelled():
                            raise

            try:
                transaction = await finish_worker(loading)
            except Exception:
                raise asyncio.CancelledError from None
            cleanup = asyncio.create_task(run_in_threadpool(transaction.rollback),
                                          name="nearmate-cancelled-candidate-cleanup")
            await finish_worker(cleanup)
            raise

    async def _commit_configured_runtime(self, transaction: RuntimeReplacementTransaction) -> None:
        """配置候选提交时报告真实阶段，并在 readiness 后结算唯一初始化状态。"""
        observer = self.runtime_initialization
        token = self._configuration_initialization_token
        try:
            if observer is not None and token is not None:
                await transaction.commit(report_phase=partial(observer.report_initialization_phase, token))
            else:
                await transaction.commit()
        except BaseException:
            if observer is not None and token is not None:
                await observer.finish_configuration_initialization(token, success=transaction.state == "committed")
            raise
        else:
            if observer is not None and token is not None:
                await observer.finish_configuration_initialization(token, success=True)
        finally:
            self._configuration_initialization_token = None

    async def _fail_prepared_configuration_initialization(self) -> None:
        """候选尚未提交而持久化失败时，也必须关闭初始化状态和操作身份。"""
        token, self._configuration_initialization_token = self._configuration_initialization_token, None
        if token is not None and self.runtime_initialization is not None:
            await self.runtime_initialization.finish_configuration_initialization(token, success=False)

    async def configure_voice_execution(self, configuration: ExecutionConfiguration) -> dict:
        """保存用户确认的执行方式，并关闭依赖旧配置的正式运行时。

        Args:
            configuration: 已经通过设备探测的执行选择与模型目录。
        Returns:
            持久保存的执行配置。
        """

        async with self._configuration_mutation_lock:
            self._require_voice_setup_prerequisites()
            result = await run_in_threadpool(self.tts_setup.configure, configuration)
            if not self.tts_setup.status_payload()["is_ready"]:
                await self._retire_for_voice_setup()
            await self._publish_application_status()
            return result

    async def start_voice_setup(self, *, model_id: str | None = None, retry_task_id: str | None = None) -> dict:
        """先收口正式资源，再启动唯一的语音准备任务。

        Args:
            model_id: 新任务选择的模型，与重试编号二选一。
            retry_task_id: 要重试的已失败任务编号。
        Returns:
            新配置任务的结构化状态。
        """

        async with self._configuration_mutation_lock:
            self._require_voice_setup_prerequisites()
            if (model_id is None) == (retry_task_id is None):
                raise InvalidRequest("必须提供模型或重试任务之一")
            if retry_task_id is not None:
                status = self.tts_setup.status_payload()
                if status["task_id"] != retry_task_id or not status["can_retry"]:
                    raise StateConflict("语音任务不可重试或已过期")
                model_id = str(status["model_id"])
            catalog = await run_in_threadpool(self.tts_setup.catalog_payload)
            if model_id not in {item["model_id"] for item in catalog["models"] if item["download_available"]}:
                raise InvalidRequest("模型不在已固定版本的发行目录中")
            await run_in_threadpool(self.tts_setup.invalidate)
            await self._retire_for_voice_setup()
            result = await run_in_threadpool(self.tts_setup.start, model_id)
            await self._publish_application_status()
            return result

    async def start_voice_model_download(self, *, model_id: str | None = None, retry_task_id: str | None = None) -> dict:
        """串行提交纯文件下载，保留当前语音门禁和正式运行时。

        Args:
            model_id: 新下载模型编号，与重试任务编号二选一。
            retry_task_id: 需要继续下载的失败任务编号。
        Returns:
            独立的模型下载任务状态。
        """

        async with self._configuration_mutation_lock:

            if (model_id is None) == (retry_task_id is None):
                raise InvalidRequest('必须提供模型或重试任务之一')
            if self.tts_setup is None:
                raise StateConflict('语音配置服务尚未建立')
            # 与语音切换共用提交锁，防止下载恰好插入“旧助手已关闭、新配置尚未启动”的窗口。
            if retry_task_id is not None:
                return await run_in_threadpool(self.tts_setup.retry_model_download, retry_task_id)
            return await run_in_threadpool(self.tts_setup.start_model_download, model_id)

    async def confirm_voice_setup(self, task_id: str) -> dict:
        """提交当前试听后启动正式助手，失败时保留可修复的初始化状态。

        Args:
            task_id: 用户实际试听并确认的任务。
        Returns:
            包含 LLM 和语音门禁的完整首次初始化状态。
        """

        async with self._configuration_mutation_lock:
            self._require_voice_setup_prerequisites()
            if self._voice_cleanup_steps:
                await self._retire_for_voice_setup()
            await run_in_threadpool(self.tts_setup.confirm, task_id)
            transaction = None
            try:
                transaction = await self._prepare_runtime_replacement()
                if transaction is not None:
                    await self._commit_configured_runtime(transaction)
                self._resume_runtime_consumers_if_initialized(
                    self.hub.bootstrap.status_payload()
                )
            except BaseException:
                if transaction is not None and transaction.state != "committed":
                    await run_in_threadpool(transaction.rollback)
                self.tts_setup.invalidate()
                await self._fail_prepared_configuration_initialization()
                raise
            await self._publish_application_status()
            return self.hub.bootstrap.status_payload()

    def _require_voice_setup_prerequisites(self) -> None:
        """语音步骤必须排在必需 LLM 配置之后。"""

        if self.tts_setup is None or not validate_llm_settings_ready(self.settings.llm).is_ready:
            raise StateConflict("请先完成必需 LLM 职责配置")

    async def _retire_for_voice_setup(self) -> None:
        """保留失败的关闭步骤供重试，任一资源未释放都不能加载新模型。"""

        if not self._voice_cleanup_steps:
            self._voice_cleanup_steps.update(self.runtime_cleanup_steps())
            if self.stop_voice_setup_consumers is not None:
                self._voice_cleanup_steps["consumers"] = self.stop_voice_setup_consumers
            self._voice_cleanup_steps["assistant"] = lambda: run_in_threadpool(self.assistant_process_supervisor.shutdown)
            self._voice_cleanup_steps["input"] = lambda: run_in_threadpool(self.realtime_input_runtime_owner.shutdown)
        for name, close in tuple(self._voice_cleanup_steps.items()):
            result = close()
            if isawaitable(result):
                await result
            del self._voice_cleanup_steps[name]

    def runtime_cleanup_steps(self) -> dict[str, Callable]:
        """冻结旧世代并逐项回收领域状态，失败项目可独立重试。

        Returns:
            按资源名称登记的幂等关闭端口；没有旧助手时为空。
        """
        replacement = self.assistant_process_supervisor.detach_for_shutdown()
        if replacement is None or replacement.old_epoch is None:
            return {}
        old_epoch = replacement.old_epoch

        async def release_events(port):
            events = port(old_epoch)
            for event in events:
                # 控制客户端可能已断开；领域账本已收口，广播失败不回滚它。
                try:
                    await asyncio.wait_for(self.hub.publisher("materials").publish_ephemeral(event), timeout=1)
                except Exception:
                    pass

        steps = {
            "activities": lambda: self.activity_service.interrupt_active(old_epoch, "runtime_interrupted"),
            "compaction": lambda: self.release_runtime_memory_compaction(old_epoch),
            "materials": lambda: release_events(self.interrupt_transition_materials),
            "interactions": lambda: release_events(self.background_agent_interactions.invalidate_runtime_epoch),
        }
        if self.terminate_self_reflection_runtime is not None:
            steps["self_reflection"] = lambda: self.terminate_self_reflection_runtime(old_epoch, replacement.current_epoch)
        if self.interrupt_transition_notices is not None:
            steps["notices"] = lambda: self.interrupt_transition_notices(old_epoch)
        return steps

    def _build_runtime_replacement(self, *, include_realtime_input: bool) -> RuntimeReplacementTransaction:
        """在调用方持有配置锁时构建资源候选。"""
        return RuntimeReplacementTransaction.prepare(
            realtime_input_runtime_owner=self.realtime_input_runtime_owner,
            assistant_process_supervisor=self.assistant_process_supervisor,
            activity_service=self.activity_service,
            interrupt_background_agent_interactions=(
                self.background_agent_interactions.invalidate_runtime_epoch
            ),
            interrupt_runtime_activities=self.interrupt_runtime_activities,
            release_runtime_memory_compaction=self.release_runtime_memory_compaction,
            terminate_self_reflection_runtime=self.terminate_self_reflection_runtime,
            publish_assistant_candidate=self.publish_assistant_candidate,
            broadcast_ephemeral=self.hub.publisher("materials").publish_ephemeral,
            restart_livekit_media_session=self.restart_livekit_media_session,
            include_realtime_input=include_realtime_input,
        )
