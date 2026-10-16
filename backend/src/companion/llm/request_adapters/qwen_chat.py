from __future__ import annotations

import logging
from typing import Any

from companion.llm.request_intent import LlmInvocationIntent
from companion.llm.request_profiles import QwenChatRequestProfile
from companion.observability.logging_config import get_backend_logger
from companion.observability.structured_logging import log_event


logger = get_backend_logger(__name__)


class QwenChatRequestAdapter:
    """按具体模型请求配置把统一能力意图编译成 Qwen Chat 方言。

    Args:
        request_profile: 当前模型已经通过合同测试的请求参数配置。
    """

    def __init__(self, request_profile: QwenChatRequestProfile) -> None:
        self._request_profile = request_profile

    @property
    def request_profile(self) -> QwenChatRequestProfile:
        """返回当前 Adapter 使用的只读模型请求配置。

        Returns:
            Factory 注入的模型请求配置。
        """

        return self._request_profile

    def compile(
        self,
        *,
        intent: LlmInvocationIntent,
        tools: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """生成 Qwen Thinking、联网和输出格式参数。

        Args:
            intent: 业务调用声明的厂家无关能力。
            tools: 标准函数工具；当前 Qwen 接入没有验证该能力。

        Returns:
            可以直接与 model、messages、stream 合并的独立字典。

        Raises:
            ValueError: 调用请求了当前模型没有验证或已知冲突的能力组合。
        """

        if tools or intent.tool_mode != "forbid":
            raise ValueError("Qwen Chat 接入不支持项目函数工具")

        wants_web_access = intent.web_search or intent.web_extract
        if intent.output_format == "json_object" and wants_web_access:
            # 百炼联网搜索明确拒绝 JSON Mode，提前失败可避免一次注定返回 400 的请求。
            raise ValueError("Qwen 联网搜索不支持 json_object 输出")
        if intent.output_format != self._request_profile.required_output_format:
            raise ValueError(
                "当前 Qwen 模型请求配置只允许 "
                f"{self._request_profile.required_output_format} 输出"
            )
        if wants_web_access and not self._request_profile.supports_web_search:
            raise ValueError("当前 Qwen 模型请求配置不支持联网搜索或网页抓取")

        enable_thinking = self._compile_thinking_switch(intent.thinking_level)
        extra_body: dict[str, Any] = {"enable_thinking": enable_thinking}
        # Cognitive 的联网请求必须命中 Qwen 厂商检索，不允许退回只依赖模型记忆的默认行为。
        if wants_web_access:
            extra_body["enable_search"] = True
            extra_body["search_options"] = {"forced_search": True}

        request: dict[str, Any] = {"extra_body": extra_body}
        if intent.output_format == "json_object":
            request["response_format"] = {"type": "json_object"}
        if intent.max_output_tokens is not None:
            request["max_tokens"] = intent.max_output_tokens
        return request

    def _compile_thinking_switch(self, thinking_level: str) -> bool:
        """把统一思考档位翻译成当前 Qwen 模型能够表达的开关。

        Args:
            thinking_level: 业务请求的 inherit、off、minimal 或 high 档位。

        Returns:
            发送给百炼 `enable_thinking` 的布尔值。

        Raises:
            ValueError: 当前模型只允许关闭思考，却收到开启意图。
        """

        if thinking_level in {"inherit", "off"}:
            return False
        if self._request_profile.thinking_mode == "off_only":
            raise ValueError("当前 Qwen 模型请求配置只允许关闭思考")

        # Qwen Chat 只提供布尔开关，业务请求的档位会丢失；日志明确记录这次能力降级。
        log_event(
            logger,
            logging.INFO,
            "llm_capability_degraded",
            provider="qwen",
            model=self._request_profile.model,
            request_profile_id=self._request_profile.profile_id,
            requested_thinking_level=thinking_level,
            actual_mapping="enable_thinking=true",
            reason="qwen_chat_supports_boolean_thinking_only",
        )
        return True
