"""保存用户确认的推理方式，不把安装包名称当成设备选择。"""

from __future__ import annotations

import json
import os
from pathlib import Path
import time
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from companion.core import runtime_paths


class ExecutionSelection(BaseModel):
    """一次明确确认的 ONNX 与 llama.cpp 执行组合。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    onnx_provider: Literal["cuda", "directml", "cpu"]
    llama_backend: Literal["cuda", "vulkan", "cpu"]

    @property
    def ort_provider(self) -> str:
        """返回 ONNX Runtime 实际接受的执行提供方名称。"""

        return {
            "cuda": "CUDAExecutionProvider",
            "directml": "DmlExecutionProvider",
            "cpu": "CPUExecutionProvider",
        }[self.onnx_provider]


class ExecutionConfiguration(BaseModel):
    """持久化的执行选择及用户模型根目录。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[1] = 1
    selection: ExecutionSelection
    model_directory: str = Field(min_length=1)


def read_execution_configuration(path: Path) -> ExecutionConfiguration | None:
    """读取经过本轮确认的配置并检查模型目录边界。

    安装 profile 可能在升级时变化，后端选择的兼容迁移由各运行时入口统一处理；
    这里保留用户原始确认快照，避免读取配置时丢失可复用的语音缓存。

    Args:
        path: 当前用户的执行配置文件。
    Returns:
        结构或目录边界合法的配置；不存在或损坏时返回空值。
    """

    try:
        configuration = ExecutionConfiguration.model_validate_json(path.read_text(encoding="utf-8"))
        # 旧配置也要检查升级边界，不能只在用户重新选择目录时生效。
        resolve_user_model_directory(configuration.model_directory)
        return configuration
    except (OSError, ValueError):
        return None


def resolve_user_model_directory(directory: str) -> Path:
    """解析用户模型路径，确保升级程序不能把模型当作安装文件删除。

    Args:
        directory: 用户选定或持久配置中的绝对模型目录。
    Returns:
        位于安装目录之外的真实路径；此检查不会创建目录或文件。
    """

    path = Path(directory).expanduser()
    if not path.is_absolute():
        raise ValueError("请选择模型的绝对目录")
    path = path.resolve()
    # 先解析目录联接，再判断归属，避免别名把下载内容引入安装目录。
    if os.environ.get("NEARMATE_PACKAGED") == "1":
        resource_root = runtime_paths.RESOURCE_ROOT.resolve()
        installation_root = resource_root.parent
        if path.is_relative_to(resource_root) or path.is_relative_to(installation_root):
            raise ValueError("模型不能保存到 NearMate 安装目录，请选择用户目录或其他磁盘")
    return path


def atomic_write_json(path: Path, payload: object) -> None:
    """原子提交 JSON，并短暂重试 Windows 文件占用，避免留下半份配置。

    Args:
        path: 已确定的目标文件。
        payload: 可序列化的配置或任务状态。
    Returns:
        无返回值。
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        # Windows 的普通读取和安全扫描也可能暂时阻止替换，不能把一次占用当作任务失败。
        # 总等待限制为 1.55 秒；持续权限错误仍需上报，并保留旧文件。
        retry_delays = iter((0.05, 0.1, 0.2, 0.4, 0.8))
        while True:
            try:
                os.replace(temporary, path)
                break
            except OSError as error:
                if getattr(error, "winerror", None) not in {5, 32, 33}:
                    raise
                delay = next(retry_delays, None)
                if delay is None:
                    raise
                time.sleep(delay)
    finally:
        temporary.unlink(missing_ok=True)
