from __future__ import annotations

from dataclasses import dataclass, field, replace
import os
from pathlib import Path
from typing import Any, Literal

import yaml

from companion.core.runtime_paths import BACKEND_ROOT, USER_DATA_ROOT
from companion.core.runtime_profile import normalize_execution_selection, runtime_profile_defaults


DEFAULT_REALTIME_AUDIO_CONFIG_PATH = BACKEND_ROOT / "config" / "realtime_audio.yaml"


@dataclass(frozen=True)
class WakewordYamlConfig:
    """唤醒词 YAML 配置。"""

    # sherpa-onnx KeywordSpotter 的执行 provider，当前安装版本支持 cpu、cuda 和 coreml。
    provider: str = "cpu"
    # CUDA/CoreML 等 provider 的设备编号；CPU 路径保留 0 即可。
    device: int = 0
    # sherpa-onnx 神经网络计算线程数，CPU 路径尤其敏感，默认沿用官方构造函数默认值。
    num_threads: int = 2


@dataclass(frozen=True)
class WakeResponseYamlConfig:
    """唤醒回复 YAML 配置。"""

    # 唤醒命中后，播放唤醒回复音频前等待的毫秒数；0 表示不等待。
    playback_delay_ms: float = 500.0


@dataclass(frozen=True)
class VadYamlConfig:
    """VAD YAML 配置。"""

    # ONNX Runtime provider 名称，例如 CPUExecutionProvider 或 CUDAExecutionProvider。
    provider: str = "CPUExecutionProvider"
    # 连续命中多少个 VAD 语音窗口后才确认用户正式开口，降低单帧误判造成的 turn 抖动。
    speech_start_window_count: int = 6


@dataclass(frozen=True)
class AsrEngineYamlConfig:
    """ASR 引擎执行参数 YAML 配置。"""

    # Qwen ASR encoder 的 ONNX provider 策略；auto 会按 CUDA、DML、CPU 顺序自动选择。
    onnx_provider: str = "auto"
    # Qwen ASR 的 GGUF LLM backend 策略；auto 会根据显卡和 DLL 自动选择。
    llm_backend: Literal["vulkan", "cuda", "auto"] = "auto"
    # 是否打印 Qwen ASR 内部推理日志，便于排查编码、解码和热词后处理链路。
    verbose: bool = True


@dataclass(frozen=True)
class AsrDecodeProfileYamlConfig:
    """ASR 单类识别策略 YAML 配置。"""

    # 当前识别类型的分块时长。
    chunk_size_sec: float
    # 当前识别类型保留的历史 chunk 数量。
    memory_chunks: int
    # 仅 partial 使用的最近窗口长度；final 固定为 None。
    window_sec: float | None = None


@dataclass(frozen=True)
class AsrSessionYamlConfig:
    """ASR 会话业务参数 YAML 配置。"""

    # ASR 热词列表来自 YAML，Qwen ASR engine 会直接消费该列表。
    hotwords: tuple[str, ...] = field(default_factory=tuple)
    # partial profile 负责实时首字延迟与历史窗口策略。
    partial: AsrDecodeProfileYamlConfig = field(
        default_factory=lambda: AsrDecodeProfileYamlConfig(
            chunk_size_sec=8.0,
            memory_chunks=1,
            window_sec=12.0,
        )
    )
    # final profile 负责最终文本稳定性策略。
    final: AsrDecodeProfileYamlConfig = field(
        default_factory=lambda: AsrDecodeProfileYamlConfig(
            chunk_size_sec=12.0,
            memory_chunks=1,
            window_sec=None,
        )
    )


@dataclass(frozen=True)
class AsrYamlConfig:
    """ASR YAML 配置。"""

    # ASR 引擎运行参数。
    engine: AsrEngineYamlConfig = field(default_factory=AsrEngineYamlConfig)
    # ASR 会话业务参数。
    session: AsrSessionYamlConfig = field(default_factory=AsrSessionYamlConfig)


@dataclass(frozen=True)
class SpeakerVerificationYamlConfig:
    """说话人确认 YAML 配置。"""

    # sherpa-onnx 执行 provider，支持 cpu、cuda。
    provider: str = "cpu"
    # extractor 神经网络计算线程数。
    num_threads: int = 1
    # 单次或 final 检测分数达到该值时直接放行。
    high_similarity: float = 0.28
    # 非 final 单次检测分数低于该值时，连续两次才锁定拒绝。
    low_similarity: float = 0.20
    # 多次检测落在灰区时，final 阶段使用本轮均值放行阈值。
    mean_similarity: float = 0.28


