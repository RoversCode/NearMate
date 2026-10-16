"""管理可复用的 Claude Code 会话，以及每次 Activity 的临时 Turn。"""

from __future__ import annotations

import asyncio
import copy
import dataclasses
import inspect
import threading
import uuid
from collections.abc import Awaitable
from contextlib import aclosing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal

from claude_agent_sdk import ClaudeSDKClient
from claude_agent_sdk.types import ResultMessage, UserMessage

from companion.background_agent.interaction_bridge import (
    AgentInteractionAnswer,
    AgentInteractionRequest,
    InteractionBridge,
)
from companion.background_agent.contracts import AgentMaterial
from companion.background_agent.material_access import AgentSessionMaterialCapability
from companion.background_agent.permission_gate import (
    ActivityPermissionGate,
    PermissionResolutionOutcome,
)
from companion.observability.logging_config import get_backend_logger


logger = get_backend_logger(__name__)


SessionPolicy = Literal["new_session", "resume_required"]
SessionHealth = Literal["new", "connecting", "ready", "busy", "closed", "broken"]


def _consume_background_disconnect(task: asyncio.Task[None]) -> None:
    """消费超时后继续运行的 Session 断开任务，避免异常变成未处理警告。

    Args:
        task: 已经脱离同步关闭预算、仍由事件循环执行的断开任务。

    Returns:
        无；任务异常只记录诊断，不影响其它 Session 的清理。
    """

    try:
        task.result()
    except asyncio.CancelledError:
        logger.warning("后台 AgentSession 断开任务被事件循环取消")
    except Exception:
        logger.exception("后台 AgentSession 断开任务失败")


@dataclass(frozen=True, slots=True)
class AgentSessionTurn:
    """保存一个 Activity Turn 独占且不可复用的 SDK 回调能力。

    Args:
        turn_token: 不可复用的随机能力令牌，所有 SDK 回调必须携带。
        activity_id: 当前 Activity 编号。
        bridge: 当前 Activity 的待答请求桥。
        permission_gate: 当前 Activity 的权限门。
        material_access: 当前 AgentSession 累计材料能力。
        workspace_root: 当前会话绑定的工作区根目录。
        current_scene_path: 本轮冻结截图路径。
    """

    turn_token: str
    activity_id: str
    bridge: InteractionBridge
    permission_gate: ActivityPermissionGate
    material_access: AgentSessionMaterialCapability
    workspace_root: str
    current_scene_path: str | None = None


@dataclass(frozen=True, slots=True)
class AgentSessionResumeIdentity:
    """冻结 Background Correction 恢复旧 SDK 对话所需的身份。

    Args:
        agent_session_id: NearMate 产品级 AgentSession 编号。
        sdk_session_id: Claude SDK init 已经确认的真实会话编号。
        workspace_root: 会话唯一绑定的工作区绝对路径。
    """

    agent_session_id: str
    sdk_session_id: str
    workspace_root: str


@dataclass(frozen=True, slots=True)
class AgentQueryDispatch:
    """冻结一次已经越过 Session 最终门禁的 SDK query 现场。

    Args:
        activity_id: 当前 query 所属的 Activity 编号；无 Turn 的内部调用为空。
        prompt: 去除首尾空白后真正交给 SDK 的任务文本。
        options: 当前 Client 连接时实际生效的 SDK options 对象。
    """

    activity_id: str | None
    prompt: str
    options: Any


AgentQueryDispatcher = Callable[
    [ClaudeSDKClient, AgentQueryDispatch],
    Awaitable[None],
]


async def dispatch_agent_query_directly(
    client: ClaudeSDKClient,
    dispatch: AgentQueryDispatch,
) -> None:
    """不附加旁路行为，直接把规范 query 写入 SDK transport。

    Args:
        client: 已经连接的 Claude SDK Client。
        dispatch: 已经通过 Session 最终门禁的请求现场。

    Returns:
        无；SDK 异常或取消原样传播。
    """

    await client.query(dispatch.prompt)


