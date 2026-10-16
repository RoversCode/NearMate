"""把一项 Activity 的工具权限收敛为一次用户授权。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Literal

from companion.background_agent.interaction_bridge import (
    AgentInteractionAnswer,
    AgentInteractionRequest,
    InteractionBridge,
)


ActivityPermissionGateState = Literal["waiting", "granted", "denied"]
PermissionResolutionOutcome = Literal["allowed", "denied"]


class ActivityPermissionGate:
    """让同一 Activity 的所有 SDK 权限回调共享一次用户决定。

    第一条真实权限请求会创建唯一交互卡；并发回调等待同一个决定。用户允许后，
    本 Activity 后续权限回调直接放行；用户拒绝后，所有等待者都收到中断结果。

    Args:
        interaction_bridge: 当前 Activity 独占的跨线程交互桥。
        resolution_notifier: Gate 真正应用决定后发布权威事实的回调；第三个参数表示
            Foreground 是否已经即时确认这次自然语言授权。
        denial_notifier: SDK 恢复前冻结 Activity 取消意图的回调。
    """

    def __init__(
        self,
        *,
        interaction_bridge: InteractionBridge,
        resolution_notifier: Callable[
            [AgentInteractionRequest, PermissionResolutionOutcome, bool], None
        ],
        denial_notifier: Callable[[], None] | None = None,
    ) -> None:
        """创建一项尚未得到用户决定的 Activity 权限门。

        Args:
            interaction_bridge: 当前 Activity 独占的跨线程交互桥。
            resolution_notifier: 决定生效后的权威事件出口；第三个参数表示
                Foreground 是否已经即时确认这次自然语言授权。
            denial_notifier: 拒绝生效时冻结取消意图的出口。
        """

        self._interaction_bridge = interaction_bridge
        self._resolution_notifier = resolution_notifier
        self._denial_notifier = denial_notifier
        self._state: ActivityPermissionGateState = "waiting"
        self._decision_task: asyncio.Task[AgentInteractionAnswer] | None = None
        self._decision_lock = asyncio.Lock()

    @property
    def state(self) -> ActivityPermissionGateState:
        """返回当前 Activity 的授权状态。

        Returns:
            waiting、granted 或 denied。
        """

        return self._state

    async def request_permission(
        self,
        *,
        tool_name: str,
        input_data: dict[str, object],
        action_summary: str,
    ) -> AgentInteractionAnswer:
        """取得当前 Activity 的工具执行资格。

        Args:
            tool_name: 首次触发授权的 SDK 工具名。
            input_data: 首次工具准备执行的输入，仅供 Desktop 详情使用。
            action_summary: 可进入角色表达的安全操作摘要。

        Returns:
            允许时返回 allow；拒绝或 Activity 收口时返回 cancel。
        """

        async with self._decision_lock:
            if self._state == "granted":
                return _granted_answer()
            if self._state == "denied":
                return _denied_answer()
            if self._decision_task is None:
                self._decision_task = asyncio.create_task(
                    self._request_first_decision(
                        tool_name=tool_name,
                        input_data=input_data,
                        action_summary=action_summary,
                    )
                )
            decision_task = self._decision_task

        # 某个 SDK 并发调用被取消时，不能连带取消其它调用共同等待的用户决定。
        return await asyncio.shield(decision_task)

    async def _request_first_decision(
        self,
        *,
        tool_name: str,
        input_data: dict[str, object],
        action_summary: str,
    ) -> AgentInteractionAnswer:
        """创建唯一授权卡，并把用户决定原子应用到 Gate。

        Args:
            tool_name: 首次触发授权的 SDK 工具名。
            input_data: 首次工具输入。
            action_summary: 首次安全操作摘要。

        Returns:
            供所有等待权限回调共享的规范结果。
        """

        answer = await self._interaction_bridge.request(
            tool_name=tool_name,
            input_data=input_data,
            action_summary=action_summary,
            answer_applied=self._apply_decision,
        )
        if self._state == "granted":
            return AgentInteractionAnswer(
                request_id=answer.request_id,
                outcome="allow",
                answers={},
            )
        if self._state == "denied":
            return _denied_answer(request_id=answer.request_id)
        # Bridge 关闭使用内部 closed 回答，不再发布一条已经不存在的交互解决事件。
        self._state = "denied"
        return AgentInteractionAnswer(
            request_id=answer.request_id,
            outcome="cancel",
            answers={},
            reason=answer.reason or "后台任务已经结束。",
        )

    def _apply_decision(
        self,
        request: AgentInteractionRequest,
        answer: AgentInteractionAnswer,
    ) -> None:
        """在 SDK Future 已命中、但权限回调尚未返回时应用用户决定。

        Args:
            request: 首次授权卡对应的请求。
            answer: 主进程校验并送回的用户决定。
        """

        if self._state != "waiting":
            return
        if answer.outcome == "allow":
            self._state = "granted"
            self._resolution_notifier(
                request,
                "allowed",
                bool(answer.submission_id),
            )
            return

        self._state = "denied"
        if self._denial_notifier is not None:
            self._denial_notifier()
        self._resolution_notifier(request, "denied", False)


def _granted_answer() -> AgentInteractionAnswer:
    """构造 Activity 已授权后的本地放行结果。

    Returns:
        不再创建交互卡的 allow 结果。
    """

    return AgentInteractionAnswer(
        request_id="activity-permission-granted",
        outcome="allow",
        answers={},
    )


def _denied_answer(*, request_id: str = "activity-permission-denied") -> AgentInteractionAnswer:
    """构造会中断当前 Activity 的拒绝结果。

    Args:
        request_id: 首次授权卡编号；后续本地拒绝可使用稳定内部编号。

    Returns:
        带稳定取消原因的 cancel 结果。
    """

    return AgentInteractionAnswer(
        request_id=request_id,
        outcome="cancel",
        answers={},
        reason="permission_denied",
    )