@dataclass(frozen=True)
class WorkerYamlConfig:
    """实时音频 worker YAML 配置。"""

    # 触发 Smart Turn 判定前需要累计的静音时长。
    turn_silence_timeout_ms: float = 200.0
    # 两次 partial ASR 之间至少新增多少毫秒音频，内部会换算成 PCM16 字节数。
    partial_asr_min_new_audio_ms: float = 500.0
    # 两次 partial ASR 之间的最小触发间隔。
    partial_asr_min_interval_ms: float = 400.0
    # final ASR 和说话人确认的最短有效人声音频时长，内部会换算成 PCM16 字节数。
    min_final_asr_audio_ms: float = 600.0


@dataclass(frozen=True)
class SmartTurnYamlConfig:
    """Smart Turn YAML 配置。"""

    # ONNX Runtime provider 名称，例如 CPUExecutionProvider 或 CUDAExecutionProvider。
    provider: str = "CPUExecutionProvider"
    # Smart Turn 判定结束后，继续给用户一个很短的补说宽限窗口。
    final_grace_ms: float = 300.0
    # 低于该结束概率时进入“可能还没说完”的灰区等待。
    ambiguous_probability: float = 0.5
    # 持续等待到该时长后才给前端展示“继续说话中”的提示。
    hint_delay_ms: float = 2000.0
    # 灰区下等待到该静音时长后，允许按“说完了”收束。
    ambiguous_silence_timeout_ms: float = 1000.0
    # 无论模型概率如何，达到这个绝对静音时长都强制收束当前 turn。
    absolute_silence_timeout_ms: float = 4500.0


@dataclass(frozen=True)
class TextEouYamlConfig:
    """文本 EOU YAML 配置。"""

    # 是否启用文本 EOU 辅助 Smart Turn；关闭时 worker 完全回退原始 Smart Turn 时序。
    enabled: bool = True
    # FireRedChat turn detector 模型目录，默认落在 backend/ckpt 下，避免生产运行依赖 ref_code。
    model_dir: str = str(BACKEND_ROOT / "ckpt" / "turn_detector")
    # 使用中文或多语言模型文件。
    lang: Literal["chinese", "multilingual"] = "chinese"
    # Text EOU 固定使用 CPUExecutionProvider，避免 CUDA/DirectML 对 q8 模型产生明显数值差异。
    provider: str = "CPUExecutionProvider"
    # 文本 EOU 高置信结束阈值，达到后可覆盖 Smart Turn 的“未结束”判断。
    high_finish_probability: float = 0.95
    # 文本 EOU 极低置信未结束阈值，低于该值时延长低置信静音等待。
    unlikely_probability: float = 0.08
    # 文本 EOU 极低置信时，对 Smart Turn 绝对静音兜底的倍率。
    low_confidence_timeout_multiplier: float = 2.0


@dataclass(frozen=True)
class DebugAudioYamlConfig:
    """调试音频落盘配置。"""

    # 默认关闭，避免生产或日常开发时持续保存环境音。
    enabled: bool = False
    # 默认目录落在 backend/data 下，和 SQLite 记录同属本地开发数据，不进入代码资产目录。
    output_dir: str = str(USER_DATA_ROOT / "debug-audio")
    # 单个 session 最多保留的调试文件数量，防止长时间排障无限占用磁盘。
    max_session_files: int = 200


@dataclass(frozen=True)
class AecSidecarDebugAudioYamlConfig:
    """AEC sidecar 连续调试音频落盘配置。"""

    # 默认关闭，避免每次桌面端正式运行都持续保存整段连续音频。
    enabled: bool = False
    # sidecar 连续录音单独落到 backend/data/debug-audio/aec-sidecar，和 backend turn 级调试录音语义分离。
    output_dir: str = str(USER_DATA_ROOT / "debug-audio" / "aec-sidecar")
    # 最多保留最近 N 次 sidecar 启动目录，避免长期运行把连续录音无限累积到磁盘。
    max_runs: int = 10


@dataclass(frozen=True)
class LatencyYamlConfig:
    """实时延迟记录 YAML 配置。"""

    # 默认关闭，避免日常运行持续把每个 turn 的延迟 marker 落盘。
    enabled: bool = False
    # JSONL 落盘路径，相对仓库根目录解析，避免运行目录漂移导致落盘位置不确定。
    output_path: str = str(USER_DATA_ROOT / "latency-traces" / "realtime-latency.jsonl")


