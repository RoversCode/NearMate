"""把 SDK 的运行时权限和问题请求桥接到 NearMate 控制面。"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from threading import Lock
from typing import Any, Callable, Literal

from companion.background_agent.contracts import AgentMaterial
from companion.background_agent.question_contract import normalize_agent_questions
from companion.observability.logging_config import get_backend_logger


logger = get_backend_logger(__name__)


@dataclass(frozen=True, slots=True)
class AgentInteractionRequest:
    """保存一条等待用户处理的后台交互请求。

    Args:
        request_id: NearMate 交互请求编号。
        activity_id: 所属 Activity 编号。
        kind: permission 表示权限审批，question 表示资料问题。
        tool_name: 触发请求的 SDK 工具名。
        input_data: 已截取且 JSON 友好的工具输入。
        action_summary: 允许 Foreground 转述的固定中文操作类别。
        grant_scope: 权限请求授权的固定 Activity 范围；资料问题为空。
        questions: AskUserQuestion 的问题列表。
    """

    request_id: str
    activity_id: str
    kind: Literal["permission", "question"]
    tool_name: str
    input_data: dict[str, object]
    action_summary: str
    grant_scope: Literal["activity"] | None
    questions: list[dict[str, object]]


@dataclass(frozen=True, slots=True)
class AgentInteractionAnswer:
    """保存主进程送回的一次用户回答。

    Args:
        request_id: NearMate 交互请求编号。
        outcome: 用户允许、拒绝、回答或取消的决定。
        answers: 资料问题原文到用户答案的映射。
        reason: 用户填写或产品生成的拒绝、取消原因。
        user_text: Foreground 收到的原始用户话语；桌面点击时为空。
        submission_id: 自然语言回答对应的 Foreground 提交编号；桌面点击时为空。
        materials: 本次回答成功接纳、需要同时告诉 Claude 的新增正式材料。
    """

    request_id: str
    outcome: Literal["allow", "deny", "answer", "cancel"]
    answers: dict[str, str]
    reason: str = ""
    user_text: str = ""
    submission_id: str | None = None
    materials: tuple[AgentMaterial, ...] = ()


@dataclass(frozen=True, slots=True)
class _PendingInteraction:
    """保存一条待答请求恢复 SDK 所需的全部上下文。

    Args:
        request: 已经通知主进程的原始交互请求。
        future: SDK 事件循环中等待回答的 Future。
        answer_applied_notifier: 正常回答恢复 Future 后需要调用的权威事件出口。
    """

    request: AgentInteractionRequest
    future: asyncio.Future[AgentInteractionAnswer]
    answer_applied_notifier: Callable[
        [AgentInteractionRequest, AgentInteractionAnswer], None
    ] | None


class InteractionBridge:
    """让 SDK 回调可以跨线程等待主进程用户回答。

    回调所在的 asyncio 事件循环由 Agent Runner 独占；主进程命令通过
    ``answer_from_runtime_thread`` 安全地把结果投递回这个事件循环。

    Args:
        activity_id: 当前后台 Activity 编号。
        request_notifier: 新请求产生后的 Runtime 通知函数。
    """

    def __init__(
        self,
        *,
        activity_id: str,
        request_notifier: Callable[[AgentInteractionRequest], None],
        answer_applied_notifier: Callable[
            [AgentInteractionRequest, AgentInteractionAnswer], None
        ]
        | None = None,
    ) -> None:
        """初始化交互桥。

        Args:
            activity_id: 当前后台 Activity 编号。
            request_notifier: 新请求产生后向 Runtime 事件账本发送通知的函数。
            answer_applied_notifier: 正常回答命中后、SDK 调用返回前的默认权威事件出口。
        """

        self._activity_id = activity_id
        self._request_notifier = request_notifier
        self._answer_applied_notifier = answer_applied_notifier
        self._loop: asyncio.AbstractEventLoop | None = None
        self._pending: dict[str, _PendingInteraction] = {}
        self._pending_lock = Lock()
        self._closed = False

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """记录 SDK 所在的事件循环。

        Args:
            loop: Agent Runner 专用的 asyncio 事件循环。

        Returns:
            无。
        """

        self._loop = loop

    async def request(
        self,
        *,
        tool_name: str,
        input_data: dict[str, object],
        action_summary: str = "",
        questions: list[dict[str, object]] | None = None,
        answer_applied: Callable[
            [AgentInteractionRequest, AgentInteractionAnswer], None
        ]
        | None = None,
    ) -> AgentInteractionAnswer:
        """挂起 SDK 回调并等待用户回答。

        Args:
            tool_name: 触发等待的 SDK 工具名。
            input_data: 需要展示或回传的工具输入。
            action_summary: 权限请求允许进入 Foreground 的固定中文操作摘要。
            questions: AskUserQuestion 的问题列表。
            answer_applied: 正常回答命中后、SDK 调用返回前执行的权威状态回调。

        Returns:
            用户允许、拒绝、回答或取消的结果。
        """

        normalized_questions: list[dict[str, object]] = []
        if tool_name == "AskUserQuestion":
            parsed_questions = normalize_agent_questions(questions)
            if parsed_questions is None:
                return AgentInteractionAnswer(
                    request_id=str(uuid.uuid4()),
                    outcome="cancel",
                    answers={},
                    reason="后台资料问题格式无效。",
                )
            normalized_questions = [dict(question) for question in parsed_questions]

        loop = asyncio.get_running_loop()
        self.bind_loop(loop)
        request_id = str(uuid.uuid4())
        kind: Literal["permission", "question"] = (
            "question" if tool_name == "AskUserQuestion" else "permission"
        )
        request = AgentInteractionRequest(
            request_id=request_id,
            activity_id=self._activity_id,
            kind=kind,
            tool_name=tool_name,
            input_data=_truncate_json_mapping(input_data),
            action_summary=action_summary.strip(),
            grant_scope=("activity" if kind == "permission" else None),
            questions=normalized_questions,
        )
        future: asyncio.Future[AgentInteractionAnswer] = loop.create_future()
        pending_interaction = _PendingInteraction(
            request=request,
            future=future,
            answer_applied_notifier=answer_applied or self._answer_applied_notifier,
        )
        with self._pending_lock:
            # close 可能与 SDK 回调并发发生；登记和关闭必须共享同一把锁，
            # 否则请求会在 close 完成后才进入字典，永远没有人解决它。
            if self._closed:
                return AgentInteractionAnswer(
                    request_id=request_id,
                    outcome="cancel",
                    answers={},
                    reason="后台任务已经结束。",
                )
            self._pending[request_id] = pending_interaction
        try:
            self._request_notifier(request)
        except Exception:
            with self._pending_lock:
                self._pending.pop(request_id, None)
            future.cancel()
            return AgentInteractionAnswer(
                request_id=request_id,
                outcome="deny",
                answers={},
                reason="后台交互通知无法送达。",
            )
        # 回答应用动作由同一个事件循环回调在 Future 恢复后执行。这样等待协程被取消时，
        # Bridge 仍握有原请求上下文，可以用同一请求编号关闭主进程 Registry。
        return await future

    def answer_from_runtime_thread(
        self,
        answer: AgentInteractionAnswer,
        *,
        before_resolve: Callable[[], None] | None = None,
    ) -> bool:
        """从助手子进程命令处理线程解决一条挂起 Future。

        Args:
            answer: 主进程校验过的用户回答。
            before_resolve: 在 SDK Future 恢复前、同一事件循环回调中执行的状态冻结动作。

        Returns:
            找到仍在等待的请求并成功安排解决时返回真。
        """

        with self._pending_lock:
            loop = self._loop
            if loop is None or self._closed:
                return False
            pending_interaction = self._pending.get(answer.request_id)
            if pending_interaction is None or pending_interaction.future.done():
                return False
            future = pending_interaction.future

            def resolve() -> None:
                """在 SDK 事件循环中恢复等待并发布唯一权威结果。

                Returns:
                    无。
                """

                resolved_answer = answer
                applied_notifier = pending_interaction.answer_applied_notifier
                if future.done():
                    # 等待 SDK 回调已经取消时，原用户决定不能再改变 Gate 或材料范围；
                    # 仍需用原请求编号发布 cancelled，避免 Registry 永久停在 answering。
                    resolved_answer = AgentInteractionAnswer(
                        request_id=answer.request_id,
                        outcome="cancel",
                        answers={},
                        reason="后台交互等待已经结束。",
                    )
                    applied_notifier = self._answer_applied_notifier
                else:
                    try:
                        if before_resolve is not None:
                            before_resolve()
                    except Exception:
                        # 前置状态没能落地时不能继续执行原决定，也不能让 SDK Future 永久悬挂。
                        logger.exception(
                            "后台交互回答的前置状态应用失败 request_id=%s",
                            answer.request_id,
                        )
                        resolved_answer = AgentInteractionAnswer(
                            request_id=answer.request_id,
                            outcome="cancel",
                            answers={},
                            reason="后台交互状态应用失败。",
                        )
                        applied_notifier = self._answer_applied_notifier
                    future.set_result(resolved_answer)

                if applied_notifier is None:
                    return
                try:
                    applied_notifier(pending_interaction.request, resolved_answer)
                except Exception:
                    logger.exception(
                        "后台交互回答的权威结果发布失败 request_id=%s",
                        answer.request_id,
                    )

            # 调度与所有权转移必须处于同一把锁内。调度失败时请求仍留在待答表，
            # 随后的 close() 才能取得所有权并发布 cancelled。
            try:
                loop.call_soon_threadsafe(resolve)
            except RuntimeError:
                logger.exception(
                    "后台交互回答无法进入 SDK 事件循环 request_id=%s",
                    answer.request_id,
                )
                return False
            self._pending.pop(answer.request_id, None)
        return True

    def close(
        self,
        reason: str = "后台任务已经结束。",
        *,
        before_resolve: Callable[[tuple[str, ...]], None] | None = None,
    ) -> tuple[str, ...]:
        """结束任务时解决全部挂起请求，避免 SDK 回调永久泄漏。

        Args:
            reason: 交给 SDK 的取消说明。
            before_resolve: 在任何 SDK Future 恢复前处理全部挂起请求编号的回调。

        Returns:
            本次关闭实际收口的挂起请求编号。
        """

        with self._pending_lock:
            if self._closed:
                return ()
            self._closed = True
            pending_items = tuple(self._pending.items())
            # close() 已取得这些 Future 的唯一所有权，迟到回答不能再命中它们。
            self._pending.clear()
        pending_request_ids = tuple(request_id for request_id, _pending in pending_items)

        loop = self._loop

        def resolve_all() -> None:
            """在 SDK 事件循环中取消全部挂起请求。

            Returns:
                无。
            """

            answer = AgentInteractionAnswer(
                request_id="closed",
                outcome="cancel",
                answers={},
                reason=reason,
            )
            for _request_id, pending_interaction in pending_items:
                future = pending_interaction.future
                if not future.done():
                    future.set_result(answer)

        # 待答卡片属于主进程和 UI 的权威事实。先让 Runner 发布所有 resolved，
        # 再把恢复动作排进 SDK 事件循环，终态才不可能抢在卡片收口之前出现。
        # 即使事件出口本身抛错，也必须在 finally 中恢复 Future，避免泄漏 SDK 回调。
        try:
            if before_resolve is not None:
                before_resolve(pending_request_ids)
        finally:
            if loop is not None:
                loop.call_soon_threadsafe(resolve_all)
        return pending_request_ids


def _truncate_json_mapping(value: dict[str, object], limit: int = 4000) -> dict[str, object]:
    """截断权限卡片输入，避免把完整文件内容或命令输出广播给用户界面。

    Args:
        value: 需要进入控制事件的工具输入。
        limit: 单个字符串最多保留的字符数。

    Returns:
        只保留有限字符串和集合长度的 JSON 友好字典。
    """

    result: dict[str, object] = {}
    for key, item in value.items():
        if isinstance(item, str) and len(item) > limit:
            result[key] = item[:limit] + "..."
        elif isinstance(item, list):
            result[key] = item[:20]
        elif isinstance(item, dict):
            result[key] = _truncate_json_mapping(item, limit)
        else:
            result[key] = item
    return result
