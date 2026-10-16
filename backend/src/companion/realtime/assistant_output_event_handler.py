"""处理助手 generation、文本、音频与产品 Turn 收口事件。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from companion.activities.notice.delivery_event_mapper import map_notice_delivery_event
from companion.activities.notice.models import NoticeDeliveryCommand, NoticeDeliveryResult
from companion.observability.logging_config import get_backend_logger
from companion.realtime.activity_runtime_event_handler import ActivityRuntimeEventHandler
from companion.realtime.assistant_events import RuntimeEvent
from companion.realtime.assistant_output_settlement_service import (
    AssistantOutputSettlementService,
)
from companion.realtime.assistant_output_commit_coordinator import (
    AssistantOutputCommitCoordinator,
)
from companion.realtime.assistant_public_event_projection import (
    project_public_assistant_event,
)
from companion.realtime.realtime_input_runtime import RealtimeInputRuntime
from companion.realtime.turn_material_release_service import TurnMaterialReleaseService
from companion.realtime.playout_delivery import PlayoutCompletion, PlayoutDeliveryRegistry
from companion.realtime.visible_output_journal import VisibleOutputSettlement


logger = get_backend_logger(__name__)


class AssistantOutputEventHandler:
    """投影助手输出事件，并收口 Notice、材料与产品 Turn 状态。"""

    def __init__(
        self,
        *,
        publish_interaction: Callable[[dict[str, Any]], Awaitable[None]],
        publish_assistant_output: Callable[[dict[str, Any]], Awaitable[None]],
        realtime_input_runtime_provider: Callable[[], RealtimeInputRuntime],
        activity_event_handler: ActivityRuntimeEventHandler,
        turn_material_release_service: TurnMaterialReleaseService,
        notice_delivery_handler: Callable[
            [NoticeDeliveryCommand], NoticeDeliveryResult | None
        ]
        | None = None,
        playout_delivery_registry: PlayoutDeliveryRegistry | None = None,
        output_settlement_service: AssistantOutputSettlementService | None = None,
        output_settlement_committed_handler: Callable[
            [VisibleOutputSettlement], object
        ]
        | None = None,
    ) -> None:
        """初始化助手输出收口端口。

        Args:
            publish_interaction: 广播 Turn 状态事件的 Interaction 领域端口。
            publish_assistant_output: 广播助手正文与播放交付事件的 Assistant Output
                领域端口；``assistant.text_final`` 仍由 Hub 额外生成桌宠壳层投影。
            realtime_input_runtime_provider: 读取当前产品 Turn 状态所有者的端口。生产环境中
                ``AssistantRuntimeEventRouter`` 将当前 ``LiveKitMediaSession`` 持有的
                ``RealtimeInputRuntime`` 包装为 provider 传入；它负责按 ``request_id`` 把
                音频开始、取消、结束或助手失败写回对应的产品 Turn。
            activity_event_handler: 同一个 ``AssistantRuntimeEventRouter`` 创建的
                ``ActivityRuntimeEventHandler``。当发起 TaskResult 的 Foreground 请求收到
                ``assistant.text_final``、生成被打断或被替代时，本处理器调用它的
                ``acknowledge_task_result_request``，让已终态的 TaskResult 可以继续结果卡、
                Notice 与材料租约的交付判断。
            turn_material_release_service: 同一个 ``AssistantRuntimeEventRouter`` 创建的
                ``TurnMaterialReleaseService``。它内部绑定 ``MaterialLifecycle.finish_turn``
                与 Materials 领域瞬时 Publisher；收到请求终态后，按事件中的
                ``turn_material_context_id`` 结束本轮材料并广播瞬时材料快照。
            notice_delivery_handler: 同步推进 Notice 交付状态的端口。生产环境由
                ``ApplicationContainer.create_livekit_media_session`` 注入
                ``NoticeDeliveryCoordinator.apply_delivery``。本处理器把
                ``assistant.text_final``、生成取消、替代或失败映射为交付命令交给它，
                由 Coordinator 清除已完成的 Notice 批次或执行相应补偿。
            output_settlement_service: 在文本广播成功后记录正文并提交稳定记忆的服务。
            output_settlement_committed_handler: settlement 真正越过正文提交 lane 后的通知端口。
        """

        self._publish_interaction = publish_interaction
        self._publish_assistant_output = publish_assistant_output
        self._realtime_input_runtime_provider = realtime_input_runtime_provider
        self._activity_event_handler = activity_event_handler
        self._turn_material_release_service = turn_material_release_service
        self._notice_delivery_handler = notice_delivery_handler
        self._playout_delivery_registry = playout_delivery_registry or PlayoutDeliveryRegistry()
        output_settlement_service_value = (
            output_settlement_service or AssistantOutputSettlementService()
        )
        self._output_commit_coordinator = AssistantOutputCommitCoordinator(
            publish_assistant_output=publish_assistant_output,
            output_settlement_service=output_settlement_service_value,
            turn_material_release_service=turn_material_release_service,
            activity_event_handler=activity_event_handler,
            notice_delivery_handler=notice_delivery_handler,
            settlement_committed_handler=output_settlement_committed_handler,
        )

    def cancel_request(self, request_id: str) -> None:
        """禁止一条已失效请求继续公开迟到正文。

        Args:
            request_id: 需要在主进程输出边界登记墓碑的请求编号。

        Returns:
            None: 输出提交协调器会同步保存取消事实。
        """

        self._output_commit_coordinator.cancel_request(request_id)

    async def handle(self, event: RuntimeEvent) -> None:
        """处理已经通过失败补偿、世代过滤和 Activity 路由的事件。

        Args:
            event: 当前助手运行时世代产生的输出事件。
        """

        if event.type == "assistant.text_chunk":
            chunk_text = event.payload.get("text")
            if isinstance(chunk_text, str) and not chunk_text.strip():
                # 正常生产流会在 Foreground 合并空白。这里吸收绕过聚合层的事件，
                # 避免一个合法 Provider delta 把整项请求误判为输出不可用。
                logger.warning(
                    "吸收绕过正文聚合层的空白助手分片 "
                    "event_type=%s source=%s "
                    "reason_code=assistant_output_whitespace_chunk_absorbed",
                    event.type,
                    event.source,
                )
                return

        if event.type in {"assistant.generation_interrupted", "assistant.generation_superseded"}:
            self._notify_delivery(event)
            await self._turn_material_release_service.release_from_payload(event.payload)
            await self._acknowledge_task_result_request(event)
            request_id = _clean_text(event.payload.get("request_id"))
            if request_id:
                # generation 没有产出音频时不会再有 audio_finished。这里必须直接释放
                # 它登记过的输出身份，否则 Turn 会永久停在 processing，后续提醒也无法播报。
                await self._broadcast_turn_events(
                    self._realtime_input_runtime_provider().apply_assistant_output_finished(
                        reason=(
                            "assistant_generation_interrupted"
                            if event.type == "assistant.generation_interrupted"
                            else "assistant_generation_superseded"
                        ),
                        now=event.produced_at,
                        request_id=request_id,
                        generation_id=(
                            _clean_text(event.payload.get("generation_id")) or None
                        ),
                    )
                )
            return

        # 状态失败是内部生命周期事实，只负责释放 Turn；公共失败气泡由请求失败协议产生。
        if event.type == "assistant.state_changed":
            if event.payload.get("assistant_state") == "failed":
                await self._handle_assistant_failed(event)
            return

        # 控制 WebSocket 不承载二进制音频；音频块只由 LiveKit 媒体会话发布。
        if event.type == "assistant.audio_chunk":
            logger.info("Skipping assistant.audio_chunk in event router; LiveKit owns audio publishing")
            return
        if event.type in {"wake_response.audio_chunk", "wake_response.audio_finished"}:
            logger.info(
                "Skipping %s in event router; LiveKit owns wake response audio publishing",
                event.type,
            )
            return
        if event.type == "assistant.audio_publishing_started":
            await self._handle_audio_publishing_started(event)
            return
        if event.type == "assistant.audio_publishing_cancelled":
            await self._handle_audio_publishing_cancelled(event)
            return
        if event.type == "assistant.tts_segment_failed":
            await self._handle_tts_segment_failed(event)
            return

        if event.type in {
            "assistant.text_chunk",
            "assistant.text_segment_final",
            "assistant.text_final",
        }:
            await self._output_commit_coordinator.handle_text(event)
            return

        await self._publish_assistant_output(project_public_assistant_event(event))
        if event.type == "assistant.audio_finished":
            await self._handle_audio_finished(event)
            return

    async def handle_settlement(self, event: RuntimeEvent) -> None:
        """让输出结算等待同一执行身份的所有已公开正文完成提交。"""

        await self._output_commit_coordinator.handle_settlement(event)

    async def _handle_audio_publishing_started(self, event: RuntimeEvent) -> None:
        """把音频首帧发布事实写入产品 Turn，并建立一次播放交付账本。"""

        request_id = _clean_text(event.payload.get("request_id"))
        if not request_id:
            logger.warning("assistant.audio_publishing_started 缺少 request_id，已忽略状态推进")
            return
        generation_id = event.payload.get("generation_id")
        generation_id_value = generation_id if isinstance(generation_id, str) else None
        await self._broadcast_turn_events(
            self._realtime_input_runtime_provider().apply_assistant_audio_publishing_started(
                now=event.produced_at,
                generation_id=generation_id_value,
                request_id=request_id,
            )
        )
        delivery_id = _clean_text(event.payload.get("delivery_id"))
        generation_id_value = _clean_text(event.payload.get("generation_id"))
        if not delivery_id or not generation_id_value:
            logger.warning("assistant.audio_publishing_started 缺少 delivery_id 或 generation_id")
            return
        update = self._playout_delivery_registry.start_publishing(
            delivery_id=delivery_id,
            request_id=request_id,
            generation_id=generation_id_value,
            occurred_at=event.produced_at,
        )
        if update.public_event is not None:
            await self._publish_assistant_output(update.public_event)

    async def _handle_audio_publishing_cancelled(self, event: RuntimeEvent) -> None:
        """把音频播放取消事实写入产品 Turn，并收口同一次播放交付。"""

        request_id = _clean_text(event.payload.get("request_id"))
        if not request_id:
            logger.warning("assistant.audio_publishing_cancelled 缺少 request_id，已忽略状态推进")
            return
        generation_id = event.payload.get("generation_id")
        generation_id_value = generation_id if isinstance(generation_id, str) else None
        await self._broadcast_turn_events(
            self._realtime_input_runtime_provider().apply_assistant_audio_publishing_cancelled(
                now=event.produced_at,
                generation_id=generation_id_value,
                request_id=request_id,
            )
        )
        delivery_id = _clean_text(event.payload.get("delivery_id"))
        generation_id_value = _clean_text(event.payload.get("generation_id"))
        if delivery_id and generation_id_value:
            self._playout_delivery_registry.abort(
                delivery_id=delivery_id,
                request_id=request_id,
                generation_id=generation_id_value,
            )

    async def _handle_audio_finished(self, event: RuntimeEvent) -> None:
        """把音频输出完成写入对应产品 Turn。"""

        request_id = _clean_text(event.payload.get("request_id"))
        if not request_id:
            logger.warning("assistant.audio_finished 缺少 request_id，已忽略状态收尾")
            return
        await self._broadcast_turn_events(
            self._realtime_input_runtime_provider().apply_assistant_output_finished(
                reason="assistant_audio_finished",
                now=event.produced_at,
                request_id=request_id,
                generation_id=_clean_text(event.payload.get("generation_id")) or None,
            )
        )
        delivery_id = _clean_text(event.payload.get("delivery_id"))
        generation_id = _clean_text(event.payload.get("generation_id"))
        if delivery_id and generation_id:
            self._playout_delivery_registry.finish(
                delivery_id=delivery_id,
                request_id=request_id,
                generation_id=generation_id,
                completion=_playout_completion(event.payload.get("playout_completion")),
                occurred_at=event.produced_at,
            )

    async def _handle_tts_segment_failed(self, event: RuntimeEvent) -> None:
        """把 TTS 异常转换为不泄露原始错误的播放失败事件。"""

        delivery_id = _clean_text(event.payload.get("delivery_id"))
        request_id = _clean_text(event.payload.get("request_id"))
        generation_id = _clean_text(event.payload.get("generation_id"))
        if not delivery_id or not request_id or not generation_id:
            logger.warning("assistant.tts_segment_failed 缺少交付身份，已忽略公开失败")
            return
        update = self._playout_delivery_registry.fail(
            delivery_id=delivery_id,
            request_id=request_id,
            generation_id=generation_id,
            failure_code="tts_failed",
            failure_message="语音合成没有完成，文字回复仍可查看。",
            occurred_at=event.produced_at,
        )
        if update.public_event is not None:
            await self._publish_assistant_output(update.public_event)

    async def _handle_assistant_failed(self, event: RuntimeEvent) -> None:
        """释放模型显式失败事件对应的产品 Turn 忙碌态。"""

        self._notify_delivery(event)
        request_id = _clean_text(event.payload.get("request_id"))
        if not request_id:
            logger.warning("assistant.state_changed=failed 缺少 request_id，已忽略状态收尾")
            return
        await self._broadcast_turn_events(
            self._realtime_input_runtime_provider().apply_assistant_output_finished(
                reason="assistant_failed",
                now=event.produced_at,
                request_id=request_id,
                generation_id=_clean_text(event.payload.get("generation_id")) or None,
            )
        )

    async def _acknowledge_task_result_request(self, event: RuntimeEvent) -> None:
        """用请求终态开放其发起的 TaskResult 结果交付边界。"""

        request_id = _clean_text(event.payload.get("request_id"))
        if event.runtime_epoch is None or not request_id:
            return
        await self._activity_event_handler.acknowledge_task_result_request(
            event.runtime_epoch,
            request_id,
        )

    async def _broadcast_turn_events(self, events: list[dict[str, Any]]) -> None:
        """广播 RealtimeInputRuntime 已同步状态后产生的 Turn 事件。"""

        for turn_event in events:
            await self._publish_interaction(turn_event)

    def _notify_delivery(self, event: RuntimeEvent) -> None:
        """把助手输出状态变化同步交给 Notice 交付账本。"""

        if self._notice_delivery_handler is None:
            return
        command = map_notice_delivery_event(event.type, event.payload)
        if command is not None:
            self._notice_delivery_handler(command)


def _clean_text(value: object) -> str:
    """把内部事件字段清洗为无首尾空白的字符串。"""

    return value.strip() if isinstance(value, str) else ""


def _playout_completion(value: object) -> PlayoutCompletion:
    """把运行时完成分类收窄为播放交付账本接受的稳定值。

    Args:
        value: 音频调度器写入事件的原始完成分类。

    Returns:
        合法分类原样返回；缺失或非法值按底层异常处理，避免误记为已送达。
    """

    if value == "drained":
        return "drained"
    if value == "unconfirmed_timeout":
        return "unconfirmed_timeout"
    if value == "unconfirmed_error":
        return "unconfirmed_error"
    return "unconfirmed_error"
