from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from threading import Event
from typing import Any

from companion.foreground_tools.cognitive.contracts import CognitiveExecutionPlan
from companion.foreground_tools.cognitive.inference_policies import (
    cognitive_invocation_intent,
)
from companion.foreground_tools.cognitive.prompt import (
    build_cognitive_request_text,
    build_cognitive_system_prompt,
)
from companion.llm.call_trace import ActivityLlmCallTrace
from companion.prompt.xml_markup import render_xml_cdata_leaf


class CognitiveModelNotConfiguredError(RuntimeError):
    """表示认知任务模型尚未配置，Failure Reporter 据此归为配置类错误。"""

    activity_failure_code = "configuration_invalid"


class CognitiveTaskExecutor:
    """按 CognitiveExecutionPlan 执行一次综合 Cognitive 请求。"""

    def __init__(
        self,
        *,
        client: Any | None,
        now: Callable[[], str] | None = None,
    ) -> None:
        """初始化认知任务执行器。

        Args:
            client: 支持图片、推理、标记文本和联网能力的 Cognitive 客户端。
            now: 可注入的本地 ISO 时间来源。
        """

        self._client = client
        self._now = now or _local_iso_datetime_text

    def stream(
        self,
        *,
        activity_id: str,
        plan: CognitiveExecutionPlan,
        source_user_text: str,
        cancellation_event: Event | None = None,
    ) -> Any:
        """流式执行一份综合认知计划。

        Args:
            activity_id: 当前任务的 Activity 编号，用于 Cognitive 日志关联。
            plan: 已准备好图片和 URL 证据的认知计划。
            source_user_text: 触发任务的 Foreground 文本投影。
            cancellation_event: 可选的流式请求协作取消信号。

        Returns:
            Knowledge 客户端产生的文本流。
        """

        if self._client is None:
            raise CognitiveModelNotConfiguredError("认知任务模型尚未配置。")

        messages = _build_cognitive_messages(
            plan=plan,
            source_user_text=source_user_text,
            checked_at=self._now(),
        )
        return self._client.stream_text(
            messages=messages,
            intent=cognitive_invocation_intent(plan),
            audit_source="activity",
            audit_operation="execute",
            trace=ActivityLlmCallTrace.new(
                activity_id=activity_id,
                activity_kind="cognitive",
            ),
            cancellation_event=cancellation_event,
        )


def _build_cognitive_messages(
    *,
    plan: CognitiveExecutionPlan,
    source_user_text: str,
    checked_at: str,
) -> list[dict[str, Any]]:
    """构造包含综合目标和图片证据的 Cognitive 消息。

    Args:
        plan: 当前综合认知计划。
        source_user_text: 当前请求的 Foreground 文本投影。
        checked_at: 本次执行时间。

    Returns:
        可直接传给 OpenAI 兼容客户端的消息列表。
    """

    text = build_cognitive_request_text(
        plan=plan,
        source_user_text=source_user_text,
        checked_at=checked_at,
    )
    if not plan.images:
        user_content: str | list[dict[str, Any]] = text
    else:
        user_content = [{"type": "text", "text": text}]
        # 每张图片前插入来源标签块，形态与 Foreground 侧视觉输入完全一致，
        # 保证标签与图片顺序严格一一对应。
        for image in plan.images:
            user_content.append({"type": "text", "text": render_xml_cdata_leaf(image.prompt_tag, image.label)})
            user_content.append({"type": "image_url", "image_url": {"url": image.data_url}})
    return [
        {"role": "system", "content": build_cognitive_system_prompt(plan)},
        {"role": "user", "content": user_content},
    ]

def _local_iso_datetime_text() -> str:
    """返回当前本地 ISO 时间。

    Returns:
        带本地时区偏移的 ISO 时间文本。
    """

    return datetime.now().astimezone().isoformat()
