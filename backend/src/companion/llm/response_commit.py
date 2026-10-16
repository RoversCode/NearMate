from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal
from uuid import uuid4

from companion.foreground_tools.orchestration.transactions import CommitAction


ForegroundResponseKind = Literal[
    "assistant",
    "assistant_segment",
    "tool_transaction",
    "interrupted",
    "runtime_fallback",
]


@dataclass(frozen=True)
class ForegroundResponseCommit:
    """描述一条最终回复及其唯一历史提交事务。

    Args:
        kind: 普通回复、工具事务、中断片段或运行时兜底。
        visible_text: 用户实际看到的完整最终文本。
        action: 由 generation supervisor 接受并成功入队 final 时提交的幂等动作。
    """

    kind: ForegroundResponseKind
    visible_text: str
    action: CommitAction

    @classmethod
    def create(
        cls,
        *,
        kind: ForegroundResponseKind,
        visible_text: str,
        on_accept: Callable[[], None],
        after_commit: Callable[[], None] | None = None,
    ) -> ForegroundResponseCommit:
        """创建一条等待 supervisor 接受的最终回复事务。

        Args:
            kind: 最终回复类型。
            visible_text: 用户实际可见的最终文本。
            on_accept: final 被当前 generation 接受时执行的历史写入。
            after_commit: final 已公开后执行的压缩等非关键收尾。

        Returns:
            初始状态为 prepared 的最终回复事务。
        """

        transaction_id = f"response-{uuid4().hex}"
        return cls(
            kind=kind,
            visible_text=visible_text,
            action=CommitAction(
                on_accept=on_accept,
                after_commit=after_commit,
                transaction_id=transaction_id,
                resource_type="foreground_response",
                resource_id=transaction_id,
            ),
        )