@dataclass(frozen=True)
class RealtimeAudioYamlConfig:
    """实时音频 YAML 聚合配置。"""

    # 输入链路验证模式默认开启，只运行 wake word、VAD、Smart Turn 和 ASR，不启动 LLM/TTS 输出侧。
    realtime_input_only_mode: bool
    # 唤醒执行配置是当前真实语音入口必需配置；唤醒词文本由当前激活角色卡管理。
    wakeword: WakewordYamlConfig
    # 唤醒回复播放节奏配置，只影响唤醒回复音频，不影响 wake.detected 文本事件。
    wake_response: WakeResponseYamlConfig = field(default_factory=WakeResponseYamlConfig)
    # VAD 配置控制 Silero ONNX 执行设备。
    vad: VadYamlConfig = field(default_factory=VadYamlConfig)
    # ASR 配置集中读取，统一维护 engine/session 分层结构。
    asr: AsrYamlConfig = field(default_factory=AsrYamlConfig)
    # 说话人确认配置集中维护 provider 和阈值，避免散落在 Settings。
    speaker_verification: SpeakerVerificationYamlConfig = field(default_factory=SpeakerVerificationYamlConfig)
    # worker 运行阈值统一从 YAML 注入，代码内部只做一次字节换算。
    worker: WorkerYamlConfig = field(default_factory=WorkerYamlConfig)
    # Smart Turn 配置控制轮次结束判断模型执行设备。
    smart_turn: SmartTurnYamlConfig = field(default_factory=SmartTurnYamlConfig)
    # 文本 EOU 配置控制基于 final ASR 文本的辅助轮次结束判断。
    text_eou: TextEouYamlConfig = field(default_factory=TextEouYamlConfig)
    # 调试音频配置默认关闭，显式启用后才保存本地 WAV 和 manifest。
    debug_audio: DebugAudioYamlConfig = field(default_factory=DebugAudioYamlConfig)
    # AEC sidecar 连续调试录音默认关闭，显式启用后才由 desktop main 把配置下发给 sidecar。
    aec_sidecar_debug_audio: AecSidecarDebugAudioYamlConfig = field(default_factory=AecSidecarDebugAudioYamlConfig)
    # 实时延迟记录默认关闭，显式启用后才创建记录器并落盘 JSONL marker。
    latency: LatencyYamlConfig = field(default_factory=LatencyYamlConfig)


def apply_execution_configuration(
    config: RealtimeAudioYamlConfig,
    execution_config_path: Path | None = None,
) -> RealtimeAudioYamlConfig:
    """把用户确认的执行方式应用到实时输入配置。

    Args:
        config: 已读取并完成字段校验的实时音频配置。
        execution_config_path: 用户确认的执行配置文件路径。

    Returns:
        根据用户确认的 ONNX 与 llama.cpp 后端调整后的配置。
    """

    from companion.core.execution_config import read_execution_configuration

    saved = read_execution_configuration(execution_config_path or USER_DATA_ROOT / "execution-config.json")
    if saved is None and os.environ.get("NEARMATE_PACKAGED") != "1":
        return config
    # 首次引导还没有执行配置时，控制面可以启动，但实时输入运行时不会被创建。
    # 这里保留 CPU 占位配置，避免配置页面为了展示状态而触发底层 GPU 加载。
    onnx_provider = saved.selection.ort_provider if saved else "CPUExecutionProvider"
    llama_backend = saved.selection.llama_backend if saved else "auto"
    if saved is not None and (
        os.environ.get("NEARMATE_PACKAGED") == "1"
        or os.environ.get("NEARMATE_RUNTIME_PROFILE")
    ):
        # 用户数据会跨安装包升级保留；旧 CUDA 包留下的选择不能覆盖当前 Universal 包的 Vulkan/DirectML 组合。
        normalized_onnx_provider, llama_backend = normalize_execution_selection(
            saved.selection.onnx_provider,
            saved.selection.llama_backend,
        )
        onnx_provider = {
            "cuda": "CUDAExecutionProvider",
            "directml": "DmlExecutionProvider",
            "cpu": "CPUExecutionProvider",
        }[normalized_onnx_provider]
    onnx_policy = "dml" if onnx_provider == "DmlExecutionProvider" else "cpu"
    return replace(
        config,
        wakeword=replace(config.wakeword, provider="cpu"),
        vad=replace(config.vad, provider=onnx_provider),
        asr=replace(config.asr, engine=replace(config.asr.engine, onnx_provider=onnx_policy, llm_backend=llama_backend)),
        speaker_verification=replace(config.speaker_verification, provider="cpu"),
        smart_turn=replace(config.smart_turn, provider=onnx_provider),
        text_eou=replace(config.text_eou, provider=onnx_provider),
    )


