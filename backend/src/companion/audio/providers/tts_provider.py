from __future__ import annotations

import time
import os
import wave
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from io import BytesIO
from pathlib import Path
from threading import RLock
from typing import Any

import numpy as np

from companion.audio.providers.base import TtsChunk
from companion.audio.streamvox_models import resolve_streamvox_model_profile
from companion.core.streamvox_runtime_config import read_prepared_streamvox_voice
from companion.observability.logging_config import get_backend_logger


logger = get_backend_logger(__name__)


# 中文与英文句末标点都可触发 TTS 片段提交；逗号不切，避免太碎导致语音韵律破裂。
SENTENCE_BOUNDARY_PUNCTUATION = frozenset({"。", "！", "？", "!", "?", "；", ";"})
# 每个 StreamVox 模型都用同名默认音色做启动锚点，避免跨模型复用旧角色名。
DEFAULT_STREAMVOX_ROLE_NAME = "demo_role"


@dataclass(frozen=True)
class StreamVoxProviderConfig:
    """表示 StreamVox provider 的运行配置。

    Attributes:
        model: 当前使用的 StreamVox 模型名。
        device: 推理设备配置。
        llama_backend: GGUF 模型使用的 llama.cpp 后端策略。
        reference_audio_path: 默认音色使用的参考音频路径。
        role_name: 默认音色角色名。
        prompt_text: 参考音频对应的提示文本。
        language: 合成语言配置。
        streamvox_seed: StreamVox 推理随机种子。
        inference_options: 当前模型族专属推理参数。
        allow_missing_requested_role_creation: 是否允许用当前参考资产创建缺失的请求 role。
        track_performance: 是否启用 StreamVox 内部性能跟踪。
        model_directory: 已下载并通过清单校验的模型目录。
        voice_directory: 当前角色素材指纹对应的缓存目录。
        allow_development_fallback: 源码开发态是否允许使用旧的按模型名加载方式。
        onnx_provider: 已确认的 ONNX 后端。
        execution_config_path: 用户确认的执行配置文件。
        voice_setup_path: 用户确认的语音配置状态文件。
    """

    # Provider 只保存模型推理配置，语音准备状态由首次配置服务管理。
    # StreamVox 默认运行模型使用轻量 Qwen3 克隆模型，优先保证实时聊天首包延迟。
    model: str = "qwen3-tts-clone-0.6b-gguf"
    # 设备默认交给 StreamVox 自动选择，兼容 CPU/GPU 不同机器。
    device: str = "auto"
    # None 表示不显式传 llama_backend，由 StreamVox SDK 使用默认后端选择策略。
    llama_backend: str | None = None
    # StreamVox make_prompt 需要的参考音频路径，通常来自 backend/ckpt/streamvox/Condition3.wav。
    reference_audio_path: Path | None = None
    # demo_role 是项目在每个 StreamVox 模型下维护的默认音色名；不同模型的同名角色由 StreamVox 自身隔离。
    role_name: str = DEFAULT_STREAMVOX_ROLE_NAME
    # 参考音频对应文本，用于构造 voice prompt。
    prompt_text: str = "所以今天我想要讨论另外两个问题，很相似。可是却不一样的问题，我相信自己是谁。"
    # StreamVox 默认中文输出；后续多语言可在 provider 配置层扩展。
    language: str = "chinese"
    # None 表示不向 StreamVox 显式传 seed，让 SDK 使用模型默认采样行为。
    streamvox_seed: int | None = None
    # 按模型族保存额外推理参数，例如 Qwen3 language、MOSS mode 或 VoxCPM2 control_text。
    inference_options: dict[str, Any] | None = None
    # 只有角色卡资产完整的启动路径会打开该开关；全局兜底和手动切换不能自动创建任意缺失 role。
    allow_missing_requested_role_creation: bool = False
    # 流式推理性能跟踪默认关闭，避免测试与生产日志噪声。
    track_performance: bool = False
    model_directory: Path | None = None
    voice_directory: Path | None = None
    onnx_provider: str | None = None
    execution_config_path: Path | None = None
    voice_setup_path: Path | None = None
    allow_development_fallback: bool = False


def split_text_for_tts(text: str) -> list[str]:
    """按句末标点优先切分待合成文本。

    Args:
        text: 待切分的完整文本。

    Returns:
        适合逐段送入 TTS 的文本片段列表。
    """

    # 空白内容没有播报意义，直接返回空列表让上层继续等待更多 LLM token。
    if not text.strip():
        return []

    # current 保存当前候选片段；遇到句末标点后立即提交，降低边生成边播的等待时间。
    segments: list[str] = []
    current = ""
    for char in text:
        current += char

        # 标点级切句的业务意图是“短句一到就送 TTS”，但不在逗号处切断自然语气。
        if char in SENTENCE_BOUNDARY_PUNCTUATION:
            segment = current.strip()
            if segment:
                segments.append(segment)
            current = ""

    # 没有结束标点的尾段仍需返回，便于 LLM 完成时 flush 最后一段未闭合文本。
    tail = current.strip()
    if tail:
        segments.append(tail)

    return segments


