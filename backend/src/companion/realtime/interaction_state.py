from __future__ import annotations

from dataclasses import dataclass, field
from threading import RLock
from typing import Literal
from uuid import uuid4

from companion.observability.logging_config import get_backend_logger


logger = get_backend_logger(__name__)

TurnState = Literal["idle", "listening", "processing", "speaking"]
InputTurnState = Literal["idle", "listening"]
AssistantOutputTurnState = Literal["idle", "processing", "speaking"]


@dataclass(frozen=True)
class InteractionTimeoutResult:
    """描述本次惰性时间衰减造成的状态变化。"""

    followup_changed: bool = False


@dataclass(frozen=True)
class FollowupToggleResult:
    """描述一次手动追问热区切换结果。

    Args:
        accepted: 本次切换是否被接受。
        followup_state: 切换后或拒绝时保留的追问热区状态。
        rejected_reason: 拒绝原因；成功时为空。

    Returns:
        无返回值；该对象作为调用方广播和 HTTP 响应的数据源。
    """

    accepted: bool
    followup_state: str
    rejected_reason: str | None = None


@dataclass
class InputState:
    """只记录用户输入轨的生命周期。

    输入轨可能因为短人声、切换音轨或 ASR reset 被清空，但这些动作不能修改助手输出。

    Returns:
        无返回值；实例由 ``InteractionState`` 持有并原地更新。
    """

    state: InputTurnState = "idle"

    @property
    def active(self) -> bool:
        """返回 VAD 是否已经确认用户正在说话。"""

        return self.state == "listening"

    def start(self) -> None:
        """把输入轨置为用户正在说话。"""

        self.state = "listening"

    def finish(self) -> None:
        """收口输入轨，不触碰任何助手输出身份。"""

        self.state = "idle"


@dataclass
class AssistantOutputState:
    """只记录当前助手请求和它的输出生命周期。

    Args:
        request_id: 当前助手请求编号；没有活跃输出时为空。
        generation_id: 当前助手 generation 编号；Bridge 入队后可能暂时为空。
        state: 输出处于 idle、processing 或 speaking。
    """

    request_id: str | None = None
    generation_id: str | None = None
    state: AssistantOutputTurnState = "idle"

    @property
    def playout_active(self) -> bool:
        """返回当前 generation 是否已经进入真实播音阶段。"""

        return self.state == "speaking"

    def register_request(self, request_id: str, *, generation_id: str | None = None) -> bool:
        """登记一条新的助手请求，并让它替代旧输出。

        Args:
            request_id: 已经进入助手输出队列的请求编号。
            generation_id: 已知的 generation 编号；请求刚入队时可以为空。

        Returns:
            请求身份有效并完成登记时返回真。

        Raises:
            ValueError: 请求编号为空。
        """

        normalized_request_id = request_id.strip()
        if not normalized_request_id:
            raise ValueError("助手请求必须绑定非空 request_id")
        if self.request_id != normalized_request_id:
            # 新助手请求会替代旧播音；旧事件之后只能被身份校验挡住。
            self.request_id = normalized_request_id
            self.generation_id = generation_id.strip() if isinstance(generation_id, str) and generation_id.strip() else None
            self.state = "processing"
            return True
        if generation_id is not None and generation_id.strip():
            normalized_generation_id = generation_id.strip()
            if self.generation_id not in {None, normalized_generation_id}:
                return False
            self.generation_id = normalized_generation_id
        if self.state == "idle":
            self.state = "processing"
        return True

    def start_playout(
        self,
        *,
        request_id: str,
        generation_id: str | None,
    ) -> bool:
        """应用首帧发布事实，拒绝旧请求或旧 generation 的迟到事件。"""

        if not self._matches(request_id=request_id, generation_id=generation_id):
            return False
        if generation_id and self.generation_id is None:
            self.generation_id = generation_id
        self.state = "speaking"
        return True

    def cancel_playout(self, *, request_id: str, generation_id: str | None) -> bool:
        """应用播音取消事实并清空当前输出身份。"""

        if not self._matches(request_id=request_id, generation_id=generation_id):
            return False
        self.clear()
        return True

    def finish(self, *, request_id: str, generation_id: str | None = None) -> bool:
        """应用助手输出真实终态并清空身份。"""

        if not self._matches(request_id=request_id, generation_id=generation_id):
            return False
        self.clear()
        return True

    def clear(self) -> None:
        """清除助手输出身份和播音阶段。"""

        self.request_id = None
        self.generation_id = None
        self.state = "idle"

    def _matches(self, *, request_id: str, generation_id: str | None) -> bool:
        """检查事件是否仍属于当前助手请求。"""

        if self.request_id != request_id:
            return False
        if generation_id is None or self.generation_id is None:
            return True
        return self.generation_id == generation_id


