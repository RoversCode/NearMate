"""NearMate 安装 profile 的运行期选择。

安装器通过环境变量 ``NEARMATE_RUNTIME_PROFILE`` 告诉 Backend 当前随包的
原生运行环境。这里集中保存 profile 到实际 provider/backend 的映射，避免
ASR 和 StreamVox TTS 按安装 profile 选择后端；Text EOU 固定 CPU，避免小模型在不同 GPU provider 上产生不可接受的概率差异。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal


RuntimeProfile = Literal["cuda", "universal"]


@dataclass(frozen=True, slots=True)
class RuntimeProfileDefaults:
    """描述一个 NearMate 安装 profile 的 GPU 组件默认后端。"""

    name: RuntimeProfile
    asr_onnx_provider: str
    streamvox_onnx_provider: str
    text_eou_provider: str
    llama_backend: Literal["cuda", "vulkan"]


_PROFILE_DEFAULTS: dict[RuntimeProfile, RuntimeProfileDefaults] = {
    "cuda": RuntimeProfileDefaults(
        name="cuda",
        asr_onnx_provider="cuda",
        streamvox_onnx_provider="cuda",
        text_eou_provider="CPUExecutionProvider",
        llama_backend="cuda",
    ),
    "universal": RuntimeProfileDefaults(
        name="universal",
        asr_onnx_provider="dml",
        streamvox_onnx_provider="directml",
        text_eou_provider="CPUExecutionProvider",
        llama_backend="vulkan",
    ),
}


def resolve_runtime_profile(value: str | None = None) -> RuntimeProfile:
    """读取并校验当前安装 profile。

    Args:
        value: 可选的显式 profile；为空时读取 ``NEARMATE_RUNTIME_PROFILE``。

    Returns:
        ``cuda`` 或 ``universal``。未设置时保留现有 CUDA 开发环境默认值。

    Raises:
        ValueError: profile 不是受支持的值时抛出。
    """

    raw_value = value if value is not None else os.environ.get("NEARMATE_RUNTIME_PROFILE", "cuda")
    normalized = raw_value.strip().lower()
    if normalized not in _PROFILE_DEFAULTS:
        raise ValueError("NEARMATE_RUNTIME_PROFILE 只能是 cuda 或 universal")
    return normalized  # type: ignore[return-value]


def runtime_profile_defaults(value: str | None = None) -> RuntimeProfileDefaults:
    """返回当前 profile 的 provider/backend 默认值。

    Args:
        value: 可选的显式 profile。

    Returns:
        不可变的 profile 默认值快照。
    """

    return _PROFILE_DEFAULTS[resolve_runtime_profile(value)]


def normalize_execution_selection(
    onnx_provider: str,
    llama_backend: str,
    value: str | None = None,
) -> tuple[str, str]:
    """把持久化执行选择收敛到当前安装 profile 支持的后端组合。

    Args:
        onnx_provider: 持久化的 ONNX provider 名称。
        llama_backend: 持久化的 llama.cpp 后端名称。
        value: 可选的显式安装 profile；为空时读取当前环境。

    Returns:
        当前 profile 支持的 `(onnx_provider, llama_backend)`。兼容选择原样保留，
        来自另一种安装包的旧选择改用当前 profile 的 GPU 默认值。
    """

    defaults = runtime_profile_defaults(value)
    profile_onnx_provider = "cuda" if defaults.name == "cuda" else "directml"
    if onnx_provider in {profile_onnx_provider, "cpu"} and llama_backend in {
        defaults.llama_backend,
        "cpu",
    }:
        return onnx_provider, llama_backend
    return profile_onnx_provider, defaults.llama_backend