def load_realtime_audio_config(
    path: Path | str = DEFAULT_REALTIME_AUDIO_CONFIG_PATH,
    *,
    execution_config_path: Path | None = None,
) -> RealtimeAudioYamlConfig:
    """读取实时音频 YAML 配置。"""

    # YAML 文件是用户编辑配置的事实源，缺失时直接失败可以尽早暴露部署配置不完整。
    config_path = Path(path)
    try:
        raw_config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise FileNotFoundError(f"实时音频配置文件缺失：{config_path}") from exc

    # 顶层必须是映射，避免列表或字符串被误解释为有效配置。
    if not isinstance(raw_config, dict):
        raise ValueError("实时音频 YAML 顶层必须是对象")

    realtime_input_only_mode = _read_optional_bool(
        raw_config.get("realtime_input_only_mode", True),
        field_name="realtime_input_only_mode",
    )

    wakeword_section = raw_config.get("wakeword")
    if not isinstance(wakeword_section, dict):
        raise ValueError("实时音频 YAML 必须包含 wakeword 对象")

    # wakeword provider 直接传给 sherpa-onnx，必须限制到当前版本声明支持的值。
    wakeword_provider = _read_optional_string(
        wakeword_section.get("provider", "cpu"),
        field_name="wakeword.provider",
    ).lower()
    if wakeword_provider not in {"cpu", "cuda", "coreml"}:
        raise ValueError("wakeword.provider 只能是 cpu、cuda 或 coreml")

    # device 和 num_threads 都会进入 sherpa-onnx 构造函数，提前校验为非负/正整数可以避免底层错误难以定位。
    wakeword_device = _read_optional_int(wakeword_section.get("device", 0), field_name="wakeword.device")
    if wakeword_device < 0:
        raise ValueError("wakeword.device 必须是非负整数")
    wakeword_num_threads = _read_optional_int(
        wakeword_section.get("num_threads", 2),
        field_name="wakeword.num_threads",
    )
    if wakeword_num_threads <= 0:
        raise ValueError("wakeword.num_threads 必须是正整数")

    wake_response_section = raw_config.get("wake_response", {})
    if not isinstance(wake_response_section, dict):
        raise ValueError("实时音频 YAML 中 wake_response 必须是对象")
    wake_response_playback_delay_ms = _read_non_negative_float(
        wake_response_section.get(
            "playback_delay_ms",
            WakeResponseYamlConfig.playback_delay_ms,
        ),
        field_name="wake_response.playback_delay_ms",
    )

    vad_section = raw_config.get("vad", {})
    if not isinstance(vad_section, dict):
        raise ValueError("实时音频 YAML 中 vad 必须是对象")
    vad_provider = _read_optional_string(
        vad_section.get("provider", "CPUExecutionProvider"),
        field_name="vad.provider",
    )
    vad_speech_start_window_count = _read_optional_int(
        vad_section.get("speech_start_window_count", VadYamlConfig.speech_start_window_count),
        field_name="vad.speech_start_window_count",
    )
    if vad_speech_start_window_count <= 0:
        raise ValueError("vad.speech_start_window_count 必须是正整数")

    asr_section = raw_config.get("asr", {})
    if not isinstance(asr_section, dict):
        raise ValueError("实时音频 YAML 中 asr 必须是对象")

    # 现在强制要求使用 engine/session 分层结构，避免继续扩散历史平铺字段。
    engine_section = asr_section.get("engine")
    if not isinstance(engine_section, dict):
        raise ValueError("实时音频 YAML 中 asr.engine 必须是对象")
    session_section = asr_section.get("session")
    if not isinstance(session_section, dict):
        raise ValueError("实时音频 YAML 中 asr.session 必须是对象")

    asr_onnx_provider = _read_optional_asr_onnx_provider(
        engine_section.get("onnx_provider", "auto"),
        field_name="asr.engine.onnx_provider",
    )
    asr_llm_backend = _read_optional_llm_backend(
        engine_section.get("llm_backend", "auto"),
        field_name="asr.engine.llm_backend",
    )
    asr_verbose = _read_optional_bool(
        engine_section.get("verbose", True),
        field_name="asr.engine.verbose",
    )

    hotwords = _read_string_list(session_section.get("hotwords", []), field_name="asr.session.hotwords")
    partial_section = session_section.get("partial")
    if not isinstance(partial_section, dict):
        raise ValueError("实时音频 YAML 中 asr.session.partial 必须是对象")
    final_section = session_section.get("final")
    if not isinstance(final_section, dict):
        raise ValueError("实时音频 YAML 中 asr.session.final 必须是对象")

    partial_profile = _read_asr_decode_profile_config(
        partial_section,
        field_name="asr.session.partial",
        default_window_sec=12.0,
    )
    final_profile = _read_asr_decode_profile_config(
        final_section,
        field_name="asr.session.final",
        default_window_sec=None,
    )

    speaker_verification_section = raw_config.get("speaker_verification", {})
    if not isinstance(speaker_verification_section, dict):
        raise ValueError("实时音频 YAML 中 speaker_verification 必须是对象")
    if "threshold" in speaker_verification_section:
        raise ValueError(
            "speaker_verification.threshold 已移除，请改用 high_similarity、low_similarity 和 mean_similarity"
        )
    speaker_verification_provider = _read_optional_string(
        speaker_verification_section.get("provider", SpeakerVerificationYamlConfig.provider),
        field_name="speaker_verification.provider",
    ).lower()
    speaker_verification_num_threads = _read_optional_int(
        speaker_verification_section.get("num_threads", SpeakerVerificationYamlConfig.num_threads),
        field_name="speaker_verification.num_threads",
    )
    if speaker_verification_num_threads <= 0:
        raise ValueError("speaker_verification.num_threads 必须是正整数")
    speaker_verification_high_similarity = _read_probability(
        speaker_verification_section.get(
            "high_similarity",
            SpeakerVerificationYamlConfig.high_similarity,
        ),
        field_name="speaker_verification.high_similarity",
    )
    speaker_verification_low_similarity = _read_probability(
        speaker_verification_section.get(
            "low_similarity",
            SpeakerVerificationYamlConfig.low_similarity,
        ),
        field_name="speaker_verification.low_similarity",
    )
    speaker_verification_mean_similarity = _read_probability(
        speaker_verification_section.get(
            "mean_similarity",
            SpeakerVerificationYamlConfig.mean_similarity,
        ),
        field_name="speaker_verification.mean_similarity",
    )

    worker_section = raw_config.get("worker", {})
    if not isinstance(worker_section, dict):
        raise ValueError("实时音频 YAML 中 worker 必须是对象")
    turn_silence_timeout_ms = _read_non_negative_float(
        worker_section.get("turn_silence_timeout_ms", WorkerYamlConfig.turn_silence_timeout_ms),
        field_name="worker.turn_silence_timeout_ms",
    )
    partial_asr_min_new_audio_ms = _read_non_negative_float(
        worker_section.get("partial_asr_min_new_audio_ms", WorkerYamlConfig.partial_asr_min_new_audio_ms),
        field_name="worker.partial_asr_min_new_audio_ms",
    )
    partial_asr_min_interval_ms = _read_non_negative_float(
        worker_section.get("partial_asr_min_interval_ms", WorkerYamlConfig.partial_asr_min_interval_ms),
        field_name="worker.partial_asr_min_interval_ms",
    )
    min_final_asr_audio_ms = _read_non_negative_float(
        worker_section.get("min_final_asr_audio_ms", WorkerYamlConfig.min_final_asr_audio_ms),
        field_name="worker.min_final_asr_audio_ms",
    )

    smart_turn_section = raw_config.get("smart_turn", {})
    if not isinstance(smart_turn_section, dict):
        raise ValueError("实时音频 YAML 中 smart_turn 必须是对象")
    smart_turn_provider = _read_optional_string(
        smart_turn_section.get("provider", "CPUExecutionProvider"),
        field_name="smart_turn.provider",
    )
    smart_turn_final_grace_ms = _read_non_negative_float(
        smart_turn_section.get("final_grace_ms", SmartTurnYamlConfig.final_grace_ms),
        field_name="smart_turn.final_grace_ms",
    )
    smart_turn_ambiguous_probability = _read_probability(
        smart_turn_section.get("ambiguous_probability", SmartTurnYamlConfig.ambiguous_probability),
        field_name="smart_turn.ambiguous_probability",
    )
    smart_turn_hint_delay_ms = _read_non_negative_float(
        smart_turn_section.get("hint_delay_ms", SmartTurnYamlConfig.hint_delay_ms),
        field_name="smart_turn.hint_delay_ms",
    )
    smart_turn_ambiguous_silence_timeout_ms = _read_non_negative_float(
        smart_turn_section.get(
            "ambiguous_silence_timeout_ms",
            SmartTurnYamlConfig.ambiguous_silence_timeout_ms,
        ),
        field_name="smart_turn.ambiguous_silence_timeout_ms",
    )
    smart_turn_absolute_silence_timeout_ms = _read_non_negative_float(
        smart_turn_section.get(
            "absolute_silence_timeout_ms",
            SmartTurnYamlConfig.absolute_silence_timeout_ms,
        ),
        field_name="smart_turn.absolute_silence_timeout_ms",
    )

    text_eou_section = raw_config.get("text_eou", {})
    if not isinstance(text_eou_section, dict):
        raise ValueError("实时音频 YAML 中 text_eou 必须是对象")
    text_eou_enabled = _read_optional_bool(
        text_eou_section.get("enabled", TextEouYamlConfig.enabled),
        field_name="text_eou.enabled",
    )
    text_eou_model_dir = _read_optional_string(
        text_eou_section.get("model_dir", TextEouYamlConfig.model_dir),
        field_name="text_eou.model_dir",
    )
    text_eou_model_dir = _resolve_backend_relative_path(text_eou_model_dir)
    text_eou_lang = _read_optional_string(
        text_eou_section.get("lang", TextEouYamlConfig.lang),
        field_name="text_eou.lang",
    )
    if text_eou_lang not in {"chinese", "multilingual"}:
        raise ValueError("text_eou.lang 只能是 chinese 或 multilingual")
    text_eou_provider = _read_optional_string(
        text_eou_section.get("provider", TextEouYamlConfig.provider),
        field_name="text_eou.provider",
    )
    # 安装包 profile 是 GPU 组件的唯一事实源；源码开发没有显式 profile 时，
    # 必须尊重 YAML 中的 auto/CPU 安全值，避免把开发机强制拉到 CUDA。
    if os.environ.get("NEARMATE_PACKAGED") == "1" or os.environ.get("NEARMATE_RUNTIME_PROFILE"):
        profile_defaults = runtime_profile_defaults()
        asr_onnx_provider = profile_defaults.asr_onnx_provider
        asr_llm_backend = profile_defaults.llama_backend
        text_eou_provider = profile_defaults.text_eou_provider
    text_eou_high_finish_probability = _read_probability(
        text_eou_section.get(
            "high_finish_probability",
            TextEouYamlConfig.high_finish_probability,
        ),
        field_name="text_eou.high_finish_probability",
    )
    text_eou_unlikely_probability = _read_probability(
        text_eou_section.get(
            "unlikely_probability",
            TextEouYamlConfig.unlikely_probability,
        ),
        field_name="text_eou.unlikely_probability",
    )
    text_eou_low_confidence_timeout_multiplier = _read_non_negative_float(
        text_eou_section.get(
            "low_confidence_timeout_multiplier",
            TextEouYamlConfig.low_confidence_timeout_multiplier,
        ),
        field_name="text_eou.low_confidence_timeout_multiplier",
    )

    debug_audio_section = raw_config.get("debug_audio", {})
    if not isinstance(debug_audio_section, dict):
        raise ValueError("实时音频 YAML 中 debug_audio 必须是对象")

    debug_audio_enabled = _read_optional_bool(
        debug_audio_section.get("enabled", False),
        field_name="debug_audio.enabled",
    )
    debug_audio_output_dir = _read_optional_string(
        debug_audio_section.get("output_dir", DebugAudioYamlConfig.output_dir),
        field_name="debug_audio.output_dir",
    )
    debug_audio_output_dir = _resolve_user_data_relative_path(debug_audio_output_dir)
    debug_audio_max_session_files = _read_optional_int(
        debug_audio_section.get("max_session_files", 200),
        field_name="debug_audio.max_session_files",
    )
    if debug_audio_max_session_files <= 0:
        raise ValueError("debug_audio.max_session_files 必须是正整数")

    aec_sidecar_debug_audio_section = raw_config.get("aec_sidecar_debug_audio", {})
    if not isinstance(aec_sidecar_debug_audio_section, dict):
        raise ValueError("实时音频 YAML 中 aec_sidecar_debug_audio 必须是对象")

    aec_sidecar_debug_audio_enabled = _read_optional_bool(
        aec_sidecar_debug_audio_section.get("enabled", False),
        field_name="aec_sidecar_debug_audio.enabled",
    )
    aec_sidecar_debug_audio_output_dir = _read_optional_string(
        aec_sidecar_debug_audio_section.get("output_dir", AecSidecarDebugAudioYamlConfig.output_dir),
        field_name="aec_sidecar_debug_audio.output_dir",
    )
    aec_sidecar_debug_audio_output_dir = _resolve_user_data_relative_path(aec_sidecar_debug_audio_output_dir)
    aec_sidecar_debug_audio_max_runs = _read_optional_int(
        aec_sidecar_debug_audio_section.get("max_runs", AecSidecarDebugAudioYamlConfig.max_runs),
        field_name="aec_sidecar_debug_audio.max_runs",
    )
    if aec_sidecar_debug_audio_max_runs <= 0:
        raise ValueError("aec_sidecar_debug_audio.max_runs 必须是正整数")

    latency_section = raw_config.get("latency", {})
    if not isinstance(latency_section, dict):
        raise ValueError("实时音频 YAML 中 latency 必须是对象")
    latency_enabled = _read_optional_bool(
        latency_section.get("enabled", LatencyYamlConfig.enabled),
        field_name="latency.enabled",
    )
    latency_output_path = _read_optional_string(
        latency_section.get("output_path", LatencyYamlConfig.output_path),
        field_name="latency.output_path",
    )
    latency_output_path = _resolve_user_data_relative_path(latency_output_path)

    return apply_execution_configuration(RealtimeAudioYamlConfig(
        realtime_input_only_mode=realtime_input_only_mode,
        wakeword=WakewordYamlConfig(
            provider=wakeword_provider,
            device=wakeword_device,
            num_threads=wakeword_num_threads,
        ),
        wake_response=WakeResponseYamlConfig(playback_delay_ms=wake_response_playback_delay_ms),
        vad=VadYamlConfig(
            provider=vad_provider,
            speech_start_window_count=vad_speech_start_window_count,
        ),
        asr=AsrYamlConfig(
            engine=AsrEngineYamlConfig(
                onnx_provider=asr_onnx_provider,
                llm_backend=asr_llm_backend,
                verbose=asr_verbose,
            ),
            session=AsrSessionYamlConfig(
                hotwords=tuple(hotwords),
                partial=partial_profile,
                final=final_profile,
            ),
        ),
        speaker_verification=SpeakerVerificationYamlConfig(
            provider=speaker_verification_provider,
            num_threads=speaker_verification_num_threads,
            high_similarity=speaker_verification_high_similarity,
            low_similarity=speaker_verification_low_similarity,
            mean_similarity=speaker_verification_mean_similarity,
        ),
        worker=WorkerYamlConfig(
            turn_silence_timeout_ms=turn_silence_timeout_ms,
            partial_asr_min_new_audio_ms=partial_asr_min_new_audio_ms,
            partial_asr_min_interval_ms=partial_asr_min_interval_ms,
            min_final_asr_audio_ms=min_final_asr_audio_ms,
        ),
        smart_turn=SmartTurnYamlConfig(
            provider=smart_turn_provider,
            final_grace_ms=smart_turn_final_grace_ms,
            ambiguous_probability=smart_turn_ambiguous_probability,
            hint_delay_ms=smart_turn_hint_delay_ms,
            ambiguous_silence_timeout_ms=smart_turn_ambiguous_silence_timeout_ms,
            absolute_silence_timeout_ms=smart_turn_absolute_silence_timeout_ms,
        ),
        text_eou=TextEouYamlConfig(
            enabled=text_eou_enabled,
            model_dir=text_eou_model_dir,
            lang=text_eou_lang,  # type: ignore[arg-type]
            provider=text_eou_provider,
            high_finish_probability=text_eou_high_finish_probability,
            unlikely_probability=text_eou_unlikely_probability,
            low_confidence_timeout_multiplier=text_eou_low_confidence_timeout_multiplier,
        ),
        debug_audio=DebugAudioYamlConfig(
            enabled=debug_audio_enabled,
            output_dir=debug_audio_output_dir,
            max_session_files=debug_audio_max_session_files,
        ),
        aec_sidecar_debug_audio=AecSidecarDebugAudioYamlConfig(
            enabled=aec_sidecar_debug_audio_enabled,
            output_dir=aec_sidecar_debug_audio_output_dir,
            max_runs=aec_sidecar_debug_audio_max_runs,
        ),
        latency=LatencyYamlConfig(
            enabled=latency_enabled,
            output_path=latency_output_path,
        ),
    ), execution_config_path)


