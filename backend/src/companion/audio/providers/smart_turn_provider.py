from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import numpy as np

from companion.audio.bootstrap.asset_registry import resolve_local_audio_asset_paths
from companion.audio.bootstrap.smart_turn_runtime import (
    SMART_TURN_MODEL_NAME,
    SMART_TURN_SAMPLE_RATE,
    SMART_TURN_WINDOW_SECONDS,
    SmartTurnRuntimeConfig,
    create_feature_extractor,
    create_onnx_session,
    prepare_smart_turn_audio_window,
    prepare_smart_turn_pcm16,
)
from companion.audio.providers.base import SmartTurnDecision
from companion.observability.logging_config import get_backend_logger

logger = get_backend_logger(__name__)


@dataclass(frozen=True)
class SmartTurnProviderConfig:
    """真实 Smart Turn provider 的轻量配置。"""

    # Smart Turn 官方要求 16kHz 单声道 PCM，默认值与 bootstrap runtime 保持一致。
    sample_rate: int = SMART_TURN_SAMPLE_RATE
    # 模型最多支持 8 秒输入；超过时只保留当前 turn 的最后窗口。
    max_seconds: int = SMART_TURN_WINDOW_SECONDS
    # 结束概率达到该阈值时，provider 判断当前用户 turn 已说完。
    finish_threshold: float = 0.7
    # 真实 ONNX 推理所需配置；测试可通过注入 predictor 绕过真实模型加载。
    runtime_config: SmartTurnRuntimeConfig | None = None


def prepare_smart_turn_input(audio: np.ndarray, *, sample_rate: int, max_seconds: int) -> np.ndarray:
    """准备 Smart Turn 模型的一维 float32 固定窗口输入。"""

    # provider 层暴露稳定 API，但窗口实现只保留一份，避免与 runtime buffer 产生语义漂移。
    return prepare_smart_turn_audio_window(
        audio,
        sample_rate=sample_rate,
        window_seconds=max_seconds,
    )


def extract_smart_turn_probability(outputs: list[np.ndarray]) -> float:
    """从 Smart Turn ONNX 输出中提取已完成概率。"""

    # 真实参考实现使用 outputs[0][0].item()；模型输出已经是 sigmoid 概率，不能再次做 sigmoid 或 softmax。
    if not outputs:
        raise ValueError("Smart Turn ONNX 输出为空，无法提取完成概率")

    # 转为 numpy 数组后统一处理 list、ndarray 或 runtime 返回对象，保证概率提取路径稳定。
    probability_array = np.asarray(outputs[0], dtype=np.float32)
    if probability_array.size == 0:
        raise ValueError("Smart Turn ONNX 首个输出为空，无法提取完成概率")

    # 扁平化后取第一个概率，匹配参考 inference.py 的 outputs[0][0].item() 行为。
    return float(probability_array.reshape(-1)[0])


class RealSmartTurnProvider:
    """基于本地 ONNX Smart Turn 模型的真实 provider。 """

    def __init__(
        self,
        *,
        config: SmartTurnProviderConfig,
        predict_turn_finished_probability: Callable[[np.ndarray], float] | None = None,
    ) -> None:

        # 采样率必须为正数，否则 PCM 字节转换后的固定窗口无法定义。
        if config.sample_rate <= 0:
            raise ValueError("sample_rate 必须为正数，才能运行 Smart Turn provider")

        # 最大窗口秒数必须为正数，否则前置补零与尾部截断都会失去业务含义。
        if config.max_seconds <= 0:
            raise ValueError("max_seconds 必须为正数，才能运行 Smart Turn provider")

        # 配置保存在实例上，后续每次判断都用同一阈值和采样约定，保持 turn 判断稳定。
        self.config = config
        self._predict_turn_finished_probability = predict_turn_finished_probability
        # runtime_config 是真实模型路径和输入约定的完整快照，后续推理阶段不再重新派生。
        self._runtime_config = self._get_runtime_config()

        # 初始化模型
        self._onnx_session = None
        self._feature_extractor = None
        if self._predict_turn_finished_probability is None:
            self._onnx_session = create_onnx_session(self._runtime_config)
            self._feature_extractor = create_feature_extractor(self._runtime_config)

    def decide_turn_end(self, pcm16: bytes) -> SmartTurnDecision:
        """判断当前用户 turn 是否已经说完。"""

        # 先把当前 turn 的 PCM16 转成模型固定窗口，严格遵守“只包含当前 turn，最长取最后 8 秒”的语义。
        prepared_audio = prepare_smart_turn_pcm16(
            pcm16,
            sample_rate=self.config.sample_rate,
            window_seconds=self.config.max_seconds,
        )

        # 概率预测函数是唯一可替换边界：测试使用 fake，生产默认走本地 ONNX runtime。
        probability = float(self._predict_probability(prepared_audio))

        # 只把“是否结束”和概率向上暴露，运行时不需要知道 ONNX logits 或特征张量细节。
        return SmartTurnDecision(
            is_turn_finished=probability >= self.config.finish_threshold,
            probability=probability,
        )

    def _predict_probability(self, prepared_audio: np.ndarray) -> float:
        """计算当前 turn 已结束的概率。 """

        # 测试或替代实现优先，避免单元测试加载真实模型与重型推理依赖。
        if self._predict_turn_finished_probability is not None:
            return self._predict_turn_finished_probability(prepared_audio)

        # 构造期已经完成真实模型依赖加载；若这里缺失，说明初始化路径被破坏。
        if self._onnx_session is None or self._feature_extractor is None:
            raise RuntimeError("Smart Turn provider 未初始化真实模型依赖，无法执行推理")

        # 构建 Whisper 特征并执行推理；输出解释集中在这里，避免泄露到底层 runtime session。
        input_features = self._build_input_features(prepared_audio)
        outputs = self._onnx_session.run(None, {"input_features": input_features})
        return extract_smart_turn_probability(outputs)

    def _get_runtime_config(self) -> SmartTurnRuntimeConfig:
        """获取当前 provider 使用的 Smart Turn 运行时配置。"""

        # 高级调用方显式传入 runtime_config 时完整尊重，避免 provider 覆盖模型路径或 provider 类型。
        if self.config.runtime_config is not None:
            return self.config.runtime_config

        # 默认路径来自资产注册器，保持真实 Smart Turn 模型位于仓库内 ckpt 目录。
        asset_paths = resolve_local_audio_asset_paths()
        return SmartTurnRuntimeConfig(
            model_path=asset_paths.ckpt_dir / SMART_TURN_MODEL_NAME,
            sample_rate=self.config.sample_rate,
            window_seconds=self.config.max_seconds,
        )

    def _build_input_features(self, prepared_audio: np.ndarray) -> np.ndarray:
        """使用构造期创建的 Whisper 特征提取器构建 ONNX 输入。"""

        # 特征提取器在构造期创建，推理阶段只复用对象，避免首次静音边界才加载 transformers 依赖。
        inputs = self._feature_extractor(
            prepared_audio,
            sampling_rate=self._runtime_config.sample_rate,
            return_tensors="np",
            padding="max_length",
            max_length=self._runtime_config.window_seconds * self._runtime_config.sample_rate,
            truncation=True,
            do_normalize=True,
        )

        # ONNX 模型期望 batch 维度，参考实现中 squeeze 后再 expand_dims。
        input_features = inputs.input_features.squeeze(0).astype(np.float32)
        return np.expand_dims(input_features, axis=0)
