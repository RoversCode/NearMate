from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from companion.core.application.assistant_request_context_preparer import (
    AssistantRequestContextPreparer,
)
from companion.core.application.material_lifecycle import MaterialLifecycleService
from companion.core.broadcast import BroadcastHub
from companion.core.realtime_input_runtime_owner import RealtimeInputRuntimeOwner
from companion.observability.logging_config import get_backend_logger
from companion.foreground_self_reflection.models import (
    DivertFinalInputCommand,
    InputDiversionResult,
    UnattendedMaterialReference,
)
from companion.foreground_self_reflection.policy import (
    input_diversion_blocks_normal_request,
    input_diversion_was_accepted,
)
from companion.foreground_self_reflection.contracts import (
    ForegroundAttentionAdmissionPort,
)
from companion.materials.models import MaterialAttachment
from companion.realtime.assistant_events import (
    AssistantRequest,
    CancelSupersededSystemNoticeCommand,
)
from companion.realtime.assistant_process_bridge import AssistantOutputUnavailableError
from companion.realtime.assistant_process_client import AssistantProcessClient
from companion.realtime.livekit_media_session import LiveKitMediaSessionController
from companion.realtime.realtime_input_runtime import RealtimeInputRuntime


logger = get_backend_logger(__name__)


@dataclass(frozen=True, slots=True)
class TextTurnDispatch:
    """保存已经越过 Turn 准入屏障的一条文本输入。

    Args:
        submission_id: Desktop 在用户点击发送时生成的稳定提交编号。
        request_id: Backend 在跨异步边界前确定的助手请求编号。
        message_text: 当前允许派发的文本正文。
        turn_material_context_id: reservation 阶段冻结的本轮材料上下文编号。
        environment_snapshot_id: 两阶段准备完成后绑定的现场环境快照编号。
        attachments: 在文本越过顺序屏障时冻结的完整材料附件。
        materials: 供未处理插话片段保留的安全材料引用；普通文本为空时不影响原流程。
    """

    submission_id: str
    request_id: str
    message_text: str
    turn_material_context_id: str
    environment_snapshot_id: str | None
    attachments: tuple[MaterialAttachment, ...]
    materials: tuple[UnattendedMaterialReference, ...] = ()


@dataclass(frozen=True, slots=True)
class TurnSubmissionOutcome:
    """保存一条已正式公开用户消息的助手启动结果。

    Args:
        request_id: 本次文本输入对应的助手请求编号。
        assistant_request_started: 请求是否已经进入 Bridge 或 Assistant runtime。
        diverted_to_episode: Attention active 时是否已经进入未处理插话片段。
    """

    request_id: str
    assistant_request_started: bool
    diverted_to_episode: bool = False


