from __future__ import annotations

from dataclasses import dataclass
import logging
from pathlib import Path
from typing import Any

import numpy as np
from companion.observability.logging_config import get_backend_logger
from companion.audio.bootstrap.asset_registry import resolve_local_audio_asset_paths
from companion.audio.bootstrap.wakeword_runtime import (
    DECODER_MODEL_NAME,
    ENCODER_MODEL_NAME,
    JOINER_MODEL_NAME,
    WakewordRuntimeConfig,
    create_keyword_spotter,
    keywords_to_tokens,
)

logger = get_backend_logger(__name__)


@dataclass(frozen=True)
class WakewordProviderConfig:
    """真实唤醒词 provider 的轻量配置。"""

    # 默认模型目录保持为用户可断言的仓库相对路径，同时真实 runtime 构造仍复用 bootstrap 的绝对路径解析。
    model_dir: Path | str = Path("backend/ckpt/wakeword")
    # 可选 runtime 配置用于高级调用方显式覆盖；普通单元测试无需构造重型模型对象。
    runtime_config: WakewordRuntimeConfig | None = None
    # 唤醒词文本来自控制台用户设置，用于追踪当前 stream token 的人类可读来源。
    keywords: tuple[str, ...] = ()
    # sherpa KeywordSpotter 创建 stream 所需的关键词 token 串；为空时由 provider 初始化时根据 keywords 生成。
    keywords_token: str | None = None
    # 当前 provider 使用的 sherpa-onnx 执行后端，优先来自 runtime_config，字段保留用于测试和观测元数据。
    provider: str = "cpu"
    # 当前 provider 使用的设备编号，CUDA 场景代表 GPU 序号。
    device: int = 0
    # 当前 provider 使用的 sherpa-onnx 推理线程数。
    num_threads: int = 2


class RealWakewordProvider:
    """基于本地 sherpa-onnx KWS 的真实唤醒词 provider。"""

    def __init__(
        self,
        *,
        config: WakewordProviderConfig | None = None,
        spotter: Any | None = None,
    ) -> None:
        """初始化真实唤醒词 provider。"""

        # 真实 provider 只接受 sherpa KeywordSpotter 流式协议对象；callable fake 会绕过 stream 生命周期。
        if callable(spotter):
            raise TypeError("RealWakewordProvider 只接受 sherpa KeywordSpotter 流式协议对象")

        # 配置保存关键词与模型路径，后续 reset 和 detect 都复用同一份初始化结果。
        self.config = config or WakewordProviderConfig()
        # spotter 允许集成层注入已经创建好的 KeywordSpotter 协议对象；未注入时构造阶段创建真实对象。
        self._spotter = spotter or self._create_spotter()
        # sherpa stream 必须跨音频 chunk 长期存在；构造阶段绑定关键词，避免首次用户音频承担初始化成本。
        self._stream = self._spotter.create_stream(self._get_keywords_token())
        # 观测字段从最终 runtime config 读取，worker 打点时无需理解 provider 内部配置层级。
        runtime_config = self._get_runtime_config()
        self.provider = runtime_config.provider
        self.device = runtime_config.device
        self.num_threads = runtime_config.num_threads

    def detect(self, pcm16: bytes) -> bool:
        """检测 PCM16 音频是否命中唤醒词。"""

        # 空音频没有唤醒语义，直接返回，避免把无内容帧写入长期 stream。
        if not pcm16:
            return False

        # 初始化阶段已经保证 spotter 和 stream 就绪，detect 只负责把当前 PCM 喂入长期 stream。
        return _detect_with_sherpa_streaming_api(
            self._spotter,
            self._stream,
            pcm16,
        )

    def reset(self) -> None:
        """重置当前唤醒词检测流。"""

        # 公开 reset 作用于当前长期 stream，供 track 切换或会话重置清理唤醒上下文。
        self._spotter.reset_stream(self._stream)

    def _create_spotter(self) -> Any:
        """创建底层唤醒词 spotter。"""

        # runtime_config 显式传入时优先使用；否则必须从 provider 轻量 model_dir 派生，避免忽略调用方路径覆盖。
        runtime_config = self._get_runtime_config()
        return create_keyword_spotter(runtime_config)

    def _get_keywords_token(self) -> str:
        """获取或生成 sherpa stream 所需关键词 token。"""

        # 测试或高级调用方已经提供 token 时直接复用，避免重复 text2token。
        if self.config.keywords_token:
            return self.config.keywords_token

        # 没有关键词就无法创建有效唤醒 stream，直接失败比创建空 stream 更容易诊断配置问题。
        if not self.config.keywords:
            raise ValueError("wakeword_keywords 至少需要配置一个唤醒词")

        runtime_config = self._get_runtime_config()
        logger.info(f"当前角色卡的唤醒词；{self.config.keywords}")
        return keywords_to_tokens(self.config.keywords, runtime_config)

    def _get_runtime_config(self) -> WakewordRuntimeConfig:
        """获取当前 provider 使用的唤醒词运行时配置。"""

        # 生产装配会显式传入完整 runtime_config；测试或高级调用只传 model_dir 时才回退轻量派生。
        if self.config.runtime_config is not None:
            return self.config.runtime_config

        # 轻量派生路径仍保留执行设备字段，避免不经 RealtimeInputRuntime 构造的调用丢失观测信息。
        return _build_wakeword_runtime_config_from_model_dir(
            self.config.model_dir,
            keywords=self.config.keywords,
            provider=self.config.provider,
            device=self.config.device,
            num_threads=self.config.num_threads,
        )


