from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class SidecarConfig:
    """桌面 AEC 侧车启动配置。

    功能：保存侧车连接 LiveKit、绑定父进程、配置音频格式所需的基础参数。
    核心入参：livekit_url、token、room_name、identity 描述 LiveKit 连接身份，parent_pid 描述宿主进程，音频字段描述输入输出格式。
    预期输出：调用 validate 后确认配置满足 Task 3 的最小运行边界。
    边界异常：字段类型错误、必填字符串为空白、身份为空白、父进程编号无效、采样率不适合 10ms 帧、声道数不是单声道或预热时间为负时抛出 ValueError。
    """

    # LiveKit 服务地址，通常来自宿主进程传入的环境变量或命令行参数。
    livekit_url: str
    # LiveKit 入房令牌，必须是非空白字符串，侧车不在此处解析令牌内容。
    token: str
    # 目标房间名称，用于让 clean 麦克风发布者进入正确房间。
    room_name: str
    # 侧车参与者身份，由后端签发并由 desktop main 传入，sidecar 不在本地硬编码身份前缀。
    identity: str
    # 宿主进程编号，watchdog 用它判断侧车是否需要退出。
    parent_pid: int
    # AEC 处理侧的原始采样率，默认匹配 Windows loopback 与常见音频设备的 48kHz。
    sample_rate: int = 48000
    # clean 麦克风发布采样率，默认输出给实时语音链路常用的 16kHz 单声道。
    clean_output_sample_rate: int = 16000
    # 当前侧车基础单元只支持单声道，避免 Task 3 引入多声道同步规则。
    channels: int = 1
    # 启动后预热秒数，用于未来 runtime 等待 AEC 参考流稳定。
    warmup_seconds: float = 2.0
    # 可选调试输出目录，未设置时不落盘调试音频。
    debug_output_dir: Path | None = None
    # 可选调试运行目录保留上限，未设置时表示不主动裁剪历史 run 目录。
    max_debug_runs: int | None = None
    # 音频设备选择模式，system_default 会允许默认设备失败后自动 fallback。
    audio_device_mode: str = "system_default"
    # 手动选择的麦克风设备 id，优先级高于名称。
    microphone_id: str | None = None
    # 手动选择的麦克风设备名称，供 id 缺失或不稳定时使用。
    microphone_name: str | None = None
    # 手动选择的扬声器 loopback 设备 id，优先级高于名称。
    speaker_loopback_id: str | None = None
    # 手动选择的扬声器 loopback 设备名称。
    speaker_loopback_name: str | None = None

    def validate(self) -> None:
        """校验侧车配置是否满足基础运行前置条件。

        功能：在真实 runtime 启动前集中拒绝明显无效的连接、身份、进程和音频参数。
        核心入参：无显式入参，校验当前 SidecarConfig 实例字段。
        预期输出：合法配置正常返回 None，调用方可继续启动侧车。
        边界异常：任一字段不满足 Task 3 约束时抛出 ValueError，错误消息包含对应字段名。
        """
        # LiveKit 连接三要素必须是非空白字符串，否则侧车即使启动也无法进入目标房间。
        for field_name in ("livekit_url", "token", "room_name"):
            # getattr 让三个字符串字段共享同一段校验逻辑，同时错误消息仍保留精确字段名。
            value = getattr(self, field_name)
            if not isinstance(value, str):
                raise ValueError(f"{field_name} must be a non-blank string.")
            if not value.strip():
                raise ValueError(f"{field_name} must be a non-blank string.")

        # 身份必须是非空白字符串，具体前缀策略由后端签发 token 时决定，sidecar 只校验显式入参可用于 ready 匹配。
        if not isinstance(self.identity, str):
            raise ValueError("identity must be a non-blank string.")

        # 空白身份无法让 supervisor 校验 ready 事件，也无法让 LiveKit 房间里的参与者来源保持可审计。
        if not self.identity.strip():
            raise ValueError("identity must be a non-blank string.")

        # 父进程编号必须是普通正整数，字符串或 bool 都不能作为系统 pid 探测参数。
        if not isinstance(self.parent_pid, int) or isinstance(self.parent_pid, bool):
            raise ValueError("parent_pid must be greater than 0.")

        # 父进程编号必须能被 watchdog 用于系统探测，非正数无法代表真实宿主进程。
        if self.parent_pid <= 0:
            raise ValueError("parent_pid must be greater than 0.")

        # 两个采样率都必须能被 100 整除，因为 AEC 与发布链路都围绕精确 10ms PCM 帧工作。
        for field_name in ("sample_rate", "clean_output_sample_rate"):
            # 采样率必须是普通整数，避免字符串、None 或 bool 在比较和取模时泄漏内建异常。
            value = getattr(self, field_name)
            if not isinstance(value, int) or isinstance(value, bool):
                raise ValueError(f"{field_name} must be greater than 0.")
            # 采样率同时要求正数和 10ms 可整除，避免后续帧样本数出现零值或小数。
            if value <= 0:
                raise ValueError(f"{field_name} must be greater than 0.")
            if value % 100 != 0:
                raise ValueError(f"{field_name} must be divisible by 100.")

        # 声道数必须是普通整数，随后再检查单声道约束，确保错误始终以字段级 ValueError 呈现。
        if not isinstance(self.channels, int) or isinstance(self.channels, bool):
            raise ValueError("channels must be 1.")

        # Task 3 的侧车基础链路限定为单声道，其他声道布局需要额外的帧对齐和发布策略。
        if self.channels != 1:
            raise ValueError("channels must be 1.")

        # 预热时间必须是数字类型，避免字符串等错误输入在比较阶段泄漏 TypeError。
        if not isinstance(self.warmup_seconds, (int, float)) or isinstance(self.warmup_seconds, bool):
            raise ValueError("warmup_seconds must be greater than or equal to 0.")

        # 预热时间允许为 0，表示立即发布；负数没有时间语义，必须拒绝。
        if self.warmup_seconds < 0:
            raise ValueError("warmup_seconds must be greater than or equal to 0.")

        # debug run 保留上限属于可选策略；一旦显式设置，就必须是普通正整数。
        if self.max_debug_runs is not None:
            if not isinstance(self.max_debug_runs, int) or isinstance(self.max_debug_runs, bool):
                raise ValueError("max_debug_runs must be greater than 0.")
            if self.max_debug_runs <= 0:
                raise ValueError("max_debug_runs must be greater than 0.")

        # 音频设备模式决定默认设备失败后的恢复策略，必须在打开设备前明确。
        if self.audio_device_mode not in {"system_default", "manual"}:
            raise ValueError("audio_device_mode must be system_default or manual.")