class TurnSubmissionService:
    """把已获准文本投递到统一助手生成链路。

    Args:
        hub: 控制面广播和记录中心。
        realtime_input_runtime_owner: 当前实时输入运行时所有者。
        livekit_media_session_controller: 当前 LiveKit 媒体会话控制器。
        assistant_request_context_preparer: 助手请求上下文准备器。
        material_lifecycle: 近期材料登记与材料快照 revision 编排器。
        assistant_runtime_provider: 读取当前可用助手运行时的函数。
        input_diversion_port: Attention active 时接纳已公开文本的端口。
        current_runtime_epoch: 返回当前 Assistant Runtime 世代的函数。
        now: 生成单调时间的函数。
    """

    def __init__(
        self,
        *,
        hub: BroadcastHub,
        realtime_input_runtime_owner: RealtimeInputRuntimeOwner,
        livekit_media_session_controller: LiveKitMediaSessionController,
        assistant_request_context_preparer: AssistantRequestContextPreparer,
        material_lifecycle: MaterialLifecycleService,
        assistant_runtime_provider: Callable[[], AssistantProcessClient],
        input_diversion_port: ForegroundAttentionAdmissionPort | None = None,
        current_runtime_epoch: Callable[[], int] | None = None,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        """保存文本派发所需的稳定应用依赖。

        Args:
            hub: 控制面广播和记录中心。
            realtime_input_runtime_owner: 当前实时输入运行时所有者。
            livekit_media_session_controller: 当前 LiveKit 媒体会话控制器。
            assistant_request_context_preparer: 助手请求上下文准备器。
            material_lifecycle: 近期材料登记与材料快照 revision 编排器。
            assistant_runtime_provider: 读取当前可用助手运行时的函数。
            input_diversion_port: Attention active 时接纳已公开文本的端口。
            current_runtime_epoch: 返回当前 Assistant Runtime 世代的函数。
            now: 生成单调时间的函数。
        """

        self.hub = hub
        self.realtime_input_runtime_owner = realtime_input_runtime_owner
        self.livekit_media_session_controller = livekit_media_session_controller
        self.assistant_request_context_preparer = assistant_request_context_preparer
        self.material_lifecycle = material_lifecycle
        self.assistant_runtime_provider = assistant_runtime_provider
        self.input_diversion_port = input_diversion_port
        self.current_runtime_epoch = current_runtime_epoch
        self.now = now

    async def dispatch(self, text_turn: TextTurnDispatch) -> TurnSubmissionOutcome:
        """冻结并派发一条已经通过顺序屏障的文本输入。

        Args:
            text_turn: reservation 与环境准备都已完成的文本提交。

        Returns:
            用户消息已经公开后的助手请求启动结果。

        Raises:
            ValueError: 文本或提交编号为空。
            Exception: 用户消息公开前的准备、构造或广播失败。
        """

        trimmed_text = text_turn.message_text.strip()
        submission_id = text_turn.submission_id.strip()
        request_id = text_turn.request_id.strip()
        if not trimmed_text:
            raise ValueError("文本提交正文不能为空")
        if not submission_id:
            raise ValueError("文本提交编号不能为空")
        if not request_id:
            raise ValueError("文本助手请求编号不能为空")

        logger.info(
            "派发 desktop 文本输入 submission_id=%s text_length=%s",
            submission_id,
            len(trimmed_text),
        )
        realtime_input_runtime = (
            self.realtime_input_runtime_owner.require_for_turn_state()
        )
        input_started_at = self.now()
        # 输入归属必须早于公开事件。Desktop 据此只做 episode 临时显示，不能先把
        # 同一条正文写进稳定聊天、事后再尝试撤销。
        diversion_result = self._divert_text_input(text_turn, trimmed_text)
        history_disposition = (
            diversion_result.history_disposition
            if diversion_result is not None
            else "ordinary_turn"
        )
        if input_diversion_blocks_normal_request(diversion_result):
            # Episode append 没有成功时不能先让 Desktop 展示一条看似已经接纳的输入。
            # Reservation 会保留失败事实，用户可以显式重试；这里也绝不创建普通 Turn。
            logger.warning(
                "文本输入改道未完成，阻断普通助手请求 submission_id=%s reason=%s",
                submission_id,
                diversion_result.reason if diversion_result is not None else "unknown",
            )
            return TurnSubmissionOutcome(
                request_id=request_id,
                assistant_request_started=False,
            )
        final_payload: dict[str, object] = {
            "session_id": "desktop-text",
            "turn_id": None,
            "request_id": request_id,
            "submission_id": submission_id,
            "text": trimmed_text,
            "history_disposition": history_disposition,
        }
        if diversion_result is not None and diversion_result.episode_id is not None:
            final_payload["episode_id"] = diversion_result.episode_id
        await self.hub.publisher("interaction").publish(
            {
                "type": "user.final_transcript",
                "payload": final_payload,
            }
        )

        # transcript 成功发布后，这条用户输入才成为正式事实。近期路径登记不能提前，
        # 也不能因为快照广播异常反过来否定已经显示给用户的输入。
        if self.material_lifecycle.remember_recent_materials(text_turn.attachments):
            try:
                await self.material_lifecycle.broadcast_changed()
            except Exception:
                logger.exception(
                    "近期材料已登记但快照 revision 发布失败 submission_id=%s",
                    submission_id,
                )

        if input_diversion_was_accepted(diversion_result):
            return TurnSubmissionOutcome(
                request_id=request_id,
                assistant_request_started=False,
                diverted_to_episode=True,
            )
        attention_lease = (
            diversion_result.assistant_request_lease
            if diversion_result is not None
            else None
        )

        try:
            prepared_context = self.assistant_request_context_preparer.prepare(
                request_id=request_id,
                request_source="text_input",
                turn_material_context_id=text_turn.turn_material_context_id,
                environment_snapshot_id=text_turn.environment_snapshot_id,
            )
        except Exception:
            if attention_lease is not None:
                attention_lease.rollback()
            raise
        snapshot = prepared_context.snapshot
        try:
            request = AssistantRequest(
                request_id=request_id,
                source_session_id="desktop-text",
                interaction_session_id=(
                    realtime_input_runtime.interaction_state.interaction_session_id
                ),
                character_card=snapshot.character_card,
                user_call_name=snapshot.user_call_name,
                user_self_intro=snapshot.user_self_intro,
                user_extra_notes=snapshot.user_extra_notes,
                user_text=trimmed_text,
                source="text_input",
                source_turn_id=None,
                requested_at=input_started_at,
                turn_material_context_id=snapshot.turn_material_context_id,
                foreground_context=snapshot.foreground_context,
                environment_snapshot=snapshot.environment_snapshot,
                frozen_visual_input=snapshot.frozen_visual_input,
                runtime_memory_snapshot=snapshot.runtime_memory_snapshot,
                history_policy=snapshot.history_policy,
                activity_progress_references=(
                    snapshot.activity_progress_references
                ),
                derived_from_activity_ids=snapshot.derived_from_activity_ids,
            )
        except Exception:
            prepared_context.rollback()
            if attention_lease is not None:
                attention_lease.rollback()
            raise

        try:
            text_turn_events = realtime_input_runtime.apply_text_input_started(
                now=input_started_at,
                request_id=request_id,
            )
            for event in text_turn_events:
                await self.hub.publisher("interaction").publish(event)

            # 没有媒体会话时绝不能绕过 Bridge 直连运行时：未经身份 Registry claim
            # 提交的请求，其遗留事件会让后续新建的 Bridge 抛身份冲突并永久
            # fail-closed。按待发文本语义把本次提交收口为启动失败，由准入层
            # 统一置为可重试的失败结果。
            if not self.livekit_media_session_controller.enqueue_assistant_request(
                request
            ):
                raise AssistantOutputUnavailableError(
                    "当前没有可接收助手请求的媒体会话"
                )
        except Exception as exception:
            prepared_context.rollback()
            if attention_lease is not None:
                attention_lease.rollback()
            logger.exception(
                "用户消息已显示但助手请求启动失败 request_id=%s",
                request_id,
                exc_info=exception,
            )
            await self._restore_idle_after_start_failure(
                realtime_input_runtime,
                request_id=request_id,
            )
            return TurnSubmissionOutcome(
                request_id=request_id,
                assistant_request_started=False,
            )

        if attention_lease is not None and not attention_lease.commit_enqueued():
            logger.warning(
                "文本助手请求已入队但 Attention lease 未提交 request_id=%s",
                request_id,
            )

        try:
            committed = prepared_context.commit_enqueued()
            if not committed:
                logger.warning(
                    "助手请求已启动但 Notice lease 未提交 request_id=%s",
                    request_id,
                )
        except Exception as exception:
            logger.exception(
                "助手请求已启动但 Notice lease 提交异常 request_id=%s",
                request_id,
                exc_info=exception,
            )
        return TurnSubmissionOutcome(
            request_id=request_id,
            assistant_request_started=True,
        )

    def _divert_text_input(
        self,
        text_turn: TextTurnDispatch,
        trimmed_text: str,
    ) -> InputDiversionResult | None:
        """在普通上下文冻结前尝试把文本归入当前未处理插话片段。

        Args:
            text_turn: 已通过文本顺序屏障的输入事实。
            trimmed_text: 已裁剪且已经公开的正文。

        Returns:
            Attention 的结构化裁决；端口未接线时返回 ``None``，表示可以走普通 Turn。
            端口异常或世代无效时返回 fail-closed 的 ``not_diverted`` 结果。
        """

        port = self.input_diversion_port
        if port is None:
            return None
        epoch_reader = self.current_runtime_epoch
        if epoch_reader is None:
            logger.warning(
                "文本输入改道端口已接线但没有 runtime epoch 读取端口 submission_id=%s",
                text_turn.submission_id,
            )
            return InputDiversionResult(
                status="not_diverted",
                reason="runtime_epoch_unavailable",
            )
        try:
            runtime_epoch = epoch_reader()
        except Exception:
            logger.exception(
                "读取文本输入改道 runtime epoch 失败 submission_id=%s",
                text_turn.submission_id,
            )
            return InputDiversionResult(
                status="not_diverted",
                reason="diversion_failed",
            )
        if type(runtime_epoch) is not int or runtime_epoch <= 0:
            logger.warning(
                "无法裁决文本输入改道：Assistant Runtime 世代无效 submission_id=%s",
                text_turn.submission_id,
            )
            return InputDiversionResult(
                status="not_diverted",
                reason="runtime_epoch_unavailable",
            )
        try:
            return port.divert_final_input(
                DivertFinalInputCommand(
                    runtime_epoch=runtime_epoch,
                    input_identity=text_turn.submission_id,
                    assistant_request_id=text_turn.request_id,
                    source="text",
                    text=trimmed_text,
                    materials=text_turn.materials,
                )
            )
        except Exception:
            # Attention 是输入归属的唯一裁决者。这里不能把异常当作“没有反思”，否则
            # 同一条已经公开的正文会越过恢复状态机进入普通 AssistantRequest。
            logger.exception(
                "文本输入改道裁决失败 submission_id=%s",
                text_turn.submission_id,
            )
            return InputDiversionResult(
                status="not_diverted",
                reason="diversion_failed",
            )

    def cancel_superseded_system_notice(self, request_id: str) -> None:
        """在待发用户文本需要优先时取消指定的独立 System Notice。

        Args:
            request_id: 已被文本输入取代的 System Notice 请求编号。

        Returns:
            无。控制命令无法提交时由运行时端口抛出明确异常。
        """

        normalized_request_id = request_id.strip()
        if not normalized_request_id:
            raise ValueError("request_id 不能为空")
        command = CancelSupersededSystemNoticeCommand(
            request_id=normalized_request_id,
            reason="text_input",
            issued_at=self.now(),
        )
        if self.livekit_media_session_controller.enqueue_assistant_control(command):
            return
        self.assistant_runtime_provider().submit_control_command(command)

    async def _restore_idle_after_start_failure(
        self,
        realtime_input_runtime: RealtimeInputRuntime,
        *,
        request_id: str,
    ) -> None:
        """尽力把用户消息提交后的助手启动失败收口到 idle。

        Args:
            realtime_input_runtime: 当前产品层 Turn 状态唯一写入口。
            request_id: 用于记录补偿广播失败的请求编号。
        """

        try:
            idle_events = realtime_input_runtime.apply_assistant_output_finished(
                reason="assistant_request_start_failed",
                now=self.now(),
                request_id=request_id,
            )
            for event in idle_events:
                await self.hub.publisher("interaction").publish(event)
        except Exception as exception:
            logger.exception(
                "助手启动失败后的 idle 补偿异常 request_id=%s",
                request_id,
                exc_info=exception,
            )
