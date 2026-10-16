"""定义可恢复的 StreamVox 运行配置快照。"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any, TYPE_CHECKING

from companion.core.execution_config import read_execution_configuration
from companion.core.runtime_profile import normalize_execution_selection
from companion.core.voice_setup_assets import sha256_file

if TYPE_CHECKING:
    from companion.character_cards.models import CharacterCardDefinition
    from companion.realtime.assistant_process_client import AssistantProcessClient


@dataclass(frozen=True, slots=True)
class StreamVoxRuntimeConfiguration:
    """保存一次 StreamVox 配置切换需要的全部输入。

    Args:
        model: StreamVox 模型名称。
        device: 推理设备选择。
        llama_backend: 可选的 llama.cpp 后端。
        role_name: 当前使用的音色名称。
        streamvox_seed: 可选的推理随机种子。
        inference_options: 当前模型族的推理参数。
        onnx_provider: 已确认的 ONNX 后端。
        model_directory: 已下载的模型目录快照。
        voice_directory: 已确认的角色音色缓存目录。
    """

    model: str
    device: str
    llama_backend: str | None
    role_name: str
    streamvox_seed: int | None
    inference_options: dict[str, Any]
    onnx_provider: str | None = None
    model_directory: str | None = None
    voice_directory: str | None = None

    @classmethod
    def from_runtime_state(
        cls,
        runtime_state: dict[str, object],
    ) -> "StreamVoxRuntimeConfiguration":
        """把运行时状态解析成之后可以重新应用的配置。

        Args:
            runtime_state: 助手子进程返回的 StreamVox 状态。

        Returns:
            已复制推理参数的不可变配置快照。

        Raises:
            RuntimeError: 状态缺少模型、音色或包含非法随机种子时抛出。
        """

        model = str(runtime_state.get("model", "")).strip()
        role_name = str(runtime_state.get("role_name", "")).strip()
        if not model or not role_name:
            raise RuntimeError("StreamVox runtime state missing model or role_name")

        raw_seed = runtime_state.get("streamvox_seed")
        if raw_seed is not None and (
            not isinstance(raw_seed, int) or isinstance(raw_seed, bool) or raw_seed < 0
        ):
            raise RuntimeError("StreamVox runtime state contains invalid streamvox_seed")

        raw_options = runtime_state.get("inference_options", {})
        if not isinstance(raw_options, dict):
            raise RuntimeError("StreamVox runtime state contains invalid inference_options")

        raw_onnx_provider = runtime_state.get("onnx_provider")
        if raw_onnx_provider not in {None, "cpu", "directml", "cuda"}:
            raise RuntimeError("StreamVox runtime state contains invalid onnx_provider")
        raw_llama_backend = runtime_state.get("llama_backend")
        llama_backend = (
            raw_llama_backend
            if raw_llama_backend in {"cuda", "vulkan", "cpu", None}
            else None
        )
        return cls(
            model=model,
            device=str(runtime_state.get("device", "auto")).strip() or "auto",
            llama_backend=llama_backend,
            role_name=role_name,
            streamvox_seed=raw_seed,
            inference_options=dict(raw_options),
            onnx_provider=raw_onnx_provider,
            model_directory=runtime_state.get("model_directory"),
            voice_directory=runtime_state.get("voice_directory"),
        )

    def matches_runtime_state(self, runtime_state: dict[str, object]) -> bool:
        """判断运行时是否已经完整采用本配置。

        Args:
            runtime_state: 助手子进程返回的当前状态。

        Returns:
            切换输入、后端和缓存归属全部相同时返回真；状态无法解析时返回假。
        """

        try:
            current_configuration = self.from_runtime_state(runtime_state)
        except RuntimeError:
            return False
        return current_configuration == self

    def apply(
        self,
        runtime: AssistantProcessClient,
        *,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        """把当前快照完整应用到助手子进程。

        Args:
            runtime: 支持 StreamVox 管理命令的助手客户端。
            timeout: 等待子进程完成切换的最长秒数。

        Returns:
            子进程切换后的权威运行状态。
        """

        return runtime.switch_streamvox_config(
            model=self.model,
            device=self.device,
            llama_backend=self.llama_backend,
            role_name=self.role_name,
            streamvox_seed=self.streamvox_seed,
            inference_options=dict(self.inference_options),
            timeout=timeout,
        )

    def user_settings_patch(self) -> dict[str, object]:
        """提取需要长期保存的 StreamVox 用户设置字段。

        Returns:
            设备与 llama.cpp 后端字段 patch；模型和音色仍由角色卡管理。
        """

        return {
            "streamvox_device": self.device,
            "streamvox_llama_backend": self.llama_backend,
        }


@dataclass(frozen=True, slots=True)
class PreparedStreamVoxVoice:
    """保存用户已确认的模型目录、音色缓存和执行方式。

    Args:
        model: 已确认的模型标识。
        role_name: 已创建并经过试听确认的音色。
        model_directory: 含原始清单和权重的本地模型目录。
        voice_directory: 按当前角色素材指纹隔离的 SDK 缓存根目录。
        onnx_provider: 用户确认的 ONNX 后端。
        llama_backend: 用户确认的 llama 后端。
        context: 创建音色时冻结的素材和执行配置。
    """

    model: str
    role_name: str
    model_directory: Path
    voice_directory: Path
    onnx_provider: str
    llama_backend: str
    context: dict[str, Any]


def read_prepared_streamvox_voice(*, execution_config_path: Path, voice_setup_path: Path,
                                 model: str, role_name: str) -> PreparedStreamVoxVoice:
    """正式语音只接受已经确认且仍对应当前模型和音色的配置。

    Args:
        execution_config_path: 用户确认的执行方式文件。
        voice_setup_path: 首次语音配置的持久状态文件。
        model: 当前角色卡的模型标识。
        role_name: 当前角色卡的音色名称。
    Returns:
        可直接用于本地 SDK 初始化的已验证配置；失效状态抛出 RuntimeError。
    """
    configuration = read_execution_configuration(execution_config_path)
    if configuration is None:
        raise RuntimeError("请先确认语音执行方式和模型目录")
    try:
        state = json.loads(voice_setup_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise RuntimeError("请先完成当前角色的语音配置") from error
    if not isinstance(state, dict) or state.get("schema_version") != 2 or state.get("phase") != "ready" or state.get("is_ready") is not True:
        raise RuntimeError("当前角色的语音配置尚未确认")
    context = state.get("context")
    if state.get("model_id") != model or state.get("role_name") != role_name or not isinstance(context, dict):
        raise RuntimeError("已确认语音与当前角色的模型或音色不匹配，请重新配置")
    if context.get("model_id") != model or context.get("role_name") != role_name:
        raise RuntimeError("已确认语音上下文的模型或音色不匹配，请重新配置")
    configured_execution = configuration.selection.model_dump()
    context_execution = context.get("execution")
    if context_execution != configured_execution:
        execution_matches_after_migration = False
        if (
            os.environ.get("NEARMATE_PACKAGED") == "1"
            or os.environ.get("NEARMATE_RUNTIME_PROFILE")
        ) and isinstance(context_execution, dict):
            context_onnx_provider = context_execution.get("onnx_provider")
            context_llama_backend = context_execution.get("llama_backend")
            if isinstance(context_onnx_provider, str) and isinstance(context_llama_backend, str):
                execution_matches_after_migration = (
                    normalize_execution_selection(
                        context_onnx_provider,
                        context_llama_backend,
                    )
                    == normalize_execution_selection(
                        configuration.selection.onnx_provider,
                        configuration.selection.llama_backend,
                    )
                )
        if not execution_matches_after_migration:
            raise RuntimeError("语音执行方式已改变，请重新准备当前角色音色")
    fingerprint = context.get("fingerprint")
    if not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
        raise RuntimeError("语音缓存指纹无效，请重新准备音色")
    frozen_context = {key: value for key, value in context.items() if key not in {"fingerprint", "reference_audio"}}
    encoded = json.dumps(frozen_context, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if hashlib.sha256(encoded).hexdigest() != fingerprint:
        raise RuntimeError("语音配置内容与缓存指纹不一致")
    root = Path(configuration.model_directory).expanduser()
    if not root.is_absolute() or not model or Path(model).name != model:
        raise RuntimeError("已确认的模型目录无效")
    root = root.resolve()
    model_directory = (root / model).resolve()
    if not model_directory.is_relative_to(root):
        raise RuntimeError("模型路径超出已确认目录")
    manifest_path = model_directory / "manifest.json"
    try:
        manifest_bytes = manifest_path.read_bytes()
        manifest = json.loads(manifest_bytes)
    except (OSError, ValueError) as error:
        raise RuntimeError("本地模型尚未下载完整，请重新校验") from error
    if not isinstance(manifest, dict) or manifest.get("model") != model or hashlib.sha256(manifest_bytes).hexdigest() != context.get("manifest_sha256"):
        raise RuntimeError("本地模型清单与已确认版本不一致")
    voice_directory = (voice_setup_path.resolve().parent / "voices" / fingerprint).resolve()
    if not voice_directory.is_dir():
        raise RuntimeError("当前角色音色缓存不存在，请重新准备音色")
    selected_onnx_provider = configuration.selection.onnx_provider
    selected_llama_backend = configuration.selection.llama_backend
    if os.environ.get("NEARMATE_PACKAGED") == "1" or os.environ.get("NEARMATE_RUNTIME_PROFILE"):
        # 发行包升级会保留旧用户配置；只要角色缓存本身仍通过指纹校验，就迁移后端选择而不强迫用户重新下载模型。
        selected_onnx_provider, selected_llama_backend = normalize_execution_selection(
            selected_onnx_provider,
            selected_llama_backend,
        )
    return PreparedStreamVoxVoice(model, role_name, model_directory, voice_directory,
                                 selected_onnx_provider, selected_llama_backend,
                                 dict(context))


def read_prepared_character_voice(
    *,
    card: CharacterCardDefinition,
    reference_audio: Path | None,
    execution_config_path: Path,
    voice_setup_path: Path,
) -> PreparedStreamVoxVoice:
    """读取角色已确认音色，并核对现在的角色素材是否仍是试听时那一份。

    Args:
        card: 当前准备使用语音的角色卡。
        reference_audio: 角色资产服务解析出的参考音频路径。
        execution_config_path: 已确认的执行方式文件。
        voice_setup_path: 已确认的语音配置文件。
    Returns:
        素材和推理参数均未变化的已确认音色；失效时抛出 RuntimeError。
    """

    binding = card.meta.voice_binding
    prepared = read_prepared_streamvox_voice(
        execution_config_path=execution_config_path,
        voice_setup_path=voice_setup_path,
        model=binding.streamvox_model,
        role_name=binding.role_name,
    )
    context = prepared.context
    if (
        context.get("card_id") != card.meta.card_id
        or reference_audio is None
        or not reference_audio.is_file()
        or sha256_file(reference_audio) != context.get("reference_audio_sha256")
        or binding.prompt_text != context.get("prompt_text")
        or binding.seed != context.get("seed")
        or dict(binding.inference_options) != context.get("inference_options")
    ):
        raise RuntimeError("角色参考素材或推理参数已改变，请重新完成语音配置")
    return prepared


def development_voice_fingerprint(*, card: CharacterCardDefinition, reference_audio: Path | None) -> str:
    """为源码开发态角色生成稳定的缓存指纹。

    Args:
        card: 当前角色卡。
        reference_audio: 当前角色的参考音频；不存在时仍可根据角色绑定生成指纹。

    Returns:
        只要角色绑定或参考音频发生变化就会变化的 SHA-256 指纹。
    """

    binding = card.meta.voice_binding
    payload = {
        "card_id": card.meta.card_id,
        "model": binding.streamvox_model,
        "role_name": binding.role_name,
        "prompt_text": binding.prompt_text,
        "seed": binding.seed,
        "inference_options": dict(binding.inference_options),
        "reference_audio_sha256": (
            sha256_file(reference_audio)
            if reference_audio is not None and reference_audio.is_file()
            else None
        ),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