def _read_asr_decode_profile_config(
    section: dict[str, Any],
    *,
    field_name: str,
    default_window_sec: float | None,
) -> AsrDecodeProfileYamlConfig:
    """读取 ASR decode profile 子配置。"""

    # 识别策略必须显式写出分块与历史窗口，避免 partial/final 语义再次分散到各处。
    chunk_size_sec = _read_optional_float(
        section.get("chunk_size_sec", 8.0 if default_window_sec is not None else 12.0),
        field_name=f"{field_name}.chunk_size_sec",
    )
    if chunk_size_sec <= 0:
        raise ValueError(f"{field_name}.chunk_size_sec 必须是正数")

    memory_chunks = _read_optional_int(
        section.get("memory_chunks", 1),
        field_name=f"{field_name}.memory_chunks",
    )
    if memory_chunks < 0:
        raise ValueError(f"{field_name}.memory_chunks 必须是非负整数")

    if default_window_sec is None:
        return AsrDecodeProfileYamlConfig(
            chunk_size_sec=chunk_size_sec,
            memory_chunks=memory_chunks,
            window_sec=None,
        )

    window_sec = _read_optional_float(
        section.get("window_sec", default_window_sec),
        field_name=f"{field_name}.window_sec",
    )
    if window_sec <= 0:
        raise ValueError(f"{field_name}.window_sec 必须是正数")
    return AsrDecodeProfileYamlConfig(
        chunk_size_sec=chunk_size_sec,
        memory_chunks=memory_chunks,
        window_sec=window_sec,
    )


