"""保存运行时初始化的脱敏快照，HTTP 与控制事件共用这一份事实。"""

from dataclasses import asdict, dataclass, replace
from typing import Literal
from uuid import uuid4


RuntimeState = Literal["idle", "initializing", "ready", "failed"]
RuntimePhase = Literal["idle", "cleanup", "input_models", "assistant", "media", "ready", "failed"]

PHASE_MESSAGES = {
    "idle": "请继续完成首次配置。",
    "cleanup": "正在关闭旧运行时…",
    "input_models": "正在加载语音输入模型…",
    "assistant": "正在初始化助手与语音输出…",
    "media": "正在连接实时媒体…",
    "ready": "运行时已就绪。",
    "failed": "运行时初始化失败，请重试。",
}


@dataclass(frozen=True, slots=True)
class RuntimeInitializationSnapshot:
    """记录本次 Backend 进程的初始化身份、阶段与安全提示。

    Args:
        runtime_id: 本次进程的 UUID，重启后允许客户端重置 revision。
        operation_id: 当前或最近一次初始化编号；尚未初始化时为空。
        revision: 同一进程内递增的快照版本。
        state: 未配置、初始化中、已就绪或失败。
        phase: 已经实际进入的业务阶段，不表示百分比。
        message: 固定安全提示，不能包含第三方异常或内部秘密。
        retryable: 当前失败是否允许重试。
    """

    runtime_id: str
    operation_id: str | None = None
    revision: int = 0
    state: RuntimeState = "idle"
    phase: RuntimePhase = "idle"
    message: str = PHASE_MESSAGES["idle"]
    retryable: bool = False

    @classmethod
    def initial(cls) -> "RuntimeInitializationSnapshot":
        """为当前进程生成一次独立身份。

        Returns:
            revision 为零、尚无初始化操作的快照。
        """
        return cls(runtime_id=str(uuid4()))

    def changed(self, *, state: RuntimeState, phase: RuntimePhase,
                operation_id: str | None, retryable: bool = False,
                message: str | None = None) -> "RuntimeInitializationSnapshot":
        """推进快照版本，使迟到的 HTTP 或事件不能覆盖新阶段。

        Args:
            state: 新的初始化状态。
            phase: 已进入的真实阶段。
            operation_id: 本次状态所属的操作编号。
            retryable: 失败时是否可以重试。
            message: 可选的固定安全错误提示。
        Returns:
            新的不可变快照；原快照保持原样。
        """
        if self.revision >= 2**53 - 1:
            raise RuntimeError("运行时初始化状态版本已耗尽")
        return replace(self, revision=self.revision + 1, state=state, phase=phase,
                       operation_id=operation_id, retryable=retryable,
                       message=message or PHASE_MESSAGES[phase])

    def to_payload(self) -> dict[str, object]:
        """返回可直接用于 HTTP 与状态事件的完整公开快照。

        Returns:
            精确七字段的字典，不包含 Key、路径或原始异常。
        """
        return asdict(self)
