"""定义 Windows 已安装应用同步启动所需的稳定领域模型。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal


class LaunchMode(StrEnum):
    """描述用户希望复用现有窗口还是明确新开实例。"""

    REUSE_EXISTING = "reuse_existing"
    NEW_INSTANCE = "new_instance"


LaunchResultState = Literal["completed", "rejected", "failed"]
LaunchOutcome = Literal[
    "focused_existing",
    "launch_requested",
    "new_instance_created",
]


@dataclass(frozen=True, slots=True)
class InstalledApplication:
    """保存一个可以交给 Windows Shell 启动的应用描述。

    Args:
        display_name: 面向用户展示的应用名称。
        application_id: Windows AppsFolder 中的稳定应用身份。
        launch_target: 交给 Windows Shell 的规范启动目标。
        executable_path: 能确认时保存主程序路径，用于识别进程和窗口。
        aliases: 除展示名之外可参与匹配的应用别名。
    """

    display_name: str
    application_id: str
    launch_target: str
    executable_path: str | None = None
    aliases: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """裁剪字符串并拒绝无法安全启动的空应用描述。"""

        display_name = self.display_name.strip()
        application_id = self.application_id.strip()
        launch_target = self.launch_target.strip()
        executable_path = (
            self.executable_path.strip()
            if isinstance(self.executable_path, str) and self.executable_path.strip()
            else None
        )
        aliases = tuple(
            alias.strip()
            for alias in self.aliases
            if isinstance(alias, str) and alias.strip()
        )
        if not display_name:
            raise ValueError("应用展示名称不能为空")
        if not application_id:
            raise ValueError("应用身份不能为空")
        if not launch_target:
            raise ValueError("应用启动目标不能为空")
        object.__setattr__(self, "display_name", display_name)
        object.__setattr__(self, "application_id", application_id)
        object.__setattr__(self, "launch_target", launch_target)
        object.__setattr__(self, "executable_path", executable_path)
        object.__setattr__(self, "aliases", aliases)


@dataclass(frozen=True, slots=True)
class ApplicationRuntimeSnapshot:
    """记录某个应用当前拥有的普通窗口和进程。

    Args:
        window_handles: 按 Windows Z 序排列的普通顶层窗口句柄。
        process_ids: 当前确认属于该应用的进程编号。
    """

    window_handles: tuple[int, ...] = ()
    process_ids: frozenset[int] = frozenset()


@dataclass(frozen=True, slots=True)
class ApplicationLaunchResult:
    """描述可以安全写回 Foreground 模型的启动结果。

    Args:
        state: completed、rejected 或 failed。
        outcome: 成功时的具体完成事实。
        application_name: 已确定的安全应用展示名。
        reason: 拒绝或失败时的稳定机器原因。
        safe_message: 可以交给 Foreground 自然转述的安全说明。
        candidates: 名称最高分并列时的安全候选名。
    """

    state: LaunchResultState
    outcome: LaunchOutcome | None = None
    application_name: str | None = None
    reason: str | None = None
    safe_message: str | None = None
    candidates: tuple[str, ...] = ()

    @classmethod
    def completed(
        cls,
        *,
        outcome: LaunchOutcome,
        application_name: str,
    ) -> ApplicationLaunchResult:
        """创建一项已经同步完成的应用启动事实。

        Args:
            outcome: 置前、提交启动或确认新实例的具体事实。
            application_name: 已操作的应用展示名。

        Returns:
            可直接转换成 Tool 载荷的成功结果。
        """

        return cls(
            state="completed",
            outcome=outcome,
            application_name=application_name,
        )

    @classmethod
    def rejected(
        cls,
        *,
        reason: str,
        safe_message: str,
        candidates: tuple[str, ...] = (),
    ) -> ApplicationLaunchResult:
        """创建一项没有执行任何启动动作的业务拒绝。

        Args:
            reason: 稳定拒绝原因。
            safe_message: 面向用户的安全说明。
            candidates: 需要用户确认时的同分候选。

        Returns:
            可直接转换成 Tool 载荷的拒绝结果。
        """

        return cls(
            state="rejected",
            reason=reason,
            safe_message=safe_message,
            candidates=candidates,
        )

    @classmethod
    def failed(
        cls,
        *,
        reason: str,
        safe_message: str,
        application_name: str | None = None,
    ) -> ApplicationLaunchResult:
        """创建一项已经尝试但没有完成目标的失败事实。

        Args:
            reason: 稳定失败原因。
            safe_message: 面向用户的安全说明。
            application_name: 已经确定时附带应用展示名。

        Returns:
            可直接转换成 Tool 载荷的失败结果。
        """

        return cls(
            state="failed",
            reason=reason,
            safe_message=safe_message,
            application_name=application_name,
        )

    def to_tool_payload(self) -> dict[str, object]:
        """生成不含本地路径、进程和窗口身份的 Tool 结果。

        Returns:
            只包含稳定状态与安全展示字段的字典。
        """

        payload: dict[str, object] = {"state": self.state}
        if self.outcome is not None:
            payload["outcome"] = self.outcome
        if self.application_name is not None:
            payload["application_name"] = self.application_name
        if self.reason is not None:
            payload["reason"] = self.reason
        if self.safe_message is not None:
            payload["safe_message"] = self.safe_message
        if self.candidates:
            payload["candidates"] = list(self.candidates)
        return payload