class AgentSession:
    """让同一个产品会话串行承载多个 Activity Turn。

    Session 保存 SDK 会话编号和对话连续性；Activity 只保存一次具体目标。普通任务建立
    全新 SDK 会话；只有 Background Correction 的新 Turn 会重建独占回调 transport，
    并通过严格 SDK resume 继续引用父任务的工具结果和决定。

    Args:
        agent_session_id: NearMate 产品级会话编号。
        workspace_root: 会话绑定的工作区绝对路径。
        client_factory: 创建 ClaudeSDKClient 的工厂，测试时可注入替身。
        query_dispatcher: query 越过最终门禁后的异步提交器。
    """

    def __init__(
        self,
        *,
        agent_session_id: str | None = None,
        workspace_root: str | Path,
        client_factory: Callable[[Any], ClaudeSDKClient] = ClaudeSDKClient,
        query_dispatcher: AgentQueryDispatcher = dispatch_agent_query_directly,
    ) -> None:
        """初始化一个尚未连接的后台智能体会话。

        Args:
            agent_session_id: 可选的产品级会话编号；首次创建时自动生成。
            workspace_root: 该会话唯一允许绑定的工作区根目录。
            client_factory: 创建 SDK Client 的工厂，测试可注入替身。
            query_dispatcher: query 越过最终门禁后的异步提交器。

        Raises:
            ValueError: 工作区或显式会话编号为空。
        """

        normalized_root = str(Path(workspace_root).resolve(strict=False))
        if not normalized_root:
            raise ValueError("AgentSession 必须绑定工作区")
        self.agent_session_id = (agent_session_id or str(uuid.uuid4())).strip()
        if not self.agent_session_id:
            raise ValueError("agent_session_id 不能为空")
        self.workspace_root = normalized_root
        self._client_factory = client_factory
        self._query_dispatcher = query_dispatcher
        self._client: ClaudeSDKClient | None = None
        self._options_fingerprint: object | None = None
        self._effective_options: Any | None = None
        self.sdk_session_id: str | None = None
        self.health: SessionHealth = "new"
        self.turn: AgentSessionTurn | None = None
        self._material_capability = AgentSessionMaterialCapability(())
        self.last_checkpoint_uuid: str | None = None
        self.last_user_message_uuid: str | None = None
        self._control_lock = asyncio.Lock()
        self._state_lock = threading.RLock()
        self._response_open = False
        self._response_reader_active = False
        self._response_interrupt_requested = False
        self._prequery_interrupted_activity_id: str | None = None
        self._retired_turn_tokens: set[str] = set()
        self._client_turn_token: str | None = None
        self._response_turn_token: str | None = None

    def material_catalog(self) -> tuple[AgentMaterial, ...]:
        """返回会话累计保存的完整材料目录。

        Returns:
            包含当前可读和已失效材料事实的有序目录。
        """

        return self._material_capability.catalog()

    def resume_identity(self) -> AgentSessionResumeIdentity | None:
        """读取当前会话可供 Background Correction 冻结的恢复身份。

        Returns:
            会话未关闭、未损坏且已有 SDK 编号时返回身份；否则返回空。
        """

        with self._state_lock:
            sdk_session_id = self.sdk_session_id
            if self.health in {"closed", "broken"} or not sdk_session_id:
                return None
            return AgentSessionResumeIdentity(
                agent_session_id=self.agent_session_id,
                sdk_session_id=sdk_session_id,
                workspace_root=self.workspace_root,
            )

    def material_limitations(self) -> tuple[str, ...]:
        """返回当前目录中无法读取的材料名称。

        Returns:
            当前文件不存在或没有本地路径的材料名称。
        """

        return self._material_capability.limitations()

    def extend_materials(self, materials: tuple[AgentMaterial, ...]) -> None:
        """把用户本轮补充的正式材料加入累计读取能力。

        Args:
            materials: 已通过材料系统接纳的新增材料。

        Returns:
            无返回值。
        """

        self._material_capability.extend(materials)

    @property
    def client(self) -> ClaudeSDKClient | None:
        """返回当前 SDK Client。

        Returns:
            已连接或正在恢复的 Client；会话尚未连接时返回空。
        """

        return self._client

    def bind_turn(
        self,
        *,
        activity_id: str,
        bridge: InteractionBridge,
        permission_gate: ActivityPermissionGate,
        material_catalog: tuple[AgentMaterial, ...],
        current_scene_path: str | None = None,
    ) -> AgentSessionTurn:
        """创建并绑定当前 Activity 独占的 SDK 回调能力。

        Args:
            activity_id: 新 Turn 的 Activity 编号。
            bridge: 新 Turn 使用的待答请求桥。
            permission_gate: 新 Turn 使用的权限门。
            material_catalog: 新 Turn 需要纳入会话能力的完整材料目录。

        Raises:
            ValueError: Activity 编号为空。
            RuntimeError: 旧 Turn 的 query 或 response reader 尚未完成。

        Returns:
            供 options 回调闭包冻结使用的不可变 Turn Capability。
        """

        normalized_activity_id = activity_id.strip()
        if not normalized_activity_id:
            raise ValueError("Activity 编号不能为空")
        with self._state_lock:
            if self._response_open or self._response_reader_active:
                raise RuntimeError("旧 AgentSession Turn 尚未完成清理")
            previous_turn = self.turn
            if previous_turn is not None:
                self._retired_turn_tokens.add(previous_turn.turn_token)
                previous_turn.bridge.close("AgentSession 已切换到新的 Activity。")
            self._material_capability.extend(material_catalog)
            turn = AgentSessionTurn(
                turn_token=str(uuid.uuid4()),
                activity_id=normalized_activity_id,
                bridge=bridge,
                permission_gate=permission_gate,
                material_access=self._material_capability,
                workspace_root=self.workspace_root,
                current_scene_path=current_scene_path,
            )
            self.turn = turn
            # 提前取消只属于当时绑定的 Activity；切换到新 Turn 后不能把旧取消
            # 误用到新的 query。
            self._prequery_interrupted_activity_id = None
            return turn

    async def request_interaction(
        self,
        *,
        tool_name: str,
        input_data: dict[str, object],
        action_summary: str = "",
        questions: list[dict[str, object]] | None = None,
        turn_token: str | None,
    ) -> AgentInteractionAnswer:
        """把 SDK 的待答请求路由给当前 Activity。

        Args:
            tool_name: 发起请求的 SDK 工具名。
            input_data: 工具准备使用的原始输入。
            action_summary: 允许展示给用户的安全操作概括。
            questions: 已经过产品契约清洗的资料问题。
            turn_token: options 闭包冻结的 Turn Capability；缺失或退休时拒绝。

        Returns:
            当前 Activity 的用户回答；没有活动 Turn 时返回取消答案。
        """

        turn = self.callback_turn(turn_token)
        if turn is None:
            return AgentInteractionAnswer(
                request_id=str(uuid.uuid4()),
                outcome="cancel",
                answers={},
                reason="AgentSession 没有活动 Turn。",
            )
        return await turn.bridge.request(
            tool_name=tool_name,
            input_data=input_data,
            action_summary=action_summary,
            questions=questions,
        )

    async def request_permission(
        self,
        *,
        tool_name: str,
        input_data: dict[str, object],
        action_summary: str,
        turn_token: str | None,
    ) -> AgentInteractionAnswer:
        """把 SDK 权限请求交给当前 Activity 的权限门。

        Args:
            tool_name: 需要用户决定的 SDK 工具名。
            input_data: 工具准备执行的输入。
            action_summary: 不含命令和路径的安全操作概括。
            turn_token: options 闭包冻结的 Turn Capability；缺失或退休时拒绝。

        Returns:
            当前 Activity 的授权决定；没有活动 Turn 时返回取消答案。
        """

        turn = self.callback_turn(turn_token)
        if turn is None:
            return AgentInteractionAnswer(
                request_id=str(uuid.uuid4()),
                outcome="cancel",
                answers={},
                reason="AgentSession 没有活动 Turn。",
            )
        return await turn.permission_gate.request_permission(
            tool_name=tool_name,
            input_data=input_data,
            action_summary=action_summary,
        )

    def callback_turn(self, turn_token: str | None) -> AgentSessionTurn | None:
        """用不可伪造的能力令牌解析当前 SDK 回调所属 Turn。

        Args:
            turn_token: options 闭包在创建 transport 时冻结的能力令牌。

        Returns:
            令牌属于当前未退休 Turn 时返回其不可变 Adapter；否则返回空。
        """

        normalized_token = turn_token.strip() if isinstance(turn_token, str) else ""
        if not normalized_token:
            return None
        with self._state_lock:
            turn = self.turn
            if (
                turn is None
                or normalized_token in self._retired_turn_tokens
                or turn.turn_token != normalized_token
            ):
                return None
            return turn

    def material_capabilities_for_turn(
        self,
        turn_token: str | None,
    ) -> tuple[AgentMaterial, ...] | None:
        """原子校验 Turn 身份并冻结它当下可用的材料能力。

        token 校验和路径快照必须共享 Session 状态锁。否则旧 Hook 可能先通过
        token 校验，随后 B 绑定并扩展累计材料目录，最后旧 Hook 才读取到 B 的路径。

        Args:
            turn_token: options 回调闭包冻结的 Turn Capability。

        Returns:
            当前 Turn 的稳定路径和节点类型快照；token 缺失、退休或不匹配时返回空值。
        """

        normalized_token = turn_token.strip() if isinstance(turn_token, str) else ""
        if not normalized_token:
            return None
        with self._state_lock:
            turn = self.turn
            if (
                turn is None
                or normalized_token in self._retired_turn_tokens
                or turn.turn_token != normalized_token
            ):
                return None
            # bind_turn() 也先取得 _state_lock 再扩展同一个累计目录。路径快照完成前，
            # 新 Turn 因此无法把自己的材料插入旧 Hook 正在使用的授权视图。
            return self._material_capability.capabilities()

    def retire_turn(self, turn_token: str | None) -> bool:
        """退休一个已完成 Turn，使全部迟到 SDK 回调立即失效。

        Args:
            turn_token: 需要退休的当前 Turn Capability。

        Returns:
            本次确实退休当前 Turn 时返回真；缺失、旧令牌或重复退休返回假。
        """

        normalized_token = turn_token.strip() if isinstance(turn_token, str) else ""
        if not normalized_token:
            return False
        with self._state_lock:
            turn = self.turn
            if turn is None or turn.turn_token != normalized_token:
                return False
            self._retired_turn_tokens.add(normalized_token)
            self.turn = None
        turn.bridge.close("AgentSession Turn 已结束。")
        return True

    def _validate_turn_token_locked(self, turn_token: str | None) -> str:
        """在状态锁内确认内部调用绑定到当前 Turn。

        Args:
            turn_token: Runner 从 `bind_turn()` 得到并原样传回的能力令牌。

        Returns:
            当前有效令牌；低层连接测试没有绑定 Turn 时返回空串。

        Raises:
            RuntimeError: 已有活动 Turn，但令牌缺失、退休或不匹配。
        """

        normalized_token = turn_token.strip() if isinstance(turn_token, str) else ""
        turn = self.turn
        if turn is None:
            if normalized_token:
                raise RuntimeError("AgentSession Turn Capability 已失效")
            return ""
        if (
            not normalized_token
            or normalized_token in self._retired_turn_tokens
            or turn.turn_token != normalized_token
        ):
            raise RuntimeError("AgentSession Turn Capability 不匹配")
        return normalized_token

    async def ensure_connected(
        self,
        options: Any,
        *,
        policy: SessionPolicy = "new_session",
        turn_token: str | None = None,
    ) -> ClaudeSDKClient:
        """按策略连接或重建 Client，并严格执行 SDK 会话恢复要求。

        Args:
            options: ClaudeAgentOptions 实例。
            policy: 本次任务使用全新会话，或必须恢复指定 SDK 会话。
            turn_token: 当前 options 回调闭包冻结的 Turn Capability。

        Returns:
            已连接且可以发送 query 的 Client。

        Raises:
            RuntimeError: 会话正在忙、已关闭、已损坏，或无法设置 SDK resume。
        """

        requested_fingerprint = _options_fingerprint(options)
        async with self._control_lock:
            with self._state_lock:
                normalized_turn_token = self._validate_turn_token_locked(turn_token)
                if self.health == "closed":
                    raise RuntimeError("AgentSession 已关闭")
                if self._response_open:
                    raise RuntimeError("AgentSession 仍有未收口的 query")
                client = self._client
                sdk_session_id = self.sdk_session_id
                current_fingerprint = self._options_fingerprint
                client_turn_token = self._client_turn_token
            if policy == "new_session":
                # 显式 new_session 不能悄悄沿用旧上下文；只关闭 transport，产品级
                # Session 对象仍可继续作为新的 SDK 会话容器使用。
                try:
                    if client is not None:
                        await client.disconnect()
                finally:
                    self._client = None
                    self.sdk_session_id = None
                    self._options_fingerprint = None
                    self._effective_options = None
                    self._client_turn_token = None
                    client = None
                    sdk_session_id = None
                    self.health = "new"
            elif self.health == "broken":
                raise RuntimeError("要求恢复的 AgentSession transport 已损坏")
            elif sdk_session_id is None:
                raise RuntimeError("没有可恢复的 SDK session_id")

            if client is not None and self.health == "ready":
                if (
                    current_fingerprint == requested_fingerprint
                    and client_turn_token == normalized_turn_token
                ):
                    return client
                # ClaudeSDKClient 的 options 在 connect 时冻结。模型、权限、max_turns
                # 或 Turn 回调变化后必须重建 Client；纠正任务只能通过 frozen SDK ID 恢复。
                try:
                    await client.disconnect()
                finally:
                    self._client = None
                    self._options_fingerprint = None
                    self._effective_options = None
                    self._client_turn_token = None
                    client = None
                    self.health = "new"

            candidate_options = options
            if client is None and sdk_session_id and policy == "resume_required":
                candidate_options = _copy_options_with_resume(options, sdk_session_id)
            resume_was_applied = (
                candidate_options is not options
                and getattr(candidate_options, "resume", None) == sdk_session_id
            )
            if policy == "resume_required" and not resume_was_applied:
                raise RuntimeError("严格恢复要求 options 能够设置 resume")

            self.health = "connecting"
            self._effective_options = None
            new_client: ClaudeSDKClient | None = None
            try:
                new_client = self._client_factory(candidate_options)
                await new_client.connect()
            except Exception:
                if new_client is not None:
                    try:
                        await new_client.disconnect()
                    except Exception:
                        pass
                self.health = "broken"
                self._effective_options = None
                raise
            self._client = new_client
            self._options_fingerprint = requested_fingerprint
            self._effective_options = candidate_options
            self._client_turn_token = normalized_turn_token or None
            self.health = "ready"
            return new_client

    async def query(self, prompt: str, *, turn_token: str | None = None) -> None:
        """串行发送一轮新目标，保留同一会话历史。

        Args:
            prompt: 当前 Activity 的完整权威任务说明。
            turn_token: 当前 query 独占的 Turn Capability。

        Raises:
            ValueError: 任务说明为空。
            RuntimeError: 会话未连接，或上一轮回复仍未收口。
        """

        normalized_prompt = prompt.strip()
        if not normalized_prompt:
            raise ValueError("query prompt 不能为空")
        async with self._control_lock:
            with self._state_lock:
                normalized_turn_token = self._validate_turn_token_locked(turn_token)
                client = self._client
                if client is None or self.health not in {"ready", "busy"}:
                    raise RuntimeError("AgentSession 尚未连接")
                if self._response_open:
                    raise RuntimeError("AgentSession 仍有未收口的 query")
                if self._client_turn_token != (normalized_turn_token or None):
                    raise RuntimeError("SDK transport 不属于当前 AgentSession Turn")
                effective_options = self._effective_options
                if effective_options is None:
                    raise RuntimeError("AgentSession 缺少当前 SDK options")
                turn = self.turn
                if (
                    turn is not None
                    and self._prequery_interrupted_activity_id == turn.activity_id
                ):
                    raise RuntimeError("AgentSession 当前 Turn 已在 query 前取消")
                self.health = "busy"
                self._response_open = True
                self._response_turn_token = normalized_turn_token or None
                self._response_interrupt_requested = False
                dispatch = AgentQueryDispatch(
                    activity_id=turn.activity_id if turn is not None else None,
                    prompt=normalized_prompt,
                    options=effective_options,
                )
            try:
                await self._query_dispatcher(client, dispatch)
            except (Exception, asyncio.CancelledError):
                with self._state_lock:
                    self.health = "broken"
                    self._effective_options = None
                    self._response_open = False
                    self._response_turn_token = None
                    self._response_interrupt_requested = False
                raise

    async def receive_response(self, *, turn_token: str | None = None):
        """读取当前 query 的完整 SDK 消息流，并在流结束后释放忙状态。

        Yields:
            SDK 为当前 Activity 产生的消息，顺序与 Provider 返回一致。

        Raises:
            RuntimeError: 会话尚未建立 Client。
        """

        with self._state_lock:
            normalized_turn_token = self._validate_turn_token_locked(turn_token)
            client = self._client
            if client is None:
                raise RuntimeError("AgentSession 尚未连接")
            response_open = self._response_open
            if self._response_reader_active:
                raise RuntimeError("AgentSession 已有 response reader")
            if not response_open:
                raise RuntimeError("AgentSession 没有正在读取的 query")
            if self._response_turn_token != (normalized_turn_token or None):
                raise RuntimeError("response reader 不属于当前 AgentSession Turn")
            self._response_reader_active = True
        try:
            received_terminal_result = False
            # Runner 会在首条 ResultMessage 后关闭本层生成器。必须同时显式关闭 SDK
            # 返回的内层 reader，不能依赖 async generator 的 GC 时机；否则旧 transport
            # 的 finally 尚未完成时，下一 Turn 已经可能开始绑定。
            async with aclosing(client.receive_response()) as sdk_response_stream:
                async for message in sdk_response_stream:
                    if isinstance(message, ResultMessage):
                        received_terminal_result = True
                    if isinstance(message, UserMessage):
                        self.capture_user_message(message)
                    yield message
        except Exception:
            with self._state_lock:
                # disconnect 或一次已经成功送出的 interrupt 都可能让 reader 以异常
                # 退出。closed 不能被迟到 reader 覆盖；interrupt 的最终健康由 finally
                # 在确认本轮流已经退出后统一裁决。
                if self.health != "closed" and not self._response_interrupt_requested:
                    self.health = "broken"
                    self._effective_options = None
            raise
        finally:
            with self._state_lock:
                interrupt_requested = self._response_interrupt_requested
                self._response_reader_active = False
                self._response_open = False
                self._response_turn_token = None
                self._response_interrupt_requested = False
                if self.health == "busy":
                    # 只有 SDK 明确给出 ResultMessage，或本轮已经成功发送 interrupt，
                    # 才能证明 EOF 是合法收口。过程消息后直接 EOF 仍按 CLI 异常处理。
                    self.health = (
                        "ready"
                        if received_terminal_result or interrupt_requested
                        else "broken"
                    )
                    if self.health == "broken":
                        self._effective_options = None
            if normalized_turn_token:
                self.retire_turn(normalized_turn_token)

    async def interrupt(self, *, activity_id: str | None = None) -> bool:
        """中断当前 query，但不销毁会话上下文。

        Args:
            activity_id: 可选的 Turn 身份护栏；不匹配时拒绝中断。

        Returns:
            当前 Client 确实收到中断请求时返回真。
        """

        async with self._control_lock:
            with self._state_lock:
                turn = self.turn
                if activity_id and (
                    turn is None or turn.activity_id != activity_id.strip()
                ):
                    return False
                client = self._client
                if client is None or self.health == "closed":
                    return False
                normalized_activity_id = activity_id.strip() if activity_id else ""
                if not self._response_open and normalized_activity_id:
                    # 取消可能恰好发生在 Runner 的最后一次检查之后、query 取得控制锁
                    # 之前。把事实绑定到当前 Turn，随后 query 才能在同一锁内拒绝提交。
                    self._prequery_interrupted_activity_id = normalized_activity_id
                # query 已经打开响应流时，先登记本轮正在中断，避免 SDK 收到
                # interrupt 后立即关闭流，response reader 抢先把合法 EOF 误判为损坏。
                # 连接阶段收到的提前取消也要送给 Client，但此时没有响应流可标记。
                self._response_interrupt_requested = self._response_open
            try:
                await client.interrupt()
            except Exception:
                with self._state_lock:
                    self._response_interrupt_requested = False
                    self._response_open = False
                    if self.health != "closed":
                        self.health = "broken"
                        self._effective_options = None
                raise
            # interrupt 只停止 SDK 当前循环，真正释放 query 槽位仍由
            # receive_response 的 finally 确认；否则旧流尚未退出时新 Activity 可能并发 query。
            with self._state_lock:
                if self._response_open and self.health != "broken":
                    self.health = "busy"
                elif self.health not in {"closed", "broken"}:
                    self.health = "ready"
            return True

    async def rewind_files(self, user_message_uuid: str) -> bool:
        """按 SDK checkpoint 回退文件；只报告 SDK 实际支持的范围。

        Args:
            user_message_uuid: SDK 文件 checkpoint 对应的用户消息编号。

        Returns:
            SDK 接受 rewind 调用时返回真；缺少锚点或调用失败时返回假。
        """

        async with self._control_lock:
            with self._state_lock:
                client = self._client
                response_open = self._response_open
            if client is None or response_open or not user_message_uuid.strip():
                return False
            try:
                result = client.rewind_files(user_message_uuid.strip())
                if inspect.isawaitable(result):
                    await result
            except Exception:
                return False
            return True

    def capture_user_message(self, message: UserMessage) -> None:
        """保存 SDK UserMessage UUID，作为未来精确 rewind 的内部线索。

        Args:
            message: SDK 回复流中的用户消息。
        """

        value = getattr(message, "uuid", None)
        if isinstance(value, str) and value.strip():
            self.last_user_message_uuid = value.strip()
            # SDK 的文件 checkpoint 以用户消息 UUID 作为 rewind 锚点；V1 只保存线索，
            # 不自动替用户决定回退哪些文件。
            self.last_checkpoint_uuid = value.strip()

    async def disconnect_turn_transport(self) -> None:
        """断开当前 Turn 的 SDK/MCP transport，但保留可恢复的会话身份。

        Activity 结束后不能继续占着 Claude Code 子进程和 MCP 连接，但纠正任务仍要
        使用已经确认的 SDK session_id 恢复上下文。这个方法只清理连接态；进程关闭时
        仍由 `disconnect()` 永久关闭整个产品级 AgentSession。

        Returns:
            无返回值。

        Raises:
            Exception: SDK Client 断开失败；会话会标记为损坏，不能继续恢复。
        """

        async with self._control_lock:
            with self._state_lock:
                if self.health == "closed":
                    return
                client = self._client
                turn = self.turn
            disconnect_failed = False
            try:
                if client is not None:
                    await client.disconnect()
            except Exception:
                disconnect_failed = True
                raise
            finally:
                with self._state_lock:
                    self._client = None
                    self._options_fingerprint = None
                    self._effective_options = None
                    self._client_turn_token = None
                    self._response_turn_token = None
                    self._response_open = False
                    self._response_reader_active = False
                    self._response_interrupt_requested = False
                    self._prequery_interrupted_activity_id = None
                    if turn is not None:
                        self._retired_turn_tokens.add(turn.turn_token)
                        self.turn = None
                    if disconnect_failed:
                        self.health = "broken"
                    else:
                        self.health = "ready" if self.sdk_session_id else "new"

    async def disconnect(self) -> None:
        """关闭 Client 和当前交互桥；Session 关闭后不能再次 query。

        Returns:
            无返回值。
        """

        async with self._control_lock:
            client = self._client
            turn = self.turn
            if turn is not None:
                turn.bridge.close("AgentSession 正在关闭。")
            try:
                if client is not None:
                    await client.disconnect()
            finally:
                self._client = None
                self._options_fingerprint = None
                self._effective_options = None
                self._client_turn_token = None
                self._response_turn_token = None
                self._response_open = False
                self._response_reader_active = False
                self._response_interrupt_requested = False
                self._prequery_interrupted_activity_id = None
                if turn is not None:
                    self._retired_turn_tokens.add(turn.turn_token)
                    self.turn = None
                self.health = "closed"


