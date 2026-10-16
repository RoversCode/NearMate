"""保存主进程当前仍可回答的后台智能体交互请求。"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from threading import RLock
from typing import Literal

from companion.background_agent.question_contract import (
    normalize_agent_question_answers,
    normalize_agent_questions,
)


AgentInteractionKind = Literal["permission", "question"]
InteractionClaimState = Literal["pending", "answering"]


class InteractionRequestNotFoundError(LookupError):
    """表示请求不存在，或已经随任务和运行时收口。

    Args:
        request_id: 无法找到的后台交互请求编号。
    """


class InteractionRequestConflictError(RuntimeError):
    """表示请求存在，但当前状态不再允许本次回答。

    Args:
        message: 类型、世代或回答状态冲突的稳定原因。
    """


class InteractionAnswerValidationError(ValueError):
    """表示回答内容不满足当前资料问题契约。

    Args:
        message: 可稳定映射为 HTTP 422 的校验原因。
    """


@dataclass(frozen=True, slots=True)
class AgentInteractionEventApplication:
    """保存 Registry 应用成功后允许跨越 Control 边界的公开事件。

    Args:
        control_event: 需要广播给 Desktop 的白名单事件；内部确认事件为空。
    """

    control_event: dict[str, object] | None = None


AgentInteractionEventApplyResult = AgentInteractionEventApplication | None


@dataclass(frozen=True, slots=True)
class AgentQuestionOption:
    """保存资料问题的一项安全可选答案。

    Args:
        label: 用户可选择的简短答案。
        description: 这项答案的补充解释。
    """

    label: str
    description: str

    def to_payload(self) -> dict[str, object]:
        """转换为 Foreground 与 Control 共用的公开选项。

        Returns:
            只含选项文案和说明的字典。
        """

        return {"label": self.label, "description": self.description}


@dataclass(frozen=True, slots=True)
class AgentQuestionFact:
    """保存 AskUserQuestion 中一项经过白名单清洗的问题。

    Args:
        question: Background Agent 真正需要用户回答的问题。
        header: 面向用户的简短问题类别。
        options: Agent 提供的可选答案。
        multi_select: 是否允许同时选择多个答案。
    """

    question: str
    header: str
    options: tuple[AgentQuestionOption, ...]
    multi_select: bool

    def to_foreground_payload(self) -> dict[str, object]:
        """转换为 Foreground 动态环境使用的公开字典。

        Returns:
            不包含 SDK 其它输入字段的问题事实。
        """

        return {
            "question": self.question,
            "header": self.header,
            "options": [option.to_payload() for option in self.options],
            "multi_select": self.multi_select,
        }

    def to_control_payload(self) -> dict[str, object]:
        """转换为 Desktop 交互卡使用的公开字典。

        Returns:
            使用 Desktop camelCase 字段且不包含 SDK 私有内容的问题事实。
        """

        return {
            "question": self.question,
            "header": self.header,
            "options": [option.to_payload() for option in self.options],
            "multiSelect": self.multi_select,
        }


@dataclass(frozen=True, slots=True)
class PendingAgentInteraction:
    """保存一条主进程可校验的待答请求。

    Args:
        request_id: 后台交互请求编号。
        activity_id: 请求所属 Activity 编号。
        runtime_epoch: 产生请求的助手运行时世代。
        kind: 权限确认或资料问题。
        interaction_session_id: 请求所属的产品交互会话。
        tool_name: 触发请求的 SDK 工具名。
        input_data: 经过边界清洗、可重新渲染权限卡的工具输入。
        action_summary: 只供 Foreground 理解权限目的的安全中文操作类别。
        grant_scope: 权限决定覆盖的固定 Activity 范围；资料问题为空。
        questions: 资料问题经过白名单清洗后的公开事实；权限请求为空。
        state: 尚未提交回答，或回答正在等待子进程确认。
    """

    request_id: str
    activity_id: str
    runtime_epoch: int
    kind: AgentInteractionKind
    interaction_session_id: str = ""
    tool_name: str = ""
    input_data: dict[str, object] = field(default_factory=dict)
    action_summary: str = ""
    grant_scope: Literal["activity"] | None = None
    questions: tuple[AgentQuestionFact, ...] = ()
    state: InteractionClaimState = "pending"

    def to_foreground_payload(self) -> dict[str, object]:
        """生成普通用户轮可读取的工作交互载荷。

        Returns:
            请求类型、请求编号、所属任务和安全展示字段。
        """

        payload: dict[str, object] = {
            "request_id": self.request_id,
            "activity_id": self.activity_id,
            "kind": self.kind,
            "questions": [
                question.to_foreground_payload() for question in self.questions
            ],
        }
        if self.kind == "permission":
            payload["action_summary"] = self.action_summary
            payload["grant_scope"] = "activity"
        return payload

    def to_control_payload(self) -> dict[str, object]:
        """生成控制 WebSocket 可直接补发的交互卡片载荷。

        Returns:
            能让 Desktop 在断线重连后恢复原卡片的完整安全字段。
        """

        payload: dict[str, object] = {
            "request_id": self.request_id,
            "activity_id": self.activity_id,
            "interaction_session_id": self.interaction_session_id,
            "kind": self.kind,
            "tool_name": self.tool_name,
            "input_data": dict(self.input_data),
            "questions": [question.to_control_payload() for question in self.questions],
        }
        if self.kind == "permission":
            payload["action_summary"] = self.action_summary
            payload["grant_scope"] = "activity"
        return payload

    def to_snapshot_control_payload(self) -> dict[str, object]:
        """生成重连快照使用的开放卡片载荷。

        Returns:
            在实时 requested 载荷之外增加 pending 或 processing 状态的字典。
        """

        return {
            **self.to_control_payload(),
            "status": "pending" if self.state == "pending" else "processing",
        }


class BackgroundAgentInteractionRegistry:
    """作为主进程待答请求和运行时世代的唯一真相源。

    Args:
        无。
    """

    def __init__(self) -> None:
        """创建空的进程内请求账本。"""

        self._pending: dict[str, PendingAgentInteraction] = {}
        self._lock = RLock()

    def apply_runtime_event(
        self,
        event_type: str,
        payload: dict[str, object],
        runtime_epoch: int,
    ) -> AgentInteractionEventApplyResult:
        """登记 requested 或收口 resolved 事件。

        Args:
            event_type: 后台智能体交互事件名称。
            payload: 子进程返回的公开事件载荷。
            runtime_epoch: 事件所属助手运行时世代。

        Returns:
            事件无效时返回空；应用成功时返回公开 Control 投影结果。
        """

        request_id = _required_text(payload.get("request_id"))
        activity_id = _required_text(payload.get("activity_id"))
        if not request_id or not activity_id or runtime_epoch <= 0:
            return None

        if event_type == "background_agent.interaction_requested":
            kind = payload.get("kind")
            if kind not in {"permission", "question"}:
                return None
            candidate = PendingAgentInteraction(
                request_id=request_id,
                activity_id=activity_id,
                runtime_epoch=runtime_epoch,
                kind=kind,  # type: ignore[arg-type]
                interaction_session_id=_required_text(
                    payload.get("interaction_session_id")
                ),
                tool_name=_limited_text(payload.get("tool_name"), 100),
                input_data=_safe_input_data(payload.get("input_data")),
                action_summary=(
                    _limited_text(payload.get("action_summary"), 100)
                    if kind == "permission"
                    else ""
                ),
                grant_scope=(
                    "activity"
                    if kind == "permission" and payload.get("grant_scope") == "activity"
                    else None
                ),
                questions=(
                    _parse_question_facts(payload.get("questions"))
                    if kind == "question"
                    else ()
                ),
            )
            if kind == "question" and not candidate.questions:
                return None
            if kind == "permission" and (
                not candidate.action_summary or candidate.grant_scope != "activity"
            ):
                return None
            with self._lock:
                existing = self._pending.get(request_id)
                if existing is None:
                    self._pending[request_id] = candidate
                    return AgentInteractionEventApplication(
                        control_event={
                            "type": "background_agent.interaction_requested",
                            "payload": candidate.to_control_payload(),
                        }
                    )
                return None

        if event_type != "background_agent.interaction_resolved":
            return None
        with self._lock:
            existing = self._pending.get(request_id)
            if (
                existing is None
                or existing.activity_id != activity_id
                or existing.runtime_epoch != runtime_epoch
            ):
                return None
            outcome = payload.get("outcome")
            if outcome not in {"allowed", "denied", "answered", "cancelled"}:
                return None
            self._pending.pop(request_id, None)
            return AgentInteractionEventApplication(
                control_event={
                    "type": "background_agent.interaction_resolved",
                    "payload": {
                        "request_id": existing.request_id,
                        "activity_id": existing.activity_id,
                        "outcome": outcome,
                    },
                }
            )

    def claim_answer(
        self,
        *,
        request_id: str,
        kind: AgentInteractionKind,
        outcome: Literal["allow", "deny", "answer", "cancel"],
        answers: dict[str, str],
        current_runtime_epoch: int,
    ) -> tuple[PendingAgentInteraction, dict[str, str]]:
        """原子取得一次回答权，阻止重复或跨世代下发。

        Args:
            request_id: HTTP 路径中的交互请求编号。
            kind: 请求体声明的交互类型。
            outcome: 用户对权限或资料问题作出的决定。
            answers: 资料问题原文到用户答案的映射。
            current_runtime_epoch: 当前已发布助手运行时世代。

        Returns:
            已转为 answering 状态的请求快照和规范化答案。

        Raises:
            InteractionRequestNotFoundError: 请求不存在或已经收口。
            InteractionRequestConflictError: 类型、世代或回答状态冲突。
            InteractionAnswerValidationError: 资料答案不完整或问题键不匹配。
        """

        normalized_request_id = request_id.strip()
        with self._lock:
            existing = self._pending.get(normalized_request_id)
            if existing is None:
                raise InteractionRequestNotFoundError(normalized_request_id)
            if existing.runtime_epoch != current_runtime_epoch:
                raise InteractionRequestConflictError("interaction runtime epoch expired")
            if existing.kind != kind:
                raise InteractionRequestConflictError("interaction kind mismatch")
            if existing.state != "pending":
                raise InteractionRequestConflictError("interaction answer already submitted")
            if existing.kind == "permission" and outcome not in {
                "allow",
                "deny",
                "cancel",
            }:
                raise InteractionAnswerValidationError(
                    "permission outcome must be allow, deny or cancel"
                )
            if existing.kind == "question" and outcome not in {"answer", "cancel"}:
                raise InteractionAnswerValidationError(
                    "question outcome must be answer or cancel"
                )
            normalized_answers: dict[str, str] = {}
            if existing.kind == "question" and outcome == "answer":
                normalized_answer_result = normalize_agent_question_answers(
                    tuple(question.question for question in existing.questions),
                    answers,
                )
                if normalized_answer_result is None:
                    raise InteractionAnswerValidationError(
                        "question answers must exactly match pending questions"
                    )
                normalized_answers = normalized_answer_result
            claimed = replace(existing, state="answering")
            self._pending[normalized_request_id] = claimed
            return claimed, normalized_answers

    def release_answer_claim(self, request: PendingAgentInteraction) -> bool:
        """命令下发失败时恢复同一请求的回答权。

        Args:
            request: 此前由 `claim_answer` 返回的请求快照。

        Returns:
            当前仍是同一 answering 请求并成功恢复时返回真。
        """

        with self._lock:
            existing = self._pending.get(request.request_id)
            if existing != request or existing.state != "answering":
                return False
            self._pending[request.request_id] = replace(existing, state="pending")
            return True

    def invalidate_runtime_epoch(self, runtime_epoch: int) -> list[dict[str, object]]:
        """作废旧世代的全部待答请求并生成卡片收口事件。

        Args:
            runtime_epoch: 已经失效的助手运行时世代。

        Returns:
            需要广播给 Desktop 的 `interaction_resolved` 事件列表。
        """

        with self._lock:
            invalidated = [
                request
                for request in self._pending.values()
                if request.runtime_epoch == runtime_epoch
            ]
            for request in invalidated:
                self._pending.pop(request.request_id, None)
        return [
            {
                "type": "background_agent.interaction_resolved",
                "payload": {
                    "request_id": request.request_id,
                    "activity_id": request.activity_id,
                    "outcome": "cancelled",
                },
            }
            for request in invalidated
        ]

    def pending(self, request_id: str) -> PendingAgentInteraction | None:
        """返回测试和诊断使用的不可变请求快照。

        Args:
            request_id: 需要查询的交互请求编号。

        Returns:
            当前请求快照；不存在时为空。
        """

        with self._lock:
            return self._pending.get(request_id.strip())

    def foreground_interactions(
        self,
        interaction_session_id: str,
    ) -> list[dict[str, object]]:
        """读取当前会话仍可由自然语言回答的工作交互。

        已经进入 answering 的请求不会重复投影。权限请求只提供安全中文操作摘要，
        原始工具名、命令、参数和路径只留在 Desktop 控制卡片。

        Args:
            interaction_session_id: 当前产品交互会话编号。

        Returns:
            按登记顺序排列的安全权限或资料请求载荷。
        """

        normalized_session_id = interaction_session_id.strip()
        with self._lock:
            return [
                request.to_foreground_payload()
                for request in self._pending.values()
                if request.state == "pending"
                and request.interaction_session_id == normalized_session_id
            ]

    def pending_control_snapshot_event(self) -> dict[str, object]:
        """读取当前仍开放的交互卡片完整快照。

        `pending` 请求仍可操作，`answering` 请求投影为 `processing`。Desktop 以这份
        开放集合替换本地 pending/processing 卡，才能在断线错过 resolved 后删除旧卡。

        Returns:
            包含全部 pending 卡片的单条 Control 快照事件。
        """

        with self._lock:
            return {
                "type": "background_agent.interactions_snapshot",
                "payload": {
                    "items": [
                        request.to_snapshot_control_payload()
                        for request in self._pending.values()
                    ]
                },
            }


def _required_text(value: object) -> str:
    """只接受非空字符串身份字段。

    Args:
        value: 需要规范化的来源事件字段。

    Returns:
        裁剪后的字符串；输入不是字符串时返回空字符串。
    """

    return value.strip() if isinstance(value, str) else ""


def _limited_text(value: object, limit: int) -> str:
    """读取允许为空且长度受控的公开文本。"""

    text = value.strip() if isinstance(value, str) else ""
    return text[:limit]


def _parse_question_facts(value: object) -> tuple[AgentQuestionFact, ...]:
    """从 SDK 公开事件中提取有限且不可执行的问题事实。

    Args:
        value: `questions` 原始字段。

    Returns:
        最多十项、字段经过长度限制的问题事实。
    """

    normalized_questions = normalize_agent_questions(value)
    if normalized_questions is None:
        return ()
    facts: list[AgentQuestionFact] = []
    for raw_question in normalized_questions:
        raw_options = raw_question["options"]
        options: list[AgentQuestionOption] = []
        for raw_option in raw_options:
            options.append(
                AgentQuestionOption(
                    label=str(raw_option["label"]),
                    description=str(raw_option["description"]),
                )
            )
        facts.append(
            AgentQuestionFact(
                question=str(raw_question["question"]),
                header=str(raw_question["header"]),
                options=tuple(options),
                multi_select=raw_question["multiSelect"] is True,
            )
        )
    return tuple(facts)


def _safe_input_data(value: object) -> dict[str, object]:
    """只保留可安全重新编码的权限卡输入，避免坏对象阻断重连首帧。"""

    if not isinstance(value, dict):
        return {}
    return _safe_json_mapping(value, depth=0)


def _safe_json_mapping(value: dict[object, object], *, depth: int) -> dict[str, object]:
    """递归清洗一个 JSON 对象，并限制键数量和键长度。"""

    sanitized: dict[str, object] = {}
    for key, item in list(value.items())[:100]:
        if not isinstance(key, str) or not key.strip():
            continue
        safe_value = _safe_json_value(item, depth=depth + 1)
        if safe_value is not _UNSAFE_JSON_VALUE:
            sanitized[key.strip()[:200]] = safe_value
    return sanitized


_UNSAFE_JSON_VALUE = object()


def _safe_json_value(value: object, *, depth: int = 0) -> object:
    """递归清洗权限卡输入中的 JSON 基础类型和有限容器。"""

    if depth > 4:
        return _UNSAFE_JSON_VALUE
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:4000]
    if isinstance(value, list):
        return [
            safe_item
            for item in value[:100]
            if (safe_item := _safe_json_value(item, depth=depth + 1)) is not _UNSAFE_JSON_VALUE
        ]
    if isinstance(value, dict):
        return _safe_json_mapping(value, depth=depth)
    return _UNSAFE_JSON_VALUE