def _read_string_list(value: Any, *, field_name: str) -> list[str]:
    """读取 YAML 字符串列表字段。"""

    # 配置字段必须显式写成 YAML list，避免逗号字符串造成维护者误判实际生效内容。
    if not isinstance(value, list):
        raise ValueError(f"{field_name} 必须是字符串列表")

    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        # 每个配置项都必须是字符串，防止数字或对象被隐式转成难以察觉的热词。
        if not isinstance(item, str):
            raise ValueError(f"{field_name} 只能包含字符串")

        keyword = item.strip()
        if not keyword or keyword in seen:
            continue
        seen.add(keyword)
        result.append(keyword)

    return result


def _read_optional_string(value: Any, *, field_name: str) -> str:
    """读取 YAML 中的单个字符串字段。"""

    # 单值配置常常会被误写成数字或空值，提前拒绝可以让配置错误停在可读边界。
    if not isinstance(value, str):
        raise ValueError(f"{field_name} 必须是字符串")

    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} 不能为空")
    return normalized


def _read_optional_int(value: Any, *, field_name: str) -> int:
    """读取 YAML 中的整数配置字段。"""

    # bool 是 int 的子类，但在配置语义里 True/False 不是合法设备编号或线程数。
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field_name} 必须是整数")
    return value


def _read_optional_bool(value: Any, *, field_name: str) -> bool:
    """读取 YAML 中的布尔配置字段。"""

    # YAML bool 应写成 true/false；字符串形式容易误导运行时设备选择，必须拒绝。
    if not isinstance(value, bool):
        raise ValueError(f"{field_name} 必须是布尔值")
    return value