class AgentSessionManager:
    """按产品级会话编号管理工作区绑定的 AgentSession。

    Args:
        client_factory: 创建 SDK Client 的工厂，测试可注入替身。
        query_dispatcher: 传给全部新建 Session 的异步 query 提交器。
    """

    def __init__(
        self,
        *,
        client_factory: Callable[[Any], ClaudeSDKClient] = ClaudeSDKClient,
        query_dispatcher: AgentQueryDispatcher = dispatch_agent_query_directly,
    ) -> None:
        """初始化空的进程内 Session 表。

        Args:
            client_factory: 创建 SDK Client 的工厂，测试可注入替身。
            query_dispatcher: 传给全部新建 Session 的异步 query 提交器。
        """

        self._client_factory = client_factory
        self._query_dispatcher = query_dispatcher
        self._sessions: dict[str, AgentSession] = {}
        self._background_disconnect_tasks: set[asyncio.Task[None]] = set()
        self._lock = threading.RLock()

    def _track_background_disconnect(self, task: asyncio.Task[None]) -> None:
        """持有超时后的断开任务，直到真实 Session 清理完成。

        Args:
            task: 已经脱离同步关闭预算、仍需继续执行的断开任务。

        Returns:
            无；任务完成后从 Manager 的强引用集合移除。
        """

        self._background_disconnect_tasks.add(task)

        def consume(done_task: asyncio.Task[None]) -> None:
            """记录后台任务结果并释放 Manager 对它的强引用。"""

            self._background_disconnect_tasks.discard(done_task)
            _consume_background_disconnect(done_task)

        task.add_done_callback(consume)

    def get_or_create(
        self,
        *,
        workspace_root: str | Path,
        agent_session_id: str | None = None,
        policy: SessionPolicy = "new_session",
    ) -> AgentSession:
        """按策略取得会话，并拒绝工作区串用或伪恢复。

        Args:
            workspace_root: 新任务绑定的工作区根目录。
            agent_session_id: 需要继续的产品级 Session 编号。
            policy: 新建会话，或必须恢复现有 SDK 会话。

        Returns:
            满足策略且工作区一致的 AgentSession。

        Raises:
            RuntimeError: 要求恢复的 Session 不存在、不可用，或工作区不匹配。
        """

        normalized_id = agent_session_id.strip() if isinstance(agent_session_id, str) else ""
        if policy == "new_session":
            session = AgentSession(
                workspace_root=workspace_root,
                client_factory=self._client_factory,
                query_dispatcher=self._query_dispatcher,
            )
            with self._lock:
                self._sessions[session.agent_session_id] = session
            return session
        if not normalized_id:
            raise RuntimeError("严格恢复必须提供产品级 AgentSession 编号")
        with self._lock:
            existing = self._sessions.get(normalized_id)
            if existing is None:
                raise RuntimeError("要求恢复的 AgentSession 不存在")
            if existing.workspace_root != str(Path(workspace_root).resolve(strict=False)):
                raise RuntimeError("AgentSession 与工作区不匹配")
            if existing.health == "closed":
                raise RuntimeError("要求恢复的 AgentSession 已关闭")
            if existing.health == "broken":
                raise RuntimeError("要求恢复的 AgentSession 已损坏")
            if existing.sdk_session_id is None:
                raise RuntimeError("要求恢复的 AgentSession 缺少 SDK session_id")
            return existing

    def get(self, agent_session_id: str) -> AgentSession | None:
        """读取会话；未知编号返回空。

        Args:
            agent_session_id: 产品级 Session 编号。

        Returns:
            已登记的 AgentSession；编号未知时返回空。
        """

        with self._lock:
            return self._sessions.get(agent_session_id.strip())

    async def disconnect_all(self, timeout: float = 1.0) -> None:
        """关闭全部会话；当前版本不跨 Backend 重启恢复。

        Args:
            timeout: 为 Runner 统一关闭接口保留的超时秒数。

        Returns:
            无；超时只结束本次同步等待，已经启动的 Session 清理仍会继续执行。
        """

        with self._lock:
            sessions = tuple(self._sessions.values())
            self._sessions.clear()
        deadline = asyncio.get_running_loop().time() + max(timeout, 0.0)
        for session in sessions:
            remaining = deadline - asyncio.get_running_loop().time()
            # wait_for 会把取消直接传给 session.disconnect()，导致它的 finally
            # 先把 Session 标成 closed，Runner 随后误以为 transport 已经彻底断开。
            # 用独立 Task 加 shield 只限制当前同步等待，真实断开继续执行到完成。
            disconnect_task = asyncio.create_task(session.disconnect())
            if remaining <= 0:
                self._track_background_disconnect(disconnect_task)
                continue
            try:
                await asyncio.wait_for(asyncio.shield(disconnect_task), timeout=remaining)
            except asyncio.TimeoutError:
                self._track_background_disconnect(disconnect_task)
                # SDK transport 可能不响应；预算到期时只结束当前等待，不能取消真实清理。
                break
            except Exception:
                # 一个 Session 关闭失败不能阻止其它 Session 尽力释放。
                continue


