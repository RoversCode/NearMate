from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import numpy as np

from companion.audio.bootstrap.qwen_asr_config import QwenAsrConfig, build_qwen_asr_config_from_model_dir
from companion.audio.bootstrap.qwen_asr_engine_adapter import create_qwen_asr_engine
from companion.audio.bootstrap.qwen_asr_worker import RealtimeAsrWorker
from companion.audio.providers.base import PartialTranscript


@dataclass(frozen=True)
class AsrProviderConfig:
    """真实 ASR provider 的轻量配置。"""

    # 默认模型目录保持为用户可断言的仓库相对路径；真实 runtime 路径仍由 bootstrap 统一解析。
    model_dir: Path | str = Path("backend/ckpt/qwen_asr")
    # 可选 Qwen ASR runtime 配置用于高级调用方显式覆盖，生产装配会用它传入热词等完整参数。
    runtime_config: QwenAsrConfig | None = None


class RealAsrProvider:
    """基于本地 Qwen ASR worker 的真实 ASR provider。"""

    def __init__(
        self,
        *,
        config: AsrProviderConfig | None = None,
    ) -> None:
        """初始化真实 ASR provider 并创建底层 Qwen ASR 引擎。"""

        # 配置先归一化，后续 engine 与 worker 必须共享同一份 runtime_config，避免热词、chunk 参数不一致。
        self.config = config or AsrProviderConfig()
        # runtime_config 是真实 ASR 链路的完整配置快照，作用域限定在当前 provider 生命周期内。
        self._runtime_config = self._get_runtime_config()
        # engine 是本地 Qwen ASR 重型推理对象，构造期创建可以让启动初始化明确失败，不把错误延迟到实时音频线程。
        self._engine = create_qwen_asr_engine(self._runtime_config)
        # worker 是 provider 唯一调用的执行边界，负责串行化真实 ASR 推理并整理性能指标。
        self._worker = RealtimeAsrWorker(self._runtime_config, self._engine)

    async def transcribe_partial(self, pcm16: bytes) -> PartialTranscript:
        """生成实时中间转写结果。"""

        # partial 只暴露结构化文本，底层性能指标留在 worker 返回值中，不泄露给上层产品事件。
        result = await self._run_worker(_pcm16_to_float32_waveform(pcm16), kind="partial")
        return PartialTranscript(text=str(result.get("text", "")))   # NOTE: 只拿了text

    async def transcribe_final(self, pcm16: bytes) -> str:
        """生成当前用户轮次的最终转写文本。"""

        # final 保持简单字符串契约，便于实时编排层直接拼入后续 LLM 输入。
        result = await self._run_worker(_pcm16_to_float32_waveform(pcm16), kind="final")
        return str(result.get("text", ""))

    def shutdown(self) -> None:
        """关闭真实 ASR provider 持有的底层 worker 和 engine。"""

        # provider 不直接知道 QwenASREngine 的内部资源形态，释放动作保持在 worker 统一边界。
        self._worker.shutdown()

    async def _run_worker(self, waveform: np.ndarray, *, kind: Literal["partial", "final"]) -> dict[str, object]:
        """调用底层 ASR worker 并归一化返回结构。"""

        # 真实 worker 内部使用 asyncio.to_thread 执行重型推理，调用方必须 await，避免阻塞 LiveKit 事件循环。
        result = await self._worker.run(waveform, kind)
        return _coerce_asr_result(result)

    def _get_runtime_config(self) -> QwenAsrConfig:
        """获取 ASR worker 使用的运行时配置。"""

        # 高级调用方显式传入 runtime_config 时完整尊重，避免 provider 覆盖更细粒度的热词或线程配置。
        if self.config.runtime_config is not None:
            return self.config.runtime_config

        # 普通调用方只传 model_dir 时，由 provider 派生模型路径；热词由上层 YAML 配置显式注入 runtime_config。
        return build_qwen_asr_config_from_model_dir(self.config.model_dir)


def _pcm16_to_float32_waveform(pcm16: bytes) -> np.ndarray:
    """把 PCM16 little-endian 字节转换为一维 float32 waveform。"""

    # PCM16 每个采样点固定占 2 字节，奇数字节说明上游音频帧被截断或拼接错误。
    if len(pcm16) % 2 != 0:
        raise ValueError("PCM16 字节长度必须按 int16 对齐，当前收到奇数字节长度")

    # 明确使用小端 int16，避免 Windows/Linux 平台原生字节序差异影响模型输入。
    int_samples = np.frombuffer(pcm16, dtype="<i2")
    return (int_samples.astype(np.float32) / 32768.0).astype(np.float32, copy=False)


def _coerce_asr_result(result: Any) -> dict[str, object]:
    """把底层 ASR worker 返回值归一化为字典。"""

    # 真实 worker 默认返回 dict，直接复用即可。
    if isinstance(result, dict):
        return result

    # 宽容处理字符串结果，方便未来替换同协议轻量 engine 时仍保持 provider 输出稳定。
    if isinstance(result, str):
        return {"text": result}

    # 某些 engine 结果对象可能携带 text 属性，这里只读取文本，不把对象泄露到上层。
    if hasattr(result, "text"):
        return {"text": str(result.text)}

    # 走到这里说明注入 worker 的返回协议不符合本 provider 的最小边界。
    raise TypeError("ASR worker 返回值必须是 dict、str 或带 text 属性的对象")