def _read_optional_llm_backend(
    value: Any,
    *,
    field_name: str,
) -> Literal["vulkan", "cuda", "auto"]:
    """读取 YAML 中的 LLM backend 配置字段。"""

    normalized = _read_optional_string(value, field_name=field_name)
    if normalized not in {"vulkan", "cuda", "auto"}:
        raise ValueError(f"{field_name} 只能是 vulkan、cuda 或 auto")
    return normalized


def _read_optional_asr_onnx_provider(value: Any, *, field_name: str) -> str:
    """读取 ASR ONNX provider 策略字段。"""

    normalized = _read_optional_string(value, field_name=field_name).lower()
    if normalized not in {"auto", "cpu", "cuda", "dml"}:
        raise ValueError(f"{field_name} 只能是 auto、cpu、cuda 或 dml")
    return normalized


def _read_optional_float(value: Any, *, field_name: str) -> float:
    """读取 YAML 中的浮点配置字段。"""

    # chunk/window 长度允许整数或浮点数，但不能接受布尔值或字符串。
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} 必须是数字")
    return float(value)


def _read_non_negative_float(value: Any, *, field_name: str) -> float:
    """读取 YAML 中的非负浮点配置字段。"""

    normalized = _read_optional_float(value, field_name=field_name)
    if normalized < 0:
        raise ValueError(f"{field_name} 必须是非负数字")
    return normalized


def _read_probability(value: Any, *, field_name: str) -> float:
    """读取 YAML 中的概率配置字段。"""

    normalized = _read_optional_float(value, field_name=field_name)
    if normalized < 0 or normalized > 1:
        raise ValueError(f"{field_name} 必须在 0 到 1 之间")
    return normalized


def _resolve_backend_relative_path(value: str) -> str:
    """把只读 Backend 资产路径归一到 manifest 声明的 Backend 根目录。"""

    path = Path(value)
    if path.is_absolute():
        return str(path)
    return str(BACKEND_ROOT / path)


def _resolve_user_data_relative_path(value: str) -> str:
    """把调试与延迟输出路径归一到可写用户数据根目录。"""

    path = Path(value)
    if path.is_absolute():
        return str(path)
    return str(USER_DATA_ROOT / path)