def _copy_options_with_resume(options: Any, session_id: str) -> Any:
    """复制 SDK 选项并设置必须恢复的 Claude SDK 会话编号。

    Args:
        options: 当前 Activity 新构造的 SDK 选项。
        session_id: 需要恢复的 SDK 会话编号。

    Returns:
        带 resume 的浅复制选项；冻结对象无法修改时返回原对象。
    """

    try:
        candidate = copy.copy(options)
        setattr(candidate, "resume", session_id)
    except Exception:
        # 某些 SDK 版本的 options 可能是冻结对象；调用方会拒绝无 resume 的恢复。
        return options
    return candidate


_RECOVERY_OPTION_NAMES = frozenset(
    {
        "resume",
        "continue_conversation",
        "fork_session",
    }
)


def _options_fingerprint(options: Any) -> object:
    """生成忽略 Activity 临时回调、但覆盖执行配置的稳定指纹。

    Args:
        options: 当前 Activity 新构造的 ClaudeAgentOptions 或测试替身。

    Returns:
        可直接比较的不可变结构；相等表示旧 Client 的连接级配置仍适用。
    """

    return _stable_option_value(options, field_name="")


def _stable_option_value(value: Any, *, field_name: str) -> object:
    """递归归一化 SDK options，避免把闭包对象地址误当成配置变化。

    Args:
        value: options 中尚未归一化的任意字段。
        field_name: 当前字段名称，用于排除 SDK 恢复控制参数。

    Returns:
        只由不可变基础类型组成、可稳定比较的值。
    """

    if field_name in _RECOVERY_OPTION_NAMES:
        return "<recovery-option>"
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value.resolve(strict=False))
    if callable(value):
        # can_use_tool、Hook 和 stderr 闭包每个 Activity 都会重新创建，但它们通过
        # Turn Capability 隔离归属，不属于 SDK 对话能否 resume 的连接级配置。
        return "<activity-callback>"
    if isinstance(value, dict):
        return tuple(
            sorted(
                (
                    str(key),
                    _stable_option_value(item, field_name=str(key)),
                )
                for key, item in value.items()
            )
        )
    if isinstance(value, (list, tuple, set, frozenset)):
        items = tuple(
            _stable_option_value(item, field_name=field_name) for item in value
        )
        return tuple(sorted(items, key=repr)) if isinstance(value, (set, frozenset)) else items
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return (
            type(value).__qualname__,
            tuple(
                (
                    field.name,
                    _stable_option_value(
                        getattr(value, field.name),
                        field_name=field.name,
                    ),
                )
                for field in dataclasses.fields(value)
            ),
        )
    attributes = getattr(value, "__dict__", None)
    if isinstance(attributes, dict):
        return (
            type(value).__qualname__,
            tuple(
                sorted(
                    (
                        str(key),
                        _stable_option_value(item, field_name=str(key)),
                    )
                    for key, item in attributes.items()
                    if not str(key).startswith("_")
                )
            ),
        )
    return (type(value).__module__, type(value).__qualname__)
