from __future__ import annotations

from collections.abc import Callable, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import date, datetime
from threading import RLock
from typing import Any
from uuid import uuid4

from companion.core.local_clock import local_wall_datetime
from companion.llm.tool_protocol import (
    project_tool_calls_for_runtime_memory,
    validate_tool_protocol_messages,
)
from companion.realtime.text_tokenizer import count_text_tokens
from companion.runtime_memory.models import MemoryCommit, RuntimeMemorySnapshot
from companion.runtime_memory.token_accounting import protocol_message_token_text

ProtocolMessageSpec = tuple[
    str,
    str,
    list[dict[str, Any]],
    str | None,
    dict[str, Any],
]


@dataclass
class SessionMessage:
    """表示一条可进入前台模型上下文的 Chat-like 会话消息。

    Args:
        role: 消息角色，遵循项目内部 Chat-like 协议。
        content: 消息正文；工具调用消息允许为空串。
        created_at: 消息创建时间。
        message_id: 当前会话窗口内的单调编号。
        token_count: 该消息对上下文窗口的估算 token 数。
        tool_calls: assistant 消息携带的原生工具调用协议字段。
        tool_call_id: tool 消息对应的工具调用编号。
        llm_context_note: 仅对 LLM 可见、不会替换 UI 正文的历史事实附注。
        metadata: 仅供会话窗口内部筛选和归因使用的结构化标记，不进入 LLM 协议。
    """

    role: str
    content: str
    created_at: datetime = field(default_factory=local_wall_datetime)
    message_id: int | None = None
    token_count: int = 0
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    tool_call_id: str | None = None
    llm_context_note: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_llm_message(self) -> dict[str, Any]:
        """转换为可直接交给 LLM 客户端的 Chat-like 消息。

        Returns:
            保留角色、正文和工具协议字段的消息字典。
        """

        message: dict[str, Any] = {"role": self.role, "content": self.llm_visible_content()}
        if self.tool_calls:
            message["tool_calls"] = deepcopy(self.tool_calls)
        if self.tool_call_id:
            message["tool_call_id"] = self.tool_call_id
        return message

    def llm_visible_content(self) -> str:
        """返回正文与安全历史事实附注组成的 LLM 可见内容。

        Returns:
            可进入提示词历史区的完整消息正文。
        """

        note = self.llm_context_note.strip()
        return f"{self.content}\n\n{note}" if note else self.content


def visible_dialogue_message(message: SessionMessage) -> SessionMessage | None:
    """提取一条可供 System Notice 承接的真实对话消息。

    Args:
        message: 运行期记忆或会话窗口中的原始消息。

    Returns:
        仅保留正文和时间的用户或纯文本助手消息；内部协议消息返回空。
    """

    is_user_dialogue = message.role == "user" and bool(message.content.strip())
    is_assistant_dialogue = (
        message.role == "assistant"
        and bool(message.content.strip())
        and not message.tool_calls
        and message.tool_call_id is None
    )
    if not (is_user_dialogue or is_assistant_dialogue):
        return None

    return SessionMessage(
        role=message.role,
        content=message.content,
        created_at=message.created_at,
        message_id=message.message_id,
    )


