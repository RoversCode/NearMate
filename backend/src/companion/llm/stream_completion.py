"""验证 Chat Completions 流在结束前出现合法终态。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


StreamProtocol = Literal["chat_completions"]
_VALID_CHAT_FINISH_REASONS = frozenset(
    {
        "stop",
        "length",
        "tool_calls",
        "function_call",
        "content_filter",
    }
)


class ProviderStreamInterruptedError(RuntimeError):
    """表示 Provider 流在合法完成信号出现前结束。

    Args:
        protocol: 发生中断的流式协议。
        terminal_signal: 缺失或非法的终态信号描述。
    """

    activity_failure_code = "provider_stream_interrupted"

    def __init__(self, *, protocol: StreamProtocol, terminal_signal: str) -> None:
        super().__init__(f"{protocol} 流未正常完成：{terminal_signal}")
        self.protocol = protocol
        self.terminal_signal = terminal_signal


@dataclass(slots=True)
class StreamCompletionTracker:
    """跟踪单次 Provider 流是否出现合法完成信号。

    Args:
        protocol: 当前流使用的 Provider 协议。
    """

    protocol: StreamProtocol
    _completed: bool = False

    def observe_chat_chunk(self, chunk: Any) -> None:
        """吸收 Chat Completions 分片中的结束原因。

        Args:
            chunk: SDK 返回的 Chat Completions 分片。

        Raises:
            ProviderStreamInterruptedError: Provider 返回了非法结束原因。
        """

        choices = getattr(chunk, "choices", None)
        if not choices:
            return
        for choice in choices:
            finish_reason = getattr(choice, "finish_reason", None)
            if finish_reason is None:
                continue
            if not isinstance(finish_reason, str) or finish_reason not in (
                _VALID_CHAT_FINISH_REASONS
            ):
                raise ProviderStreamInterruptedError(
                    protocol="chat_completions",
                    terminal_signal="invalid_finish_reason",
                )
            self._completed = True

    def require_completed(self) -> None:
        """确认流在 EOF 前出现了协议规定的完成信号。

        Raises:
            ProviderStreamInterruptedError: 流在完成信号出现前静默结束。
        """

        if self._completed:
            return
        raise ProviderStreamInterruptedError(
            protocol=self.protocol,
            terminal_signal="missing_finish_reason",
        )
