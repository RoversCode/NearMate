from __future__ import annotations

from pathlib import Path

import pytest

from audio_aec_sidecar.config import SidecarConfig


def make_config(**overrides) -> SidecarConfig:
    """构造一份默认合法的 SidecarConfig，允许单个测试覆盖目标字段。

    功能：集中维护侧车配置测试所需的最小合法参数，避免每个用例重复铺开样板字段。
    核心入参：overrides 是待覆盖的配置字段键值对。
    预期输出：返回一个尚未调用 validate 的 SidecarConfig 实例。
    边界异常：若传入不存在的字段，dataclass 构造过程会直接抛出 TypeError，提示测试用例写错字段名。
    """
    # 默认配置覆盖真实侧车启动所需的身份、房间和父进程参数，是所有校验用例的稳定基线。
    values = {
        "livekit_url": "wss://livekit.example",
        "token": "test-token",
        "room_name": "desktop-room",
        "identity": "desktop-aec-worker",
        "parent_pid": 1234,
    }
    # 每个测试只替换自己关心的字段，让失败原因保持单一且易读。
    values.update(overrides)
    return SidecarConfig(**values)


def test_sidecar_config_accepts_valid_required_and_default_values() -> None:
    """验证合法配置能通过校验，并保留计划要求的默认音频参数。"""
    # debug_output_dir 使用 Path，覆盖可选路径字段在合法配置中的正常形态。
    config = make_config(debug_output_dir=Path("debug-audio"), max_debug_runs=7)

    # 合法配置不应抛出异常，调用方可以在启动侧车前统一执行该校验。
    config.validate()

    # 默认值是侧车与 AEC 管线协作的公开契约，避免启动层在缺省时得到意外音频参数。
    assert config.sample_rate == 48000
    assert config.clean_output_sample_rate == 16000
    assert config.channels == 1
    assert config.warmup_seconds == 2.0
    assert config.debug_output_dir == Path("debug-audio")
    assert config.max_debug_runs == 7


@pytest.mark.parametrize("field_name", ["livekit_url", "token", "room_name"])
def test_sidecar_config_rejects_blank_livekit_connection_fields(field_name: str) -> None:
    """验证 LiveKit 连接字段为空白时被拒绝。"""
    # 空白字符串容易从环境变量或命令行参数透传进来，必须在进入运行时前阻断。
    config = make_config(**{field_name: "   "})

    # 错误消息包含字段名，方便启动层或日志直接定位是哪一项配置缺失。
    with pytest.raises(ValueError, match=field_name):
        config.validate()


def test_sidecar_config_rejects_none_livekit_url_with_field_specific_value_error() -> None:
    """验证 livekit_url 类型错误时抛出字段级 ValueError。"""
    # None 常见于上游环境变量缺失后未做默认值处理，配置层必须给出明确字段名而不是 AttributeError。
    config = make_config(livekit_url=None)

    with pytest.raises(ValueError, match="livekit_url"):
        config.validate()


def test_sidecar_config_accepts_backend_issued_custom_identity() -> None:
    """验证侧车接受后端显式签发的自定义 AEC 身份。

    功能：覆盖 backend 调整 AEC identity 前缀后的启动兼容性。
    核心入参：identity 使用不含 desktop-aec- 前缀的 backend 返回值。
    预期输出：validate 正常返回，sidecar 后续只按显式 identity 与 ready 事件校验。
    边界异常：若配置层仍硬编码 desktop-aec- 前缀，本测试会因 ValueError 失败。
    """
    # 后端是 AEC 身份的签发者，sidecar 只验证它是非空字符串，不在本地重复猜测前缀策略。
    config = make_config(identity="microphone-worker")

    config.validate()


def test_sidecar_config_rejects_blank_identity() -> None:
    """验证侧车身份为空白时被拒绝。"""
    # 空白身份无法让 supervisor 校验 ready 事件，也无法让 LiveKit 房间区分 clean 麦克风发布者。
    config = make_config(identity="   ")

    with pytest.raises(ValueError, match="identity"):
        config.validate()


def test_sidecar_config_rejects_string_parent_pid_with_field_specific_value_error() -> None:
    """验证 parent_pid 类型错误时抛出字段级 ValueError。"""
    # 命令行或环境变量可能把 pid 作为字符串传入，配置层应明确拒绝而不是触发 TypeError。
    config = make_config(parent_pid="1")

    with pytest.raises(ValueError, match="parent_pid"):
        config.validate()


def test_sidecar_config_rejects_non_positive_parent_pid() -> None:
    """验证父进程编号必须是正整数。"""
    # 非正 pid 无法执行可靠的父进程探活，继续启动会让侧车失去退出依据。
    config = make_config(parent_pid=0)

    with pytest.raises(ValueError, match="parent_pid"):
        config.validate()


@pytest.mark.parametrize("field_name", ["sample_rate", "clean_output_sample_rate"])
def test_sidecar_config_rejects_sample_rates_that_are_not_positive(field_name: str) -> None:
    """验证采样率必须大于零。"""
    # 零采样率无法表达真实 PCM 时间轴，也会导致后续帧长计算失去意义。
    config = make_config(**{field_name: 0})

    with pytest.raises(ValueError, match=field_name):
        config.validate()


@pytest.mark.parametrize("field_name", ["sample_rate", "clean_output_sample_rate"])
def test_sidecar_config_rejects_sample_rates_that_cannot_form_10ms_frames(field_name: str) -> None:
    """验证采样率必须能被 100 整除。"""
    # AEC 与发布链路按 10ms 切帧，不能被 100 整除会产生非整数样本数。
    config = make_config(**{field_name: 44101})

    with pytest.raises(ValueError, match=field_name):
        config.validate()


def test_sidecar_config_rejects_non_mono_channels() -> None:
    """验证当前侧车只接受单声道输入输出。"""
    # Task 3 的运行时边界明确限定为单声道，立体声需要独立设计同步与帧长规则。
    config = make_config(channels=2)

    with pytest.raises(ValueError, match="channels"):
        config.validate()


def test_sidecar_config_rejects_negative_warmup_seconds() -> None:
    """验证预热秒数不能为负数。"""
    # 负数预热时间没有业务含义，会让调度层出现反直觉的时间计算。
    config = make_config(warmup_seconds=-0.1)

    with pytest.raises(ValueError, match="warmup_seconds"):
        config.validate()


def test_sidecar_config_rejects_non_positive_max_debug_runs() -> None:
    """验证 debug run 保留上限必须是正整数。"""
    # 0 次或负数都无法表达“保留最近几次启动”的有效策略。
    config = make_config(max_debug_runs=0)

    with pytest.raises(ValueError, match="max_debug_runs"):
        config.validate()