@dataclass
class SessionContext:
    """维护前台提示词所需的单个会话窗口。"""

    # 用于区分当前交互会话窗口和迟到的异步压缩结果。
    interaction_session_id: str = ""
    # 已压缩出的滚动摘要，会优先于近期原始消息进入提示词。
    rolling_summary: str = ""
    # 摘要覆盖范围由主进程计算，子进程只能原样用于 Foreground Prompt。
    summary_covered_from: date | None = None
    summary_covered_to: date | None = None
    # 当前摘要覆盖到的最后一条原始消息编号。
    summary_source_message_id: int | None = None
    # 当前滚动摘要和窗口内原始消息合计 token 数，用于 O(1) 判断是否需要压缩。
    context_token_count: int = 0
    # 文本 token 计数函数，默认使用仓库 tokenizer；测试可注入轻量确定性计数器。
    token_counter: Callable[[str], int] = count_text_tokens
    # 真实本地时间源，专用于人类日期语义；测试可注入固定时钟。
    wall_clock: Callable[[], datetime] = local_wall_datetime
    # 当前窗口内尚未被压缩移除的原始消息，仅供内部维护。
    _messages: list[SessionMessage] = field(default_factory=list, init=False, repr=False)
    # 窗口内单调递增的消息编号，重置后从 1 重新开始。
    _next_message_id: int = field(default=1, init=False, repr=False)
    # 前台 LLM 线程和后台摘要线程会同时访问会话窗口，必须用可重入锁保护复合读写。
    _lock: RLock = field(default_factory=RLock, init=False, repr=False)
    # 当前请求读取的主进程快照版本。子进程只用它生成增量提交，不把它当权威状态。
    _base_memory_epoch: int = field(default=0, init=False, repr=False)
    # 当前请求快照中最大的权威消息编号，用于找出本轮新增消息。
    _base_latest_message_id: int = field(default=0, init=False, repr=False)

    def ensure_session(self, interaction_session_id: str) -> bool:
        """记录当前请求归属，但不再用交互会话切换清空历史。

        Args:
            interaction_session_id: 主进程 InteractionState 生成的交互会话标识。

        Returns:
            会话标识发生变化时返回 True。
        """

        normalized_interaction_session_id = interaction_session_id.strip()
        if not normalized_interaction_session_id:
            raise ValueError("interaction_session_id 必须是非空字符串")

        with self._lock:
            if self.interaction_session_id == normalized_interaction_session_id:
                return False
            # 交互会话只隔离请求归属和迟到事件。聊天记忆的生命周期由主进程
            # RuntimeMemoryStore 决定，不能因为语音重连或热区切换而被清空。
            self.interaction_session_id = normalized_interaction_session_id
            return True

    def load_runtime_snapshot(
        self,
        snapshot: RuntimeMemorySnapshot,
        *,
        interaction_session_id: str,
    ) -> None:
        """用主进程快照替换子进程的本轮只读历史视图。

        Args:
            snapshot: 主进程在请求创建时冻结的运行期记忆。
            interaction_session_id: 本轮请求归属的产品交互会话编号。

        Returns:
            无。
        """

        normalized_session_id = interaction_session_id.strip()
        if not normalized_session_id:
            raise ValueError("interaction_session_id 必须是非空字符串")
        with self._lock:
            self.interaction_session_id = normalized_session_id
            self.rolling_summary = snapshot.rolling_summary
            self.summary_covered_from = snapshot.summary_covered_from
            self.summary_covered_to = snapshot.summary_covered_to
            self.summary_source_message_id = snapshot.summary_source_message_id
            self.context_token_count = snapshot.context_token_count
            self._messages = [
                SessionMessage(
                    role=message.role,
                    content=message.content,
                    created_at=message.created_at,
                    message_id=message.message_id,
                    token_count=message.token_count,
                    tool_calls=[deepcopy(item) for item in message.tool_calls],
                    tool_call_id=message.tool_call_id,
                    llm_context_note=message.llm_context_note,
                    metadata=deepcopy(message.metadata),
                )
                for message in snapshot.recent_messages
            ]
            self._next_message_id = snapshot.latest_message_id + 1
            self._base_memory_epoch = snapshot.memory_epoch
            self._base_latest_message_id = snapshot.latest_message_id

    def build_memory_commit(self, *, request_id: str) -> MemoryCommit:
        """把本轮新增历史转换成等待主进程分配编号的提交。

        Args:
            request_id: 产生本次历史增量的助手请求编号。

        Returns:
            包含协议完整新增消息的幂等提交。
        """

        normalized_request_id = request_id.strip()
        if not normalized_request_id:
            raise ValueError("request_id 必须是非空字符串")
        with self._lock:
            messages: list[dict[str, Any]] = []
            for message in self._messages:
                local_message_id = message.message_id or 0
                if local_message_id <= self._base_latest_message_id:
                    continue
                metadata = deepcopy(message.metadata)
                metadata.setdefault(
                    "origin_key",
                    f"{normalized_request_id}:{local_message_id}",
                )
                messages.append(
                    {
                        "role": message.role,
                        "content": message.content,
                        "created_at": message.created_at.isoformat(),
                        "tool_calls": deepcopy(message.tool_calls),
                        "tool_call_id": message.tool_call_id,
                        "llm_context_note": message.llm_context_note,
                        "metadata": metadata,
                    }
                )
            return MemoryCommit(
                commit_id=f"memory-{normalized_request_id}-{len(messages)}",
                request_id=normalized_request_id,
                base_memory_epoch=self._base_memory_epoch,
                messages=tuple(messages),
            )

    def record_user_message(
        self,
        content: str,
        *,
        created_at: datetime | None = None,
        metadata: dict[str, Any] | None = None,
        llm_context_note: str = "",
    ) -> SessionMessage:
        """记录一条用户消息并返回对应的会话对象。

        Args:
            content: 用户消息正文。
            created_at: 可选显式创建时间；不传时使用会话真实时钟。
            metadata: 可选内部消息标记，不会进入 LLM 协议。
            llm_context_note: 后续 turn 对 LLM 可见的安全事实附注。

        Returns:
            已写入窗口的用户消息对象。
        """

        with self._lock:
            # 为当前消息分配窗口内编号，方便后续摘要压缩裁剪前缀。
            token_count = self.token_counter(protocol_message_token_text(
                role="user",
                content=content,
                llm_context_note=llm_context_note,
            ))
            message = SessionMessage(
                role="user",
                content=content,
                created_at=created_at or self.wall_clock(),
                message_id=self._next_message_id,
                token_count=token_count,
                llm_context_note=llm_context_note.strip(),
                metadata=dict(metadata or {}),
            )
            self._messages.append(message)
            self._next_message_id += 1
            self.context_token_count += token_count
            return message

    def record_assistant_message(
        self,
        content: str,
        *,
        created_at: datetime | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SessionMessage | None:
        """记录一条助手消息，空白内容会直接忽略。

        Args:
            content: 助手可见回复正文。
            created_at: 可选显式创建时间；不传时使用会话真实时钟。
            metadata: 可选内部消息标记，不会进入 LLM 协议。

        Returns:
            已写入窗口的助手消息对象；空白内容返回 None。
        """

        with self._lock:
            # 空白输出没有可复用信息，直接丢弃可以避免污染历史。
            if not content.strip():
                return None

            # 与用户消息共用同一条编号序列，便于摘要裁剪整段历史。
            token_count = self.token_counter(protocol_message_token_text(
                role="assistant",
                content=content,
            ))
            message = SessionMessage(
                role="assistant",
                content=content,
                created_at=created_at or self.wall_clock(),
                message_id=self._next_message_id,
                token_count=token_count,
                metadata=dict(metadata or {}),
            )
            self._messages.append(message)
            self._next_message_id += 1
            self.context_token_count += token_count
            return message

    def record_tool_turn(
        self,
        assistant_message: dict[str, Any],
        tool_messages: Sequence[dict[str, Any]],
        final_assistant_content: str,
        *,
        protocol_transaction_id: str,
        created_at: datetime | None = None,
    ) -> list[SessionMessage]:
        """原子记录完整工具协议和用户实际看到的最终回复。

        Args:
            assistant_message: 携带 tool_calls 的 assistant 协议消息。
            tool_messages: 与 assistant_message 中每个 tool_call 一一对应的 tool 消息。
            final_assistant_content: 本轮用户实际看到的完整助手文本。
            protocol_transaction_id: 工具批次进入编排时已经分配的稳定事务编号。
            created_at: 可选显式创建时间；不传时每条消息使用会话真实时钟。

        Returns:
            已按 assistant、tool 结果和最终 assistant 顺序写入的消息对象列表。
        """

        if not final_assistant_content.strip():
            raise ValueError("完整工具交换必须包含非空 final assistant 回复")
        normalized_transaction_id = protocol_transaction_id.strip()
        if not normalized_transaction_id:
            raise ValueError("完整工具交换必须包含稳定事务编号")
        role, content, tool_calls, tool_call_id = _normalize_protocol_message(
            assistant_message
        )
        tool_calls = project_tool_calls_for_runtime_memory(tool_calls)
        if role != "assistant" or not tool_calls or tool_call_id is not None:
            raise ValueError("工具交换必须以包含 tool_calls 的 assistant 消息开始")

        expected_tool_call_ids = _tool_call_ids_from_tool_calls(tool_calls)
        normalized_tool_messages = [
            _normalize_protocol_message(message) for message in tool_messages
        ]
        _validate_tool_exchange(expected_tool_call_ids, normalized_tool_messages)
        transaction_metadata = {
            "protocol_transaction_id": normalized_transaction_id,
        }
        message_specs: list[ProtocolMessageSpec] = [
            (
                role,
                content,
                tool_calls,
                tool_call_id,
                dict(transaction_metadata),
            )
        ]
        message_specs.extend(
            (
                tool_role,
                tool_content,
                tool_calls_payload,
                current_tool_call_id,
                dict(transaction_metadata),
            )
            for (
                tool_role,
                tool_content,
                tool_calls_payload,
                current_tool_call_id,
            ) in normalized_tool_messages
        )
        message_specs.append(
            (
                "assistant",
                final_assistant_content,
                [],
                None,
                dict(transaction_metadata),
            )
        )

        with self._lock:
            return self._record_protocol_batch_locked(
                message_specs,
                created_at=created_at,
            )

    def record_assistant_reply(
        self,
        *,
        content: str,
        created_at: datetime | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> list[SessionMessage]:
        """原子记录用户实际看到的最终回复。

        Args:
            content: 用户实际看到的非空 assistant 回复。
            created_at: 可选显式创建时间；不传时每条消息使用会话真实时钟。
            metadata: 最终 assistant 消息专用的内部标记。

        Returns:
            只包含最终 assistant 回复的消息列表。
        """

        if not content.strip():
            raise ValueError("最终 assistant 回复不能为空")
        transaction_metadata = {
            "protocol_transaction_id": f"reply-{uuid4().hex}",
        }
        final_metadata = {**transaction_metadata, **dict(metadata or {})}
        message_specs: list[ProtocolMessageSpec] = [
            ("assistant", content, [], None, final_metadata)
        ]

        with self._lock:
            return self._record_protocol_batch_locked(
                message_specs,
                created_at=created_at,
            )

    def record_system_message(
        self,
        *,
        content: str,
        created_at: datetime | None = None,
        metadata: dict[str, Any] | None = None,
        llm_context_note: str = "",
    ) -> list[SessionMessage]:
        """原子记录一条后续 Foreground 可见的系统事实。

        Args:
            content: 已由系统确认的事实正文。
            created_at: 可选事实发生时间。
            metadata: 不改变模型语义的内部归因字段。
            llm_context_note: 给后续模型看的安全附注。

        Returns:
            只包含这条 system 消息的列表。
        """

        if not isinstance(content, str) or not content.strip():
            raise ValueError("系统事实不能为空")
        message_metadata = {
            "protocol_transaction_id": f"system-fact-{uuid4().hex}",
            **dict(metadata or {}),
        }
        system_content = content.strip()
        if llm_context_note.strip():
            system_content = f"{system_content}\n\n{llm_context_note.strip()}"
        message_specs: list[ProtocolMessageSpec] = [
            ("system", system_content, [], None, message_metadata)
        ]
        with self._lock:
            return self._record_protocol_batch_locked(
                message_specs,
                created_at=created_at,
            )

    def _record_protocol_batch_locked(
        self,
        message_specs: Sequence[ProtocolMessageSpec],
        *,
        created_at: datetime | None,
    ) -> list[SessionMessage]:
        """准备完整批次后一次性写入会话窗口。

        Args:
            message_specs: 已归一化的角色、正文、工具字段和消息元数据。
            created_at: 可选显式创建时间。

        Returns:
            已按输入顺序写入的会话消息。
        """

        prepared_messages: list[SessionMessage] = []
        next_message_id = self._next_message_id
        total_token_count = 0
        for role, content, tool_calls, tool_call_id, metadata in message_specs:
            # 任何可能失败的 token 与时间计算都发生在账本突变之前，确保异常时零写入。
            token_count = self.token_counter(protocol_message_token_text(
                role=role,
                content=content,
                tool_calls=tool_calls,
                tool_call_id=tool_call_id,
            ))
            prepared_messages.append(
                SessionMessage(
                    role=role,
                    content=content,
                    created_at=created_at or self.wall_clock(),
                    message_id=next_message_id,
                    token_count=token_count,
                    tool_calls=tool_calls,
                    tool_call_id=tool_call_id,
                    metadata=dict(metadata),
                )
            )
            next_message_id += 1
            total_token_count += token_count

        self._messages.extend(prepared_messages)
        self._next_message_id = next_message_id
        self.context_token_count += total_token_count
        return prepared_messages

    def recent_messages(self) -> list[SessionMessage]:
        """返回当前窗口内的近期消息副本。

        Returns:
            保持窗口顺序的消息列表浅拷贝。
        """

        with self._lock:
            # 只复制容器，不深拷贝消息对象，兼顾安全性和性能。
            return list[SessionMessage](self._messages)

    def visible_dialogue_messages(
        self,
        *,
        exclude_message_id: int | None = None,
        before_message_id: int | None = None,
        limit: int | None = None,
    ) -> list[SessionMessage]:
        """返回可安全交给 System Notice 或工具二轮承接上下文的可见对话。

        System Notice 不需要、也不能读取工具协议、任务记忆或只给模型看的附注；它只需
        知道用户和角色此前真正说过什么。这里返回干净副本，避免调用方误把内部字段重新
        带回 Prompt。

        Args:
            exclude_message_id: 需要排除的消息编号，常用于去掉正在处理的当前用户消息。
            before_message_id: 只返回编号严格早于该消息的可见消息，用于冻结某一轮开始前的历史边界。
            limit: 最多返回多少条最近的可见消息；不传表示不限制，零表示返回空列表。

        Returns:
            保持原始顺序的非空用户消息与纯文本助手消息。

        Raises:
            ValueError: limit 小于零。
        """

        if limit is not None and limit < 0:
            raise ValueError("limit 不能为负数")

        with self._lock:
            visible_messages: list[SessionMessage] = []
            for message in self._messages:
                visible_message = visible_dialogue_message(message)
                if visible_message is None:
                    continue
                if (
                    before_message_id is not None
                    and (
                        visible_message.message_id is None
                        or visible_message.message_id >= before_message_id
                    )
                ):
                    continue
                if (
                    exclude_message_id is not None
                    and visible_message.message_id == exclude_message_id
                ):
                    continue
                visible_messages.append(visible_message)

            # 上限针对过滤后的真实对话计算，避免末尾工具协议挤占可见历史名额。
            if limit is None:
                return visible_messages
            if limit == 0:
                return []
            return visible_messages[-limit:]

    def prompt_window_snapshot(self) -> tuple[str, tuple[SessionMessage, ...]]:
        """原子读取滚动摘要和当前消息窗口。

        Returns:
            当前滚动摘要及保持窗口顺序的消息元组。
        """

        with self._lock:
            # 消息写入后不会原地修改；调用方应立即提取所需字段，不把对象交给后台线程。
            return self.rolling_summary, tuple(self._messages)

    def validate_prompt_history(self, messages: Sequence[SessionMessage]) -> None:
        """校验即将进入提示词的持久历史没有半截工具事务。

        Args:
            messages: 已完成当前用户消息过滤的会话历史。

        Raises:
            ValueError: 工具调用、结果和最终回复不完整时抛出。
        """

        validate_tool_protocol_messages(
            [message.to_llm_message() for message in messages],
            require_final_assistant=True,
        )

def _normalize_tool_calls(value: object) -> list[dict[str, Any]]:
    """复制并清洗 Chat-like tool_calls 字段。

    Args:
        value: 尚未校验的工具调用字段。

    Returns:
        可安全写入历史的工具调用字典列表。
    """

    if not isinstance(value, list):
        return []

    tool_calls: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        tool_calls.append(deepcopy(item))
    return tool_calls


def _normalize_protocol_message(
    message: dict[str, Any],
) -> tuple[str, str, list[dict[str, Any]], str | None]:
    """归一化一条内部 Chat-like 协议消息。

    Args:
        message: 尚未校验的 Chat-like 消息字典。

    Returns:
        角色、正文、工具调用列表和响应调用编号。
    """

    role = str(message.get("role") or "").strip()
    if role not in {"assistant", "tool"}:
        raise ValueError("工具协议消息只允许 assistant 或 tool 角色")

    content = str(message.get("content") or "")
    tool_calls = _normalize_tool_calls(message.get("tool_calls"))
    tool_call_id = _normalize_tool_call_id(message.get("tool_call_id"))
    if role == "assistant" and not content.strip() and not tool_calls:
        raise ValueError("assistant 协议消息必须包含正文或 tool_calls")
    if role == "tool" and not tool_call_id:
        raise ValueError("tool 协议消息必须包含 tool_call_id")
    return role, content, tool_calls, tool_call_id


def _tool_call_ids_from_tool_calls(tool_calls: list[dict[str, Any]]) -> list[str]:
    """从 assistant.tool_calls 中提取并校验工具调用编号。

    Args:
        tool_calls: assistant 声明的工具调用列表。

    Returns:
        保持声明顺序的唯一工具调用编号。
    """

    tool_call_ids: list[str] = []
    seen_ids: set[str] = set()
    for tool_call in tool_calls:
        raw_tool_call_id = tool_call.get("id")
        tool_call_id = raw_tool_call_id.strip() if isinstance(raw_tool_call_id, str) else ""
        if not tool_call_id:
            raise ValueError("assistant tool_calls 必须包含非空 id")
        if tool_call_id in seen_ids:
            raise ValueError(f"重复 tool_call_id: {tool_call_id}")
        seen_ids.add(tool_call_id)
        tool_call_ids.append(tool_call_id)
    return tool_call_ids


def _validate_tool_exchange(
    expected_tool_call_ids: list[str],
    normalized_tool_messages: list[
        tuple[str, str, list[dict[str, Any]], str | None]
    ],
) -> None:
    """校验工具返回消息必须完整响应 assistant 声明。

    Args:
        expected_tool_call_ids: assistant 声明的工具调用编号。
        normalized_tool_messages: 已归一化的 tool 角色消息。

    Raises:
        ValueError: 工具响应缺失、重复或引用未知调用编号时抛出。
    """

    seen_tool_call_ids: set[str] = set()
    expected_tool_call_id_set = set(expected_tool_call_ids)
    for role, _content, tool_calls, tool_call_id in normalized_tool_messages:
        if role != "tool" or tool_calls:
            raise ValueError("工具交换中的后续消息必须全部是 tool 结果")
        if tool_call_id not in expected_tool_call_id_set:
            raise ValueError(f"额外 tool_call_id: {tool_call_id}")
        if tool_call_id in seen_tool_call_ids:
            raise ValueError(f"重复 tool_call_id: {tool_call_id}")
        seen_tool_call_ids.add(tool_call_id)

    missing_tool_call_ids = [
        tool_call_id
        for tool_call_id in expected_tool_call_ids
        if tool_call_id not in seen_tool_call_ids
    ]
    if missing_tool_call_ids:
        raise ValueError(f"缺少 tool_call_id 响应: {', '.join(missing_tool_call_ids)}")


def _normalize_tool_call_id(value: object) -> str | None:
    """清洗 tool 消息关联的工具调用编号。

    Args:
        value: 尚未校验的调用编号字段。

    Returns:
        清洗后的调用编号；字段无效时返回空。
    """

    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None
