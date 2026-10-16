"""处理助手请求未进入可用运行时后的精确补偿。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from companion.activities.notice.delivery_event_mapper import map_notice_delivery_event
from companion.activities.notice.models import NoticeDeliveryCommand, NoticeDeliveryResult
from companion.realtime.assistant_events import RuntimeEvent
from companion.realtime.assistant_request_failure import build_assistant_request_failed_event
from companion.realtime.assistant_request_failure import AssistantRequestFailureCode
from companion.realtime.realtime_input_runtime import RealtimeInputRuntime
from companion.realtime.turn_material_release_service import TurnMaterialReleaseService
from companion.observability.logging_config import get_backend_logger


logger = get_backend_logger(__name__)


class AssistantRequestFailureHandler:
    """补偿未启动请求的 Notice、材料、产品 Turn 与失败气泡。"""

    @staticmethod
    def owns(event: RuntimeEvent) -> bool:
        """判断一条事件是否属于请求启动失败补偿。

        Args:
            event: 尚未选择运行时处理器的事件。

        Returns:
            事件明确表示请求未启动或输出链路不可用时返回真。
        """

        if event.type == "assistant.request_enqueue_failed":
            return True
        return event.type in {
            "assistant.generation_interrupted",
            "assistant.generation_superseded",
        } and event.payload.get("reason") in {
            "runtime_submit_failed",
            "runtime_unavailable",
            "output_unavailable",
            "request_identity_conflict",
        }

    def __init__(
        self,
        *,
        publish_interaction: Callable[[dict[str, Any]], Awaitable[None]],
        publish_assistant_output: Callable[[dict[str, Any]], Awaitable[None]],
        realtime_input_runtime_provider: Callable[[], RealtimeInputRuntime],
        turn_material_release_service: TurnMaterialReleaseService,
        notice_delivery_handler: Callable[
            [NoticeDeliveryCommand], NoticeDeliveryResult | None
        ]
        | None = None,
    ) -> None:
        """初始化请求失败补偿端口。

        Args:
            publish_interaction: 广播 Turn 收口事件的 Interaction 领域端口。
            publish_assistant_output: 广播请求失败事件的 Assistant Output 领域端口。
            realtime_input_runtime_provider: 读取当前产品 Turn 状态所有者的端口。
            turn_material_release_service: 释放请求绑定材料上下文的领域用例。
            notice_delivery_handler: 同步回滚 Notice 交付状态的端口。
        """

        self._publish_interaction = publish_interaction
        self._publish_assistant_output = publish_assistant_output
        self._realtime_input_runtime_provider = realtime_input_runtime_provider
        self._turn_material_release_service = turn_material_release_service
        self._notice_delivery_handler = notice_delivery_handler
        self._failure_broadcast_request_ids: set[str] = set()

    async def try_handle(self, event: RuntimeEvent) -> bool:
        """识别并补偿请求启动失败事件。

        Args:
            event: 可能描述入队失败或运行时不可用的事件。

        Returns:
            当前事件属于请求启动失败协议并已消费时返回真。
        """
        if not self.owns(event):
            return False
        is_enqueue_failure = event.type == "assistant.request_enqueue_failed"

        # Final ASR 入队失败已由输入线程同步回滚 Notice；Bridge 失效才在此补偿。
        if not is_enqueue_failure and self._notice_delivery_handler is not None:
            # Runtime 中断时保留待重试的强提醒、丢弃过期弱提醒，并释放原请求账本。
            command = map_notice_delivery_event(event.type, event.payload)
            if command is not None:
                self._notice_delivery_handler(command)

        await self.compensate(
            event,
            code=_failure_code_for_event(event),
            require_source=True,
        )
        return True

    async def compensate(
        self,
        event: RuntimeEvent,
        *,
        code: AssistantRequestFailureCode,
        require_source: bool = False,
    ) -> bool:
        """释放失败请求，并最多广播一次稳定失败事件。

        Args:
            event: 携带请求、材料和输入关联信息的运行时事件。
            code: 对用户公开的稳定失败分类。
            require_source: 是否只处理文本或语音来源；输出链路校验失败不要求来源字段。

        Returns:
            找到非空请求编号并完成补偿时返回真。
        """

        await self._turn_material_release_service.release_from_payload(event.payload)

        request_id = _clean_text(event.payload.get("request_id"))
        if not request_id:
            return False
        if require_source and event.payload.get("source") not in {
            "text_input",
            "final_asr",
        }:
            return True

        # RealtimeInputRuntime 再次核对 request_id，防止迟到旧事件结束当前新请求。
        apply_finished = getattr(
            self._realtime_input_runtime_provider(),
            "apply_assistant_output_finished",
            None,
        )
        if callable(apply_finished):
            for turn_event in apply_finished(
                reason="assistant_request_start_failed",
                now=event.produced_at,
                request_id=request_id,
            ):
                await self._publish_interaction(turn_event)
        if (
            event.payload.get("visible_response_completed") is not True
            and request_id not in self._failure_broadcast_request_ids
        ):
            self._failure_broadcast_request_ids.add(request_id)
            try:
                await self._publish_assistant_output(
                    build_assistant_request_failed_event(request_id, code=code)
                )
            except Exception:
                # 正文出口已经失败时，失败事件也可能暂时无法发送。资源收口仍然必须完成，
                # 不能因为第二次广播失败把材料和 Turn 再次留在半完成状态。
                logger.exception(
                    "助手请求失败事件广播失败 request_id=%s code=%s",
                    request_id,
                    code,
                )
        return True


def _clean_text(value: object) -> str:
    """把内部事件字段清洗为无首尾空白的字符串。"""

    return value.strip() if isinstance(value, str) else ""


def _failure_code_for_event(event: RuntimeEvent) -> AssistantRequestFailureCode:
    """把运行时失败原因收窄成三个稳定公共错误码。"""

    if event.payload.get("reason") == "request_identity_conflict":
        return "request_identity_conflict"
    if event.payload.get("reason") in {"runtime_unavailable", "output_unavailable"}:
        return "output_unavailable"
    return "request_start_failed"
