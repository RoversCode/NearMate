"""定义 Backend 进程内运行期身份的生成与永久登记规则。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from threading import RLock
from uuid import UUID, uuid4

from companion.realtime.assistant_events import AssistantRequest


class AssistantRequestIdentityConflict(RuntimeError):
    """表示同一助手请求编号被复用或执行身份发生改写。

    Args:
        request_id: 发生冲突的助手请求编号。
        field: 发生冲突的身份字段。
        expected: Registry 已经保存的字段值。
        actual: 新提交或新事件携带的字段值。
    """

    def __init__(
        self,
        *,
        request_id: str,
        field: str,
        expected: object,
        actual: object,
    ) -> None:
        """保存冲突字段，并构造不包含用户正文的诊断消息。

        Args:
            request_id: 发生冲突的助手请求编号。
            field: 发生冲突的身份字段。
            expected: Registry 已经保存的字段值。
            actual: 新提交或新事件携带的字段值。

        Returns:
            None: 异常对象只保存诊断字段。
        """

        self.request_id = request_id
        self.field = field
        self.expected = expected
        self.actual = actual
        super().__init__(
            "助手请求身份冲突 "
            f"request_id={request_id} field={field} expected={expected!r} actual={actual!r}"
        )


class RuntimeIdentityFactory:
    """生成带业务前缀的 UUID v4 运行期身份。

    Args:
        uuid_factory: 创建 UUID 的函数；测试可以注入固定 UUID。
    """

    def __init__(self, *, uuid_factory: Callable[[], UUID] = uuid4) -> None:
        """保存 UUID 创建端口。

        Args:
            uuid_factory: 创建 UUID 的函数；测试可以注入固定 UUID。

        Returns:
            None: 构造函数只保存依赖。
        """

        self._uuid_factory = uuid_factory

    def new(self, prefix: str) -> str:
        """生成一条不依赖本地计数器或时钟的运行期身份。

        Args:
            prefix: 便于日志辨认的业务前缀，例如 ``final-asr``。

        Returns:
            由业务前缀和 UUID v4 组成的唯一身份。

        Raises:
            ValueError: 前缀为空或带有首尾空白时抛出。
        """

        normalized_prefix = prefix.strip()
        if not normalized_prefix or normalized_prefix != prefix:
            raise ValueError("运行期身份前缀必须是没有首尾空白的非空字符串")
        return f"{normalized_prefix}-{self._uuid_factory()}"


@dataclass(frozen=True, slots=True)
class AssistantRequestIdentity:
    """保存请求在当前 Backend 进程内不可改写的最小身份。

    Args:
        request_id: Backend 进程内永不复用的助手请求编号。
        source: 请求来源，例如文本、Final ASR 或 System Notice。
        source_turn_id: 可选的输入 Turn 关联编号，不承担幂等职责。
        interaction_session_id: 请求所属的产品交互会话编号。
        runtime_epoch: 真正接收请求的助手子进程世代。
        generation_id: 子进程为请求创建的输出代际编号。
    """

    request_id: str
    source: str
    source_turn_id: str | None
    interaction_session_id: str
    runtime_epoch: int | None = None
    generation_id: str | None = None


class AssistantRequestIdentityRegistry:
    """永久登记当前 Backend 进程见过的助手请求身份。

    请求一旦 claim 就不会删除。这样迟到事件可以继续验证原身份，新请求也不能在旧记录
    结算后复用同一编号。
    """

    def __init__(self) -> None:
        """初始化空的永久身份表。

        Returns:
            None: 新 Registry 尚未登记任何请求。
        """

        self._records: dict[str, AssistantRequestIdentity] = {}
        self._lock = RLock()

    def claim(self, request: AssistantRequest) -> AssistantRequestIdentity:
        """在请求进入 Bridge 队列前永久占用它的编号。

        Args:
            request: 已经完成上下文冻结的助手请求。

        Returns:
            本次新建并保存的最小身份记录。

        Raises:
            ValueError: 请求编号为空时抛出。
            AssistantRequestIdentityConflict: 请求编号已经登记时抛出。
        """

        request_id = request.request_id.strip()
        if not request_id:
            raise ValueError("助手请求编号不能为空")
        identity = AssistantRequestIdentity(
            request_id=request_id,
            source=request.source,
            source_turn_id=(
                request.source_turn_id.strip()
                if isinstance(request.source_turn_id, str) and request.source_turn_id.strip()
                else None
            ),
            interaction_session_id=request.interaction_session_id.strip(),
        )
        with self._lock:
            if request_id in self._records:
                raise AssistantRequestIdentityConflict(
                    request_id=request_id,
                    field="request_id",
                    expected="unused",
                    actual="already_claimed",
                )
            self._records[request_id] = identity
        return identity

    def bind_runtime_epoch(
        self,
        request_id: str,
        runtime_epoch: int,
    ) -> AssistantRequestIdentity:
        """把请求绑定到真正接收它的助手运行时世代。

        Args:
            request_id: 已经 claim 的助手请求编号。
            runtime_epoch: 稳定进程句柄返回的运行时世代。

        Returns:
            补齐 runtime epoch 后的身份记录。

        Raises:
            AssistantRequestIdentityConflict: 请求未知或 epoch 与既有记录不一致时抛出。
        """

        return self._bind_execution_identity(
            request_id=request_id,
            runtime_epoch=runtime_epoch,
            generation_id=None,
        )

    def get(self, request_id: str) -> AssistantRequestIdentity | None:
        """读取永久身份，使仍在播放的请求不依赖已经释放的完整请求账本。

        Args:
            request_id: 当前输出所属的助手请求编号。

        Returns:
            已登记的不可变身份；未知编号返回空。
        """

        with self._lock:
            return self._records.get(request_id.strip())

    def bind_generation(
        self,
        request_id: str,
        generation_id: str,
    ) -> AssistantRequestIdentity:
        """把请求绑定到子进程实际创建的 generation。

        Args:
            request_id: 已经 claim 的助手请求编号。
            generation_id: 子进程输出事件携带的 generation 编号。

        Returns:
            补齐 generation 后的身份记录。

        Raises:
            ValueError: generation 编号为空时抛出。
            AssistantRequestIdentityConflict: 请求未知或 generation 被改写时抛出。
        """

        normalized_generation_id = generation_id.strip()
        if not normalized_generation_id:
            raise ValueError("generation_id 不能为空")
        return self._bind_execution_identity(
            request_id=request_id,
            runtime_epoch=None,
            generation_id=normalized_generation_id,
        )

    def validate_event(
        self,
        *,
        request_id: str,
        runtime_epoch: int | None,
        generation_id: str | None,
    ) -> AssistantRequestIdentity:
        """校验输出事件仍属于请求已经绑定的执行身份。

        首条事件可以补齐此前尚未知的 epoch 或 generation；已知字段只能保持一致。

        Args:
            request_id: 输出事件携带的助手请求编号。
            runtime_epoch: 主进程句柄附加的运行时世代。
            generation_id: 子进程输出代际编号。

        Returns:
            校验并补齐后的身份记录。

        Raises:
            AssistantRequestIdentityConflict: 请求未知或执行身份不一致时抛出。
        """

        normalized_generation_id = (
            generation_id.strip()
            if isinstance(generation_id, str) and generation_id.strip()
            else None
        )
        return self._bind_execution_identity(
            request_id=request_id,
            runtime_epoch=runtime_epoch,
            generation_id=normalized_generation_id,
        )

    def _bind_execution_identity(
        self,
        *,
        request_id: str,
        runtime_epoch: int | None,
        generation_id: str | None,
    ) -> AssistantRequestIdentity:
        """在同一锁内校验并补齐可选执行身份字段。

        Args:
            request_id: 已经 claim 的助手请求编号。
            runtime_epoch: 当前事件或提交边界观察到的运行时世代。
            generation_id: 当前事件观察到的 generation 编号。

        Returns:
            校验并补齐后的不可变身份记录。

        Raises:
            AssistantRequestIdentityConflict: 请求未知或既有执行身份被改写时抛出。
        """

        normalized_request_id = request_id.strip()
        with self._lock:
            identity = self._records.get(normalized_request_id)
            if identity is None:
                raise AssistantRequestIdentityConflict(
                    request_id=normalized_request_id,
                    field="request_id",
                    expected="claimed",
                    actual="missing",
                )
            if (
                runtime_epoch is not None
                and identity.runtime_epoch is not None
                and identity.runtime_epoch != runtime_epoch
            ):
                raise AssistantRequestIdentityConflict(
                    request_id=normalized_request_id,
                    field="runtime_epoch",
                    expected=identity.runtime_epoch,
                    actual=runtime_epoch,
                )
            if (
                generation_id is not None
                and identity.generation_id is not None
                and identity.generation_id != generation_id
            ):
                raise AssistantRequestIdentityConflict(
                    request_id=normalized_request_id,
                    field="generation_id",
                    expected=identity.generation_id,
                    actual=generation_id,
                )
            updated_identity = replace(
                identity,
                runtime_epoch=(
                    identity.runtime_epoch
                    if identity.runtime_epoch is not None
                    else runtime_epoch
                ),
                generation_id=(
                    identity.generation_id
                    if identity.generation_id is not None
                    else generation_id
                ),
            )
            self._records[normalized_request_id] = updated_identity
            return updated_identity