def _detect_with_sherpa_streaming_api(spotter: Any, stream: Any, pcm16: bytes) -> bool:
    """按 sherpa KeywordSpotter 真实流式 API 执行一次唤醒检测。"""

    # sherpa stream 接收 float32 waveform，不接收 PCM16 字节；转换集中在 provider 边界完成。
    waveform = _pcm16_to_float32_waveform(pcm16)
    stream.accept_waveform(16000, waveform)

    # sherpa 可能需要多轮 decode 才产出结果，因此按 ready 状态持续推进当前 stream。
    while spotter.is_ready(stream):
        spotter.decode_stream(stream)
        result = spotter.get_result(stream)
        # 任意非空结果都代表命中唤醒词；命中后立刻 reset，避免下一帧重复触发同一结果。
        if result:
            spotter.reset_stream(stream)
            return True

    # 没有任何非空结果时保持未命中，让上层继续监听后续音频。
    return False


def _pcm16_to_float32_waveform(pcm16: bytes) -> np.ndarray:
    """把 PCM16 little-endian 字节转换为 sherpa stream 需要的一维 float32 waveform。"""

    # PCM16 每个采样点占 2 字节，奇数字节意味着上游帧边界损坏，必须在 provider 边界拒绝。
    if len(pcm16) % 2 != 0:
        raise ValueError("PCM16 字节长度必须按 int16 对齐，当前收到奇数字节长度")

    # 明确小端解析，保证 Windows/Linux 上输入真实 sherpa 的 waveform 数值一致。
    int_samples = np.frombuffer(pcm16, dtype="<i2")
    return (int_samples.astype(np.float32) / 32768.0).astype(np.float32, copy=False)


def _build_wakeword_runtime_config_from_model_dir(
    model_dir: Path | str,
    *,
    keywords: tuple[str, ...] = (),
    provider: str = "cpu",
    device: int = 0,
    num_threads: int = 2,
) -> WakewordRuntimeConfig:
    """从 provider 轻量模型目录构造唤醒词运行时配置。"""

    # provider 的 model_dir 可能是默认相对路径或调用方传入的绝对路径，统一解析后再派生模型文件路径。
    resolved_model_dir = _resolve_repo_local_model_dir(model_dir, error_label="唤醒词模型目录")
    return WakewordRuntimeConfig(
        model_dir=resolved_model_dir,
        keywords_file=resolved_model_dir / "keywords.txt",
        tokens_file=resolved_model_dir / "tokens.txt",
        encoder=resolved_model_dir / ENCODER_MODEL_NAME,
        decoder=resolved_model_dir / DECODER_MODEL_NAME,
        joiner=resolved_model_dir / JOINER_MODEL_NAME,
        keywords=keywords,
        provider=provider,
        device=device,
        num_threads=num_threads,
    )


def _resolve_repo_local_model_dir(model_dir: Path | str, *, error_label: str) -> Path:
    """解析 provider 模型目录并限制其只能位于当前仓库内。"""

    # asset registry 是当前语音链路的仓库根来源，相对路径按仓库根解析而不是按 pytest/backend 工作目录解析。
    asset_paths = resolve_local_audio_asset_paths()
    raw_model_dir = Path(model_dir).expanduser()
    resolved_model_dir = raw_model_dir.resolve() if raw_model_dir.is_absolute() else (asset_paths.repo_root / raw_model_dir).resolve()
    resolved_repo_root = asset_paths.repo_root.resolve()

    # 所有真实语音模型都必须留在本仓库内，防止 provider 静默依赖外部参考项目或用户机器上的临时目录。
    if not resolved_model_dir.is_relative_to(resolved_repo_root):
        raise ValueError(f"{error_label}必须位于当前仓库内：{resolved_model_dir}")
    return resolved_model_dir