@dataclass
class InteractionState:
    """维护交互会话和两条相互独立的实时生命周期。

    输入状态只能描述用户是否正在说话；助手状态只能描述请求、generation 和播音。
    对外 turn_state 由二者派生，避免输入 reset 把助手输出误清成 idle。

    Args:
        followup_timeout_s: 追问热区空闲秒数，超时后继续语音输入需要重新唤醒。
    """

    followup_timeout_s: float = 10.0
    interaction_session_id: str = field(default_factory=lambda: uuid4().hex)
    followup_state: str = "followup_cool"
    manual_followup_cool: bool = False
    input_state: InputState = field(default_factory=InputState)
    assistant_output_state: AssistantOutputState = field(default_factory=AssistantOutputState)
    last_activity_at: float | None = None
    last_followup_activity_at: float | None = None
    last_turn_finished_at: float | None = None
    # Final ASR 的输入收口与输出接管会被不同线程观察，必须共享同一个状态读写边界。
    _state_lock: RLock = field(default_factory=RLock, init=False, repr=False, compare=False)
    _public_state_revision: int = field(default=0, init=False, repr=False, compare=False)
    _last_public_state: tuple[str, TurnState, str] | None = field(
        default=None, init=False, repr=False, compare=False,
    )

    def public_snapshot(self) -> dict[str, object]:
        """原子读取桌面需要的交互状态，并为真实变化分配版本。

        Args:
            无。

        Returns:
            跟进模式、轮次、交互会话和非负版本；重复读取相同状态保留同一版本。

        Raises:
            RuntimeError: 版本达到 JavaScript 安全整数上限后仍需推进。
        """

        with self._state_lock:
            turn_state = self._derive_turn_state()
            public_state = (self.followup_state, turn_state, self.interaction_session_id)
            if self._last_public_state is not None and public_state != self._last_public_state:
                # HTTP 回执和 WebSocket 可交错到达，版本只能前进，不能绕回旧状态。
                if self._public_state_revision >= 2**53 - 1:
                    raise RuntimeError("交互状态版本已达到安全整数上限")
                self._public_state_revision += 1
            self._last_public_state = public_state
            return {
                "revision": self._public_state_revision,
                "followup_state": self.followup_state,
                "turn_state": turn_state,
                "session_id": self.interaction_session_id,
            }

    @property
    def turn_state(self) -> TurnState:
        """按输入优先、输出其次的规则派生产品层 turn 状态。"""

        with self._state_lock:
            return self._derive_turn_state()

    def _derive_turn_state(self) -> TurnState:
        """在调用方已经持有状态锁时计算产品层 Turn 状态。"""

        if self.input_state.active:
            return "listening"
        if self.assistant_output_state.state == "speaking":
            return "speaking"
        if self.assistant_output_state.state == "processing":
            return "processing"
        return "idle"

    def current_assistant_output_identity(self) -> tuple[str, str | None] | None:
        """原子读取当前仍在处理或播放的助手输出身份。

        Returns:
            活跃输出的请求编号和可选 generation 编号；没有活跃输出时返回空。
        """

        with self._state_lock:
            output = self.assistant_output_state
            if output.state == "idle" or output.request_id is None:
                return None
            return output.request_id, output.generation_id

    def on_wake_word(self, now: float) -> None:
        """应用唤醒词命中事实并刷新追问热区。

        Args:
            now: 唤醒确认时的单调时间。

        Returns:
            无返回值；解除手动冷却并打开跟进窗口。
        """

        with self._state_lock:
            logger.info("%s-唤醒成功，进入追问热区", now)
            self.manual_followup_cool = False
            self._refresh_activity(now=now, refresh_followup=True, reason="wake_word")

    def on_user_speech_started(self, now: float) -> None:
        """应用 VAD 防抖确认后的用户开口事实。

        Args:
            now: 确认用户开口时的单调时间。

        Returns:
            无返回值；输入进入监听并刷新跟进窗口。
        """

        with self._state_lock:
            self.input_state.start()
            self._refresh_activity(now=now, refresh_followup=True, reason="user_speech_started")

    def on_user_input_finished(self, now: float) -> None:
        """收口用户输入侧，不修改助手输出身份。

        Args:
            now: 输入结束的单调时间；保留既有接口，不用于刷新窗口。

        Returns:
            无返回值；只结束正在进行的输入。
        """

        _ = now
        with self._state_lock:
            self.input_state.finish()

    def complete_final_asr_handoff(
        self,
        now: float,
        *,
        assistant_request_id: str | None,
    ) -> None:
        """原子结束 Final ASR 输入，并可选把同一轮交给助手输出侧。

        Args:
            now: Final ASR 被应用到产品状态的单调时间。
            assistant_request_id: 有效文本对应的助手请求编号；空文本或短文本时为空。

        Returns:
            无返回值；输入和输出状态会在同一把锁内完成更新。
        """

        # Notice Drainer 与音频线程会同时读取这份状态。锁释放前，即使输入已经置为 idle，
        # 其它线程也不能把中间态误认成整个 Turn 空闲并抢先提交 System Notice。
        with self._state_lock:
            self.input_state.finish()
            if assistant_request_id is None:
                return
            self.assistant_output_state.register_request(assistant_request_id)
            self._refresh_activity(
                now=now,
                refresh_followup=True,
                reason="assistant_request_queued",
            )

    def register_assistant_request(
        self,
        now: float,
        *,
        request_id: str,
        generation_id: str | None = None,
    ) -> None:
        """登记所有来源共用的助手请求身份。

        Args:
            now: 请求进入输出侧的单调时间。
            request_id: 已经入队的助手请求编号。
            generation_id: 已知生成编号；刚入队时可以省略。

        Returns:
            无返回值；登记输出身份并刷新跟进窗口。
        """

        with self._state_lock:
            self.assistant_output_state.register_request(
                request_id,
                generation_id=generation_id,
            )
            self._refresh_activity(now=now, refresh_followup=True, reason="assistant_request_queued")

    def on_barge_in(self, now: float) -> None:
        """应用用户插话事实。

        Args:
            now: 插话确认时的单调时间。

        Returns:
            无返回值；只刷新活动和跟进窗口，不取消任何输出。
        """

        with self._state_lock:
            self._refresh_activity(now=now, refresh_followup=True, reason="barge_in")

    def on_assistant_audio_publishing_started(
        self,
        now: float,
        *,
        request_id: str,
        generation_id: str | None,
    ) -> bool:
        """应用助手音频已经进入真实播放链路的事实。

        Args:
            now: 播音开始时的单调时间。
            request_id: 播音对应的请求编号。
            generation_id: 播音对应的生成编号；未知时为空。

        Returns:
            当前输出身份匹配并进入播音时返回真；旧输出事件返回假。
        """

        with self._state_lock:
            accepted = self.assistant_output_state.start_playout(
                request_id=request_id,
                generation_id=generation_id,
            )
            if accepted:
                self._refresh_activity(now=now, refresh_followup=True, reason="assistant_audio_publishing_started")
            return accepted

    def on_assistant_audio_publishing_cancelled(
        self,
        now: float,
        *,
        request_id: str,
        generation_id: str | None,
    ) -> bool:
        """应用助手音频播放被取消的事实。

        Args:
            now: 播音取消时的单调时间。
            request_id: 被取消的请求编号。
            generation_id: 被取消的生成编号；未知时为空。

        Returns:
            当前输出身份匹配并完成收口时返回真；旧输出事件返回假。
        """

        with self._state_lock:
            accepted = self.assistant_output_state.cancel_playout(
                request_id=request_id,
                generation_id=generation_id,
            )
            if accepted:
                self._refresh_activity(now=now, refresh_followup=True, reason="assistant_audio_publishing_cancelled")
            return accepted

    def on_assistant_output_finished(
        self,
        now: float,
        *,
        request_id: str,
        generation_id: str | None = None,
    ) -> bool:
        """应用助手输出真实终态。

        Args:
            now: 输出结束时的单调时间。
            request_id: 结束的请求编号。
            generation_id: 结束的生成编号；未知时可以省略。

        Returns:
            当前输出身份匹配并结束时返回真；旧输出事件返回假。
        """

        with self._state_lock:
            accepted = self.assistant_output_state.finish(
                request_id=request_id,
                generation_id=generation_id,
            )
            if accepted:
                self.last_turn_finished_at = now
                self._refresh_activity(now=now, refresh_followup=True, reason="assistant_output_finished")
            return accepted

    def apply_timeouts(self, now: float) -> InteractionTimeoutResult:
        """根据当前时间惰性冷却追问热区。

        Args:
            now: 当前单调时间。

        Returns:
            是否发生跟进窗口冷却；有效输入和真实播音不会被冷却。
        """

        with self._state_lock:
            if self.input_state.active or self.assistant_output_state.playout_active:
                return InteractionTimeoutResult(followup_changed=False)

            if not self._followup_expired(now):
                return InteractionTimeoutResult(followup_changed=False)

            self._set_followup_state("followup_cool", now=now, reason="followup_timeout")
            return InteractionTimeoutResult(followup_changed=True)

    def toggle_followup_state(self, now: float) -> FollowupToggleResult:
        """手动切换追问热区状态。

        Args:
            now: 控制面接收到用户快捷键意图的单调时间。

        Returns:
            FollowupToggleResult: 描述切换是否成功以及当前追问热区状态。
        """

        # 用户正在说话时不能把窗口关掉，否则当前 turn 的后续音频会被唤醒门控截断。
        with self._state_lock:
            if self.followup_state == "followup_hot" and self.input_state.active:
                return FollowupToggleResult(
                    accepted=False,
                    followup_state=self.followup_state,
                    rejected_reason="user_input_active",
                )

            if self.followup_state == "followup_hot":
                self.manual_followup_cool = True
                self.last_followup_activity_at = None
                self._set_followup_state("followup_cool", now=now, reason="manual_toggle")
                return FollowupToggleResult(accepted=True, followup_state=self.followup_state)

            self.manual_followup_cool = False
            self._refresh_activity(now=now, refresh_followup=True, reason="manual_toggle")
            return FollowupToggleResult(accepted=True, followup_state=self.followup_state)

    def requires_wake_word(self) -> bool:
        """判断当前语音输入是否需要先命中唤醒词。"""

        return self.followup_state != "followup_hot"

    def _refresh_activity(self, *, now: float, refresh_followup: bool, reason: str) -> None:
        """刷新运行期活动时间，并按需刷新追问热区。"""

        self.last_activity_at = now
        if refresh_followup:
            # 手动冷却表达用户明确关闭免唤醒；除唤醒词或手动打开外，普通活动不能反向重开。
            if self.manual_followup_cool:
                return
            self._set_followup_state("followup_hot", now=now, reason=reason)
            self.last_followup_activity_at = now

    def _followup_expired(self, now: float) -> bool:
        """判断追问热区是否已经冷却。"""

        if self.followup_state != "followup_hot" or self.last_followup_activity_at is None:
            return False
        return now - self.last_followup_activity_at >= self.followup_timeout_s

    def _set_followup_state(self, new_state: str, *, now: float, reason: str) -> None:
        """更新追问热区状态，并在真实变化时记录诊断日志。"""

        old_state = self.followup_state
        if old_state == new_state:
            return

        self.followup_state = new_state
        logger.info(
            "追问热区发生变化，原因=%s，旧状态=%s，新状态=%s，单调时间=%s，"
            "interaction_session_id=%s，turn_state=%s，input_state=%s，assistant_output_state=%s，"
            "last_activity_at=%s，last_followup_activity_at=%s，manual_followup_cool=%s，"
            "active_request_id=%s，generation_id=%s",
            reason,
            old_state,
            new_state,
            now,
            self.interaction_session_id,
            self.turn_state,
            self.input_state.state,
            self.assistant_output_state.state,
            self.last_activity_at,
            self.last_followup_activity_at,
            self.manual_followup_cool,
            self.assistant_output_state.request_id,
            self.assistant_output_state.generation_id,
        )