class StreamVoxTtsProvider:
    """把文本句段转换为 StreamVox 流式音频块。"""

    def __init__(
        self,
        *,
        engine: Any | None = None,
        role_name: str | None = None,
        config: StreamVoxProviderConfig | None = None,
        is_generation_active: Callable[[str], bool] | None = None,
    ) -> None:
        """初始化 StreamVox provider。

        Args:
            engine: 可选的已构造 StreamVox engine，主要用于测试注入。
            role_name: 可选的默认角色名；未传入时会确保默认角色存在。
            config: provider 运行配置。
            is_generation_active: generation 有效性检查函数，用于打断时及时停止合成。
        """

        # engine 生命周期锁保证模型切换不会关闭仍在流式推理中的底层引擎。
        self._engine_lock = RLock()
        # 配置集中保存，避免 stream_segment 每次重复声明模型、语言和音色策略。
        self.config = config or StreamVoxProviderConfig()
        # generation 检查用于插话打断；未传入时默认当前 generation 始终有效。
        self._is_generation_active = is_generation_active or (lambda generation_id: True)
        # engine 注入用于单元测试；生产路径未注入时必须在构造阶段立即加载真实 StreamVox。
        self._engine = self._build_engine(engine=engine)
        # sample_rate 来自真实 StreamVox runtime，不再依赖 TtsChunk 的 24000Hz 默认值。
        self._sample_rate = int(self._engine.runtime.sample_rate)

        requested_role_name = role_name if role_name is not None else self.config.role_name
        self._role_name = self._resolve_requested_role_or_default(
            engine=self._engine,
            config=self.config,
            requested_role_name=requested_role_name,
        )

    def _build_engine(self, *, engine: Any | None = None, config: StreamVoxProviderConfig | None = None) -> Any:
        """创建或接收一个 StreamVox engine。

        Args:
            engine: 外部注入的 engine。
            config: 创建 engine 时使用的配置；未传入时使用当前实例配置。

        Returns:
            可用于合成的 StreamVox engine。
        """

        # active_config 允许 switch_config 先构造新 engine，再决定是否替换当前运行态。
        active_config = config or self.config
        # 记录 engine 初始化耗时，用于区分模型加载、授权和设备选择带来的启动成本。
        engine_started_at = time.perf_counter()
        if engine is None:
            from streamvox import TTSEngine

            if active_config.execution_config_path is None or active_config.voice_setup_path is None:
                if not active_config.allow_development_fallback or os.environ.get("NEARMATE_PACKAGED") == "1":
                    raise RuntimeError("正式语音需要用户已确认的语音配置状态")
                # 源码开发态沿用旧的按模型名加载方式，避免首次开发启动被安装版语音门禁阻断。
                created_engine = TTSEngine(
                    model=active_config.model,
                    device=active_config.device,
                    llama_backend=_streamvox_llama_backend_arg(active_config.llama_backend),
                    verify_model_sha256=False,
                )
                engine_source = "development_legacy"
            else:
                prepared = read_prepared_streamvox_voice(
                    execution_config_path=active_config.execution_config_path,
                    voice_setup_path=active_config.voice_setup_path,
                    model=active_config.model,
                    role_name=active_config.role_name,
                )
                if (
                    active_config.model_directory != prepared.model_directory
                    or active_config.voice_directory != prepared.voice_directory
                    or active_config.onnx_provider != prepared.onnx_provider
                    or active_config.llama_backend != prepared.llama_backend
                ):
                    raise RuntimeError("正式语音参数与已确认状态不一致")
                if active_config.model_directory is None or active_config.voice_directory is None:
                    raise RuntimeError("正式语音需要已确认的模型目录和角色缓存目录")
                model_directory = active_config.model_directory.resolve()
                if not active_config.model_directory.is_absolute() or not (model_directory / "manifest.json").is_file():
                    raise RuntimeError("本地模型尚未下载完整，请先完成语音配置")
                if not active_config.voice_directory.is_absolute() or not active_config.voice_directory.is_dir():
                    raise RuntimeError("当前角色缓存目录不存在，请先完成语音配置")
                if active_config.onnx_provider not in {"cuda", "directml", "cpu"}:
                    raise RuntimeError("正式语音需要明确确认 ONNX 执行方式")
                os.environ["STREAMVOX_HOME"] = str(active_config.voice_directory.resolve())
                created_engine = TTSEngine(
                    model=str(model_directory),
                    device=active_config.device,
                    llama_backend=_streamvox_llama_backend_arg(active_config.llama_backend),
                    onnx_provider=active_config.onnx_provider,
                    verify_model_sha256=True,
                )
                engine_source = "constructed"
        else:
            created_engine = engine
            engine_source = "injected"
        engine_elapsed_ms = (time.perf_counter() - engine_started_at) * 1000
        logger.info(
            "StreamVox engine initialized model=%s device=%s llama_backend=%s source=%s elapsed_ms=%.2f",
            active_config.model,
            active_config.device,
            active_config.llama_backend,
            engine_source,
            engine_elapsed_ms,
        )
        return created_engine

    def _shutdown_engine(self, engine: Any, *, reason: str) -> None:
        """关闭一个 StreamVox engine。

        Args:
            engine: 待关闭的 engine。
            reason: 关闭原因，用于日志诊断。
        """

        # 部分 fake 或旧版 StreamVox engine 可能没有 shutdown，生命周期收口在能力存在时执行。
        shutdown_engine = getattr(engine, "shutdown", None)
        if callable(shutdown_engine):
            logger.info("StreamVox engine shutdown reason=%s", reason)
            shutdown_engine()

    def runtime_state(self) -> dict[str, Any]:
        """返回当前 StreamVox 运行状态。

        Returns:
            面向控制台和管理接口的运行状态快照。
        """

        return {
            "model": self.config.model,
            "device": self.config.device,
            "llama_backend": self.config.llama_backend,
            "onnx_provider": self.config.onnx_provider,
            "model_directory": str(self.config.model_directory) if self.config.model_directory else None,
            "voice_directory": str(self.config.voice_directory) if self.config.voice_directory else None,
            "actual_backends": getattr(self._engine, "actual_backends", {}),
            "role_name": str(self._role_name),
            "streamvox_seed": self.config.streamvox_seed,
            "inference_options": _normalize_inference_options(
                model=self.config.model,
                inference_options=self.config.inference_options,
            ),
            "sample_rate": self._sample_rate,
            "roles": self.list_roles(),
        }

    def list_roles(self) -> list[str]:
        """列出当前模型下已缓存的角色。

        Returns:
            当前模型下可用的角色名列表。
        """

        # roles 是外部 SDK 返回值，统一转成普通字符串列表，避免前端收到 tuple 或其它可迭代对象。
        return [str(role) for role in self._engine.list_roles()]

    def ensure_default_role(self) -> str:
        """确保当前模型下存在默认持久音色。

        Returns:
            当前可用的默认角色名。
        """

        return self._ensure_default_role_for_engine(engine=self._engine, config=self.config)

    def _resolve_requested_role_or_default(
        self,
        *,
        engine: Any,
        config: StreamVoxProviderConfig,
        requested_role_name: str | None,
    ) -> str:
        """解析请求音色，不存在时回落到默认音色。

        Args:
            engine: 目标 StreamVox engine。
            config: 与该 engine 对应的配置。
            requested_role_name: 外部希望使用的 role。

        Returns:
            当前模型下最终可用的 role 名称。
        """

        normalized_requested_role_name = (requested_role_name or "").strip()
        roles = [str(role) for role in engine.list_roles()]
        if normalized_requested_role_name and normalized_requested_role_name in roles:
            logger.info(
                "StreamVox prompt initialized role_name=%s reference_audio_path=%s source=requested_existing elapsed_ms=%.2f",
                normalized_requested_role_name,
                config.reference_audio_path,
                0.0,
            )
            return normalized_requested_role_name

        if normalized_requested_role_name:
            if config.allow_missing_requested_role_creation:
                return self._create_requested_role_for_engine(
                    engine=engine,
                    config=config,
                    requested_role_name=normalized_requested_role_name,
                )
            logger.warning(
                "StreamVox requested role missing; falling back to default role requested_role_name=%s default_role_name=%s",
                normalized_requested_role_name,
                DEFAULT_STREAMVOX_ROLE_NAME,
            )
        return self._ensure_default_role_for_engine(
            engine=engine,
            config=self._config_with_role_name(config, DEFAULT_STREAMVOX_ROLE_NAME),
        )

    def _create_requested_role_for_engine(
        self,
        *,
        engine: Any,
        config: StreamVoxProviderConfig,
        requested_role_name: str,
    ) -> str:
        """用受信任资产创建缺失的请求音色。

        Args:
            engine: 目标 StreamVox engine。
            config: 包含参考音频和 prompt 的 provider 配置。
            requested_role_name: 需要创建的角色卡音色名。

        Returns:
            创建完成后的 role 名称。
        """

        normalized_prompt_text = config.prompt_text.strip()
        if config.reference_audio_path is None or not normalized_prompt_text:
            raise ValueError("创建角色卡 StreamVox 音色需要 reference_audio_path 和 prompt_text")
        prompt_started_at = time.perf_counter()
        engine.make_prompt(
            role_name=requested_role_name,
            audio_path=str(config.reference_audio_path),
            prompt_text=normalized_prompt_text,
            persist=True,
        )
        prompt_elapsed_ms = (time.perf_counter() - prompt_started_at) * 1000
        logger.info(
            "StreamVox requested role initialized role_name=%s reference_audio_path=%s source=created_requested elapsed_ms=%.2f",
            requested_role_name,
            config.reference_audio_path,
            prompt_elapsed_ms,
        )
        return requested_role_name

    def _ensure_default_role_for_engine(self, *, engine: Any, config: StreamVoxProviderConfig) -> str:
        """确保指定 engine 对应模型下存在默认持久音色。

        Args:
            engine: 目标 StreamVox engine。
            config: 与该 engine 对应的配置。

        Returns:
            已存在或新创建的默认角色名。
        """

        # prompt 初始化是启动路径的关键耗时，单独计时便于定位默认音色首次构建成本。
        prompt_started_at = time.perf_counter()
        roles = [str(role) for role in engine.list_roles()]
        if config.role_name in roles:
            prompt_elapsed_ms = (time.perf_counter() - prompt_started_at) * 1000
            logger.info(
                "StreamVox prompt initialized role_name=%s reference_audio_path=%s source=existing elapsed_ms=%.2f",
                config.role_name,
                config.reference_audio_path,
                prompt_elapsed_ms,
            )
            return config.role_name

        # 默认音色必须落盘缓存，业务意图是首句正式合成不再承担参考音频编码开销。
        if config.reference_audio_path is None:
            # 这是运行态缺少已准备素材，而不是调用参数格式错误；上层统一按
            # RuntimeError 将它转成可恢复的配置阻断。
            raise RuntimeError("当前角色音色尚未准备：创建 StreamVox 默认音色需要 reference_audio_path")
        engine.make_prompt(
            role_name=config.role_name,
            audio_path=str(config.reference_audio_path),
            prompt_text=config.prompt_text,
            persist=True,
        )
        prompt_elapsed_ms = (time.perf_counter() - prompt_started_at) * 1000
        logger.info(
            "StreamVox prompt initialized role_name=%s reference_audio_path=%s source=created elapsed_ms=%.2f",
            config.role_name,
            config.reference_audio_path,
            prompt_elapsed_ms,
        )
        return config.role_name

    def _config_with_role_name(self, config: StreamVoxProviderConfig, role_name: str) -> StreamVoxProviderConfig:
        """用指定 role_name 派生 provider 配置。"""

        return StreamVoxProviderConfig(
            model=config.model,
            device=config.device,
            llama_backend=config.llama_backend,
            reference_audio_path=config.reference_audio_path,
            role_name=role_name,
            prompt_text=config.prompt_text,
            language=config.language,
            streamvox_seed=config.streamvox_seed,
            inference_options=config.inference_options,
            allow_missing_requested_role_creation=config.allow_missing_requested_role_creation,
            track_performance=config.track_performance,
            model_directory=config.model_directory,
            voice_directory=config.voice_directory,
            onnx_provider=config.onnx_provider,
            execution_config_path=config.execution_config_path,
            voice_setup_path=config.voice_setup_path,
            allow_development_fallback=config.allow_development_fallback,
        )

    def _switch_runtime_options_without_engine_rebuild(
        self,
        *,
        config: StreamVoxProviderConfig,
        role_name: str,
    ) -> dict[str, Any]:
        """在当前 engine 内切换默认音色和推理参数。

        Args:
            config: 已归一化的新运行配置。
            role_name: 目标默认音色名。

        Returns:
            更新后的运行状态快照。
        """

        # 同模型同设备只需要改变运行态配置，重建 engine 会造成无意义的模型卸载和重新加载。
        next_role_name = self._resolve_requested_role_or_default(
            engine=self._engine,
            config=config,
            requested_role_name=role_name,
        )
        self.config = config
        self._role_name = next_role_name
        return self.runtime_state()

    def delete_role(self, role_name: str) -> Any:
        """删除当前模型下的指定缓存角色。

        Args:
            role_name: 待删除的角色名。

        Returns:
            StreamVox SDK 返回的删除结果。
        """

        # 角色名先裁剪，避免 UI 传入不可见空白导致 StreamVox 尝试删除错误角色。
        normalized_role_name = role_name.strip()
        if not normalized_role_name:
            raise ValueError("StreamVox role_name must not be blank")
        return self._engine.del_roles(normalized_role_name)

    def create_role(self, *, role_name: str, audio_path: Path, prompt_text: str) -> dict[str, Any]:
        """在当前模型下创建可复用角色。

        Args:
            role_name: 新角色名。
            audio_path: 参考音频路径。
            prompt_text: 参考音频对应文本。

        Returns:
            创建完成后的运行状态快照。
        """

        # 控制台输入可能携带首尾空白，进入 StreamVox 缓存键前统一裁剪，避免同一角色出现不可见差异。
        normalized_role_name = role_name.strip()
        # 参考文本必须和音频内容对应；空文本会显著降低克隆可靠性，因此在 provider 层直接拒绝。
        normalized_prompt_text = prompt_text.strip()
        if not normalized_role_name or not normalized_prompt_text:
            raise ValueError("StreamVox role_name and prompt_text must not be blank")

        # 角色缓存是当前模型粒度的事实源；已存在角色不能静默覆盖，避免用户误删稳定音色资产。
        existing_roles = self.list_roles()
        if normalized_role_name in existing_roles:
            raise ValueError(f"StreamVox role already exists: {normalized_role_name}")

        # 自定义角色是长期可复用资产，必须 persist=True，正式聊天和试听才能只传 role_name 保持低首包延迟。
        prompt_started_at = time.perf_counter()
        self._engine.make_prompt(
            role_name=normalized_role_name,
            audio_path=str(audio_path),
            prompt_text=normalized_prompt_text,
            persist=True,
        )
        prompt_elapsed_ms = (time.perf_counter() - prompt_started_at) * 1000
        logger.info(
            "StreamVox custom role created role_name=%s model=%s audio_path=%s elapsed_ms=%.2f",
            normalized_role_name,
            self.config.model,
            audio_path,
            prompt_elapsed_ms,
        )
        return self.runtime_state()

    def preview_role(self, *, role_name: str, text: str) -> dict[str, Any]:
        """使用已缓存角色生成试听 WAV。

        Args:
            role_name: 试听使用的角色名。
            text: 试听文本。

        Returns:
            包含 WAV 字节和采样率的试听结果。
        """

        # 试听是人工操作，不参与 generation 打断；这里仍裁剪输入，防止空白请求占用 TTS 资源。
        normalized_role_name = role_name.strip()
        normalized_text = text.strip()
        if not normalized_role_name or not normalized_text:
            raise ValueError("StreamVox preview role_name and text must not be blank")

        # 试听只允许已创建角色，避免在 preview 路径即时构造 prompt，破坏正式链路的低延迟语义。
        if normalized_role_name not in self.list_roles():
            raise ValueError(f"StreamVox role does not exist: {normalized_role_name}")

        # engine.stream 返回流式 float waveform；试听接口需要完整音频，因此在这里合并成一个 WAV 响应。
        preview_started_at = time.perf_counter()
        pcm16_chunks: list[bytes] = []
        raw_chunk_count = 0
        inference_kwargs = self._stream_inference_kwargs()
        try:
            for audio_chunk in self._engine.stream(
                normalized_text,
                role_name=normalized_role_name,
                track_performance=self.config.track_performance,
                **inference_kwargs,
            ):
                raw_chunk_count += 1
                pcm16_chunk = _float_audio_to_pcm16(audio_chunk)
                if pcm16_chunk:
                    pcm16_chunks.append(pcm16_chunk)
        except Exception:
            # 试听失败最常见于模型切换、角色缓存或模型族推理参数不匹配，必须把当前运行态一次性打全。
            logger.exception(
                "StreamVox role preview failed model=%s role_name=%s current_role_name=%s roles=%s "
                "sample_rate=%s inference_kwargs=%s raw_chunk_count=%s",
                self.config.model,
                normalized_role_name,
                self._role_name,
                self.list_roles(),
                self._sample_rate,
                inference_kwargs,
                raw_chunk_count,
            )
            raise

        pcm16 = b"".join(pcm16_chunks)
        wav_bytes = _pcm16_to_wav_bytes(pcm16=pcm16, sample_rate=self._sample_rate)
        preview_elapsed_ms = (time.perf_counter() - preview_started_at) * 1000
        logger.info(
            "StreamVox role preview finished role_name=%s text_length=%s elapsed_ms=%.2f "
            "raw_chunk_count=%s pcm16_bytes=%s sample_rate=%s",
            normalized_role_name,
            len(normalized_text),
            preview_elapsed_ms,
            raw_chunk_count,
            len(pcm16),
            self._sample_rate,
        )
        return {
            "wav_bytes": wav_bytes,
            "sample_rate": self._sample_rate,
        }

    def get_prompt_data(self, *, role_name: str) -> Any | None:
        """读取当前模型下指定角色的 StreamVox prompt 数据。

        Args:
            role_name: 需要查询的角色名。

        Returns:
            StreamVox SDK 返回的 PromptData；不存在时返回 None。
        """

        normalized_role_name = role_name.strip()
        if not normalized_role_name:
            raise ValueError("StreamVox role_name must not be blank")
        get_prompt_data = getattr(self._engine, "get_prompt_data", None)
        if not callable(get_prompt_data):
            raise ValueError("StreamVox engine does not support get_prompt_data")
        return get_prompt_data(normalized_role_name)

    def switch_config(
        self,
        *,
        model: str,
        device: str,
        llama_backend: str | None,
        role_name: str,
        streamvox_seed: int | None = None,
        inference_options: dict[str, Any] | None = None,
        before_switch: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        """在独占 engine 生命周期期间切换 StreamVox 配置。

        Args:
            model: 新模型名。
            device: 新设备配置。
            llama_backend: 新 llama.cpp 后端策略。
            role_name: 新默认角色名。
            streamvox_seed: 新推理随机种子。
            inference_options: 新模型族专属推理参数。
            before_switch: 输入预检通过后、等待 engine 生命周期锁之前执行的回调。

        Returns:
            切换完成后的运行状态快照。
        """

        # 配置字段统一裁剪，避免空白模型或角色进入 SDK 造成难以理解的底层错误。
        normalized_model = model.strip()
        normalized_device = device.strip() or "auto"
        normalized_llama_backend = _normalize_llama_backend(llama_backend)
        normalized_role_name = role_name.strip()
        normalized_seed = _normalize_streamvox_seed(streamvox_seed)
        normalized_inference_options = _normalize_inference_options(
            model=normalized_model,
            inference_options=inference_options,
        )
        if not normalized_model:
            raise ValueError("StreamVox model and role_name must not be blank")

        # 管理命令必须在取消当前回复前完成无副作用预检；空角色才回落到默认锚点。
        preflight_role_name = normalized_role_name or DEFAULT_STREAMVOX_ROLE_NAME
        if not preflight_role_name:
            raise ValueError("StreamVox model and role_name must not be blank")
        next_config = replace(
            self.config,
            model=normalized_model,
            device=normalized_device,
            llama_backend=normalized_llama_backend,
            role_name=preflight_role_name,
            streamvox_seed=normalized_seed,
            inference_options=normalized_inference_options,
        )
        if self.config.execution_config_path is not None and self.config.voice_setup_path is not None:
            prepared = read_prepared_streamvox_voice(
                execution_config_path=self.config.execution_config_path,
                voice_setup_path=self.config.voice_setup_path,
                model=normalized_model,
                role_name=preflight_role_name,
            )
            next_config = replace(
                next_config,
                model_directory=prepared.model_directory,
                voice_directory=prepared.voice_directory,
                onnx_provider=prepared.onnx_provider,
                llama_backend=prepared.llama_backend,
                device="cpu" if prepared.onnx_provider == "cpu" else "gpu",
            )
        elif normalized_model != self.config.model:
            raise RuntimeError("跨模型切换需要先完成新模型的语音配置")
        if before_switch is not None:
            before_switch()

        with self._engine_lock:
            # 模型切换沿用已确认的角色绑定，避免用模型名猜测另一份缓存。
            if normalized_model != self.config.model:
                normalized_role_name = preflight_role_name
            if not normalized_role_name:
                raise ValueError("StreamVox model and role_name must not be blank")

            # 新配置沿用授权、参考音频、prompt 文本和性能策略，只替换控制台可变项。
            if (
                normalized_model == self.config.model
                and normalized_device == self.config.device
                and normalized_llama_backend == self.config.llama_backend
                and next_config.onnx_provider == self.config.onnx_provider
                and next_config.model_directory == self.config.model_directory
                and next_config.voice_directory == self.config.voice_directory
            ):
                return self._switch_runtime_options_without_engine_rebuild(
                    config=next_config,
                    role_name=normalized_role_name,
                )

            # StreamVox 不同模型的原生后端会占用大量显存；切换前先释放旧 engine，避免新旧模型同时驻留。
            old_engine = self._engine
            self._shutdown_engine(old_engine, reason="switch_config_before_rebuild")

            # 旧 engine 已释放后再准备新 engine；加载失败时向上抛错，由控制台展示明确失败信息。
            next_engine: Any | None = None
            try:
                next_engine = self._build_engine(config=next_config)
                next_sample_rate = int(next_engine.runtime.sample_rate)
                next_role_name = self._resolve_requested_role_or_default(
                    engine=next_engine,
                    config=next_config,
                    requested_role_name=normalized_role_name,
                )
            except Exception:
                # 新 engine 已创建但 prompt 或采样率读取失败时，清理新资源，避免失败模型继续占用显存。
                if next_engine is not None:
                    try:
                        self._shutdown_engine(next_engine, reason="switch_config_failed")
                    except Exception:
                        logger.exception("StreamVox failed to shutdown new engine after switch failure")
                raise

            # 新运行态完全可用后再替换实例字段，避免失败配置污染当前 provider。
            self.config = next_config
            self._engine = next_engine
            self._sample_rate = next_sample_rate
            self._role_name = next_role_name
            return self.runtime_state()

    def stream_segment(self, text: str, generation_id: str) -> Iterable[TtsChunk]:
        """把单段文本转换为流式 PCM16 音频块。

        Args:
            text: 当前待合成的文本句段。
            generation_id: 所属 generation 标识。

        Yields:
            当前句段对应的流式 TTS 音频块。
        """

        # 空白 segment 没有可播报内容，直接结束迭代，避免制造无意义 final 音频块。
        if not text.strip():
            return

        # 启动前先检查 generation 是否仍有效，避免被取消的回复继续占用 TTS 资源。
        if not self._is_generation_active(generation_id):
            return

        # 句段迭代期间持有生命周期锁，确保模型切换不会关闭正在产出音频的底层 engine。
        with self._engine_lock:
            # StreamVox 原生返回 numpy waveform chunk；provider 负责转换为网关可发送的 PCM16 字节。
            stream = self._engine.stream(
                text,
                role_name=self._role_name,
                track_performance=self.config.track_performance,
                **self._stream_inference_kwargs(),
            )

            # 记录本 segment 是否已经产出过真实音频；只有有音频时才需要发送结束 marker。
            has_audio = False
            # segment_started_at 用于计算当前句段从提交给 StreamVox 到每个 chunk 到达的累计耗时。
            segment_started_at = time.perf_counter()
            # raw_chunk_index 是 StreamVox 原始 chunk 序号，空音频 chunk 也会计入，便于诊断上游输出形态。
            raw_chunk_index = 0
            # audio_chunk_count 只统计真正 yield 给播放通道的非空音频块。
            audio_chunk_count = 0
            # 没有真实音频时保留零值，保证空流的结束日志也能稳定生成。
            first_audio_elapsed_ms = 0.0
            for audio_chunk in stream:
                chunk_received_at = time.perf_counter()
                raw_chunk_index += 1
                # 每块产出前都检查 generation，插话取消后立即丢弃剩余音频。
                if not self._is_generation_active(generation_id):
                    return

                # 当前 StreamVox chunk 先转码；空 PCM 没有播放价值，必须跳过以免前端收到空普通 chunk。
                current_pcm16 = _float_audio_to_pcm16(audio_chunk)
                if not current_pcm16:
                    continue

                if audio_chunk_count == 0:
                    first_audio_elapsed_ms = (chunk_received_at - segment_started_at) * 1000
                    logger.info(
                        "StreamVox 首个音频块已生成，generation_id=%s，文本长度=%s，耗时=%.2f毫秒，原始chunk序号=%s",
                        generation_id,
                        len(text),
                        first_audio_elapsed_ms,
                        raw_chunk_index,
                    )

                # 非空音频块一到就立即产出，保证首包流式体验不被 final 标记策略阻塞。
                has_audio = True
                audio_chunk_count += 1
                yield TtsChunk(
                    generation_id=generation_id,
                    pcm16=current_pcm16,
                    sample_rate=self._sample_rate,
                    is_final=False,
                )

            segment_total_ms = (time.perf_counter() - segment_started_at) * 1000
            logger.info(
                "StreamVox 句段合成结束，generation_id=%s，文本长度=%s，总耗时=%.2f毫秒，"
                "原始chunk总数=%s，首chunk耗时=%.2f毫秒，实际音频块数=%s，是否产出音频=%s，采样率=%s",
                generation_id,
                len(text),
                segment_total_ms,
                raw_chunk_index,
                first_audio_elapsed_ms,
                audio_chunk_count,
                has_audio,
                self._sample_rate,
            )

            # 没有任何真实音频时自然结束，不额外制造空 final marker，避免前端收到无意义播放事件。
            if not has_audio:
                return

            # 结束前再次检查 generation，避免取消发生在最后一块之后时还发送 final 标记。
            if not self._is_generation_active(generation_id):
                return

            # 独立 final marker 表示 segment 结束；音频块本身保持非 final，避免为了标记结尾延迟首包。
            yield TtsChunk(
                generation_id=generation_id,
                pcm16=b"",
                sample_rate=self._sample_rate,
                is_final=True,
            )

    def _stream_inference_kwargs(self) -> dict[str, Any]:
        """构造当前模型正式推理和试听共用的 StreamVox 参数。

        Returns:
            可直接展开给 `engine.stream` 的关键字参数。
        """

        kwargs = _normalize_inference_options(
            model=self.config.model,
            inference_options=self.config.inference_options,
        )
        if self.config.streamvox_seed is not None:
            kwargs["seed"] = self.config.streamvox_seed

        # 历史默认 language 仍作为 Qwen3 兜底，避免旧配置没有 inference_options 时静默改成 SDK 默认。
        if (
            resolve_streamvox_model_profile(self.config.model).family == "qwen3"
            and "language" not in kwargs
        ):
            kwargs["language"] = self.config.language
        return kwargs


def _float_audio_to_pcm16(audio_chunk: np.ndarray) -> bytes:
    """把 StreamVox 浮点波形块转换为 PCM16 字节。

    Args:
        audio_chunk: StreamVox 返回的浮点波形块。

    Returns:
        转换后的 PCM16 原始字节。
    """

    # StreamVox 返回 float waveform，先转为一维 float32 并裁剪到可播放范围。
    audio = np.asarray(audio_chunk, dtype=np.float32).reshape(-1)
    if audio.size == 0:
        return b""

    # 使用 32767 与测试/播放侧约定一致，避免 -1.0 乘 32768 带来的不对称边界。
    clipped_audio = np.clip(audio, -1.0, 1.0)
    pcm16 = (clipped_audio * 32767.0).astype(np.int16)
    return pcm16.tobytes()


def _normalize_llama_backend(value: str | None) -> str | None:
    """归一化 StreamVox llama.cpp 后端策略。

    Args:
        value: 控制台或配置文件传入的后端策略；None 表示不显式指定。

    Returns:
        统一小写后的后端策略；空值返回 None。

    Raises:
        ValueError: 传入不支持的后端策略。
    """

    if value is None:
        return None

    normalized_value = value.strip().lower()
    if not normalized_value:
        return None

    if normalized_value not in {"cuda", "vulkan", "cpu"}:
        raise ValueError("StreamVox llama_backend must be cuda, vulkan, cpu or null")
    return normalized_value


def _streamvox_llama_backend_arg(value: str | None) -> str | None:
    """把内部后端策略转换为 StreamVox SDK 参数。

    Args:
        value: 已归一化或待归一化的后端策略。

    Returns:
        传给 TTSEngine 的 llama_backend；None 表示不给 SDK 显式后端。
    """

    normalized_value = _normalize_llama_backend(value)
    return normalized_value


def _normalize_streamvox_seed(value: Any) -> int | None:
    """归一化 StreamVox 推理随机种子。

    Args:
        value: 控制台、配置文件或管理命令传入的候选值。

    Returns:
        整数种子或 None。

    Raises:
        ValueError: 传入布尔值、非整数或负数。
    """

    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("StreamVox seed must be an integer or null")
    if value < 0:
        raise ValueError("StreamVox seed must be greater than or equal to 0")
    return value


def _normalize_inference_options(model: str, inference_options: dict[str, Any] | None) -> dict[str, Any]:
    """按模型族归一化 StreamVox 推理参数。

    Args:
        model: 当前 StreamVox 模型名。
        inference_options: 控制台提交的模型族专属参数。

    Returns:
        可传给 StreamVox SDK 的参数字典。
    """

    raw_options = inference_options or {}
    family = resolve_streamvox_model_profile(model).family
    if family == "qwen3":
        language = str(raw_options.get("language", "chinese")).strip().lower() or "chinese"
        icl = raw_options.get("icl", False)
        supported_languages = {
            "auto",
            "chinese",
            "english",
            "japanese",
            "korean",
            "german",
            "french",
            "russian",
            "portuguese",
            "spanish",
            "italian",
        }
        if language not in supported_languages:
            raise ValueError("StreamVox qwen3 language is not supported")
        if not isinstance(icl, bool):
            raise ValueError("StreamVox qwen3 icl must be true or false")
        return {"language": language, "icl": icl}

    if family == "moss":
        mode = raw_options.get("mode", 0)
        if mode not in {0, 1}:
            raise ValueError("StreamVox moss mode must be 0 or 1")
        return {"mode": mode}

    if family == "voxcpm2":
        mode = raw_options.get("mode", 1)
        if mode not in {1, 2, 3}:
            raise ValueError("StreamVox voxcpm2 mode must be 1, 2 or 3")
        normalized_options: dict[str, Any] = {"mode": mode}
        control_text = str(raw_options.get("control_text", "")).strip()
        if mode == 1 and control_text:
            normalized_options["control_text"] = control_text
        return normalized_options

    return {}


def _pcm16_to_wav_bytes(*, pcm16: bytes, sample_rate: int) -> bytes:
    """把单声道 PCM16 字节封装成 WAV 文件字节。

    Args:
        pcm16: 原始 PCM16 音频字节。
        sample_rate: 音频采样率。

    Returns:
        可直接返回给客户端或播放器的 WAV 文件字节。
    """

    # BytesIO 承载内存 WAV，避免试听接口为了临时结果写磁盘文件。
    buffer = BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        # 当前 StreamVox provider 统一输出单声道 PCM16，播放链路和测试都按这个契约解析。
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(pcm16)
    return buffer.getvalue()
