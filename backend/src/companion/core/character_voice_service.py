from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
import os

from companion.character_cards.models import CharacterCardDefinition
from companion.character_cards.experience_assets import VoiceReferenceAudioSnapshot
from companion.character_cards.store import CharacterCardStoreError
from companion.core.assistant_process_supervisor import AssistantProcessSupervisor
from companion.core.application.errors import StateConflict
from companion.core.broadcast import BroadcastHub
from companion.core.settings import Settings
from companion.core.streamvox_runtime_config import (
    StreamVoxRuntimeConfiguration,
    read_prepared_character_voice,
)
from companion.core.voice_caption_calibration import (
    CAPTION_CALIBRATION_TEXT,
    build_caption_timing_from_wav,
    invalidate_changed_voice_caption_timing,
)
from companion.observability.logging_config import get_backend_logger


logger = get_backend_logger(__name__)


@dataclass(frozen=True, slots=True)
class CharacterVoiceApplicationResult:
    """记录角色语音应用结果和失败补偿所需事实。

    Args:
        payload: API 可以直接返回的语音匹配状态。
        settings_patch: StreamVox 实际设备对应的用户设置字段。
        previous_runtime_configuration: 切换前可重新应用的运行配置。
        original_card: 本次操作开始前的角色卡。
        restore_card_on_rollback: 失败时是否需要把角色卡写回原值。
    """

    payload: dict[str, object]
    settings_patch: dict[str, object]
    previous_runtime_configuration: StreamVoxRuntimeConfiguration
    original_card: CharacterCardDefinition
    restore_card_on_rollback: bool


@dataclass(frozen=True, slots=True)
class CharacterVoiceCalibrationResult:
    """保存字幕语速校准结果及其语音应用补偿信息。

    Args:
        card: 已写入字幕语速的最新角色卡。
        application: 本次校准前置语音应用的结果。
    """

    card: CharacterCardDefinition
    application: CharacterVoiceApplicationResult


@dataclass(frozen=True, slots=True)
class CharacterVoiceCaptureResult:
    """记录一次运行时捕获及其外部资产补偿信息。

    Args:
        card: 捕获后要返回给控制台的角色卡。
        original_card: 捕获前磁盘中的角色卡，用于设置事务失败时恢复。
        previous_audio_snapshot: 捕获前的参考音频目录快照。
    """

    card: CharacterCardDefinition
    original_card: CharacterCardDefinition
    previous_audio_snapshot: VoiceReferenceAudioSnapshot


class CharacterVoiceService:
    """管理角色卡语音绑定与 StreamVox 运行状态的匹配。

    Args:
        hub: 控制面广播和角色卡资产中心。
        assistant_process_supervisor: 当前助手子进程所有者。
    """

    def __init__(
        self,
        *,
        hub: BroadcastHub,
        assistant_process_supervisor: AssistantProcessSupervisor,
    ) -> None:
        self.hub = hub
        self.assistant_process_supervisor = assistant_process_supervisor

    def prompt_data_signature(self, prompt_data: object | None) -> tuple[str | None, str | None]:
        """提取 StreamVox prompt 数据中的文本和参考音频身份。

        Args:
            prompt_data: runtime 返回的 PromptData 对象或测试替身。

        Returns:
            第一条 prompt 文本和第一条参考音频哈希；缺失时对应返回 None。
        """

        if prompt_data is None:
            return None, None

        prompt_text_items = list(getattr(prompt_data, "prompt_text", []))
        first_prompt_text = prompt_text_items[0] if prompt_text_items else None
        normalized_prompt_text = first_prompt_text.strip() if isinstance(first_prompt_text, str) else None

        references = tuple(getattr(prompt_data, "references", ()))
        first_reference = references[0] if references else None
        audio_sha256 = getattr(first_reference, "audio_sha256", None)
        normalized_audio_sha256 = audio_sha256.strip() if isinstance(audio_sha256, str) else None
        return normalized_prompt_text or None, normalized_audio_sha256 or None

    def is_prompt_data_matching_character_voice(
        self,
        card: CharacterCardDefinition,
        prompt_data: object | None,
    ) -> bool:
        """判断 StreamVox 已有音色是否可信复用。

        Args:
            card: 当前角色卡。
            prompt_data: StreamVox role 的 prompt 数据。

        Returns:
            文本和参考音频哈希同时匹配时返回 True。
        """

        prompt_text, audio_sha256 = self.prompt_data_signature(prompt_data)
        expected_prompt_text = self.hub.character_card_assets.read_voice_prompt_text(card)
        expected_audio_sha256 = card.meta.voice_binding.audio_sha256
        return (
            expected_prompt_text is not None
            and expected_audio_sha256 is not None
            and prompt_text == expected_prompt_text
            and audio_sha256 == expected_audio_sha256
        )

    def status(self, card: CharacterCardDefinition) -> dict[str, object]:
        """计算角色卡语音绑定与当前 StreamVox runtime 的匹配状态。

        Args:
            card: 要检查的角色卡快照。

        Returns:
            包含匹配结论、运行时摘要和角色卡期望绑定的字典。助手进程正在
            热替换、管理 RPC 超时或角色音频资产暂时不可读时，返回
            ``runtime_unavailable``，不会让已经完成的设置提交变成 HTTP 500。
        """

        expected = card.meta.voice_binding.model_dump(mode="json")

        def runtime_unavailable(exception: Exception) -> dict[str, object]:
            """把附属运行时读取故障转换成稳定的只读状态。"""

            logger.warning(
                "读取 StreamVox 角色语音状态失败，降级为 runtime_unavailable："
                " card_id=%s role_name=%s error_type=%s error=%s",
                card.meta.card_id,
                card.meta.voice_binding.role_name,
                type(exception).__name__,
                exception,
            )
            return {
                "voice_match_status": "runtime_unavailable",
                "runtime": None,
                "expected": expected,
            }

        try:
            runtime = self.assistant_process_supervisor.require_for_streamvox_management()
            runtime_state = runtime.get_streamvox_runtime_state(timeout=5.0)
            if not isinstance(runtime_state, dict):
                raise RuntimeError("StreamVox runtime state is not an object")

            expected_model = card.meta.voice_binding.streamvox_model
            expected_role = card.meta.voice_binding.role_name
            runtime_model = str(runtime_state.get("model", ""))
            runtime_role = str(runtime_state.get("role_name", ""))
            runtime_roles = [
                str(role)
                for role in runtime_state.get("roles", [])
                if isinstance(role, str)
            ]
            has_voice_assets = self.hub.character_card_assets.has_voice_assets(card)

            if runtime_model != expected_model:
                status = "mismatched"
            elif expected_role not in runtime_roles and not has_voice_assets:
                status = "missing_assets"
            elif expected_role not in runtime_roles:
                status = "missing_role"
            else:
                prompt_data_reader = getattr(runtime, "get_streamvox_prompt_data", None)
                if not callable(prompt_data_reader):
                    status = "mismatched" if has_voice_assets else "missing_assets"
                    return {
                        "voice_match_status": status,
                        "runtime": runtime_state,
                        "expected": expected,
                    }

                # 角色卡激活等写操作已经在前面完成；这里的管理 RPC 只是附属状态读取。
                # 助手热替换或短暂超时不应把已提交的设置误报成 HTTP 500。
                prompt_data = prompt_data_reader(role_name=expected_role, timeout=5.0)
                prompt_data_matches = self.is_prompt_data_matching_character_voice(
                    card,
                    prompt_data,
                )
                if not prompt_data_matches and not has_voice_assets:
                    status = "missing_assets"
                elif prompt_data_matches and runtime_role == expected_role:
                    status = "matched"
                else:
                    status = "mismatched"

            return {
                "voice_match_status": status,
                "runtime": runtime_state,
                "expected": expected,
            }
        except Exception as exception:  # noqa: BLE001
            # 状态查询是提交后的附属投影；无论是 RPC、运行时返回格式还是本地
            # 参考音频读取失败，都只能影响匹配标记，不能推翻已经落盘的设置。
            return runtime_unavailable(exception)

    def apply(
        self,
        card: CharacterCardDefinition,
        *,
        current_settings: Settings,
    ) -> CharacterVoiceApplicationResult:
        """应用当前角色已经试听确认的音色；素材失效时要求重新配置。

        Args:
            card: 准备应用的最新角色卡。
            current_settings: 统一设置锁保护下的最新设置。

        Returns:
            API 状态、设置 patch 和失败补偿快照。
        """

        expected_model = card.meta.voice_binding.streamvox_model
        expected_role = card.meta.voice_binding.role_name
        expected_options = dict(card.meta.voice_binding.inference_options)
        expected_seed = card.meta.voice_binding.seed
        development_fallback_allowed = (
            os.environ.get("NEARMATE_PACKAGED") != "1"
            and hasattr(current_settings, "streamvox_device")
            and (
                not Path(current_settings.execution_config_path).is_file()
                or not Path(current_settings.voice_setup_path).is_file()
            )
        )
        prepared = None
        try:
            prepared = read_prepared_character_voice(
                card=card,
                reference_audio=self.hub.character_card_assets.resolve_voice_reference_audio(card),
                execution_config_path=Path(current_settings.execution_config_path),
                voice_setup_path=Path(current_settings.voice_setup_path),
            )
        except (RuntimeError, OSError) as error:
            if not development_fallback_allowed:
                raise StateConflict(str(error)) from error
            # 源码开发需要支持未完成首次语音引导时切换角色；助手进程会用旧式
            # 按模型名加载方式启动 StreamVox，安装包仍由上面的正式门禁保护。
            logger.info("源码开发态使用 StreamVox 回退配置：%s", error)

        # 未准备好的目标必须在任何切换或补偿前拒绝，避免打断仍可用的当前语音。
        runtime = self.assistant_process_supervisor.require_for_streamvox_management()
        runtime_state = runtime.get_streamvox_runtime_state(timeout=5.0)
        previous_runtime_configuration = StreamVoxRuntimeConfiguration.from_runtime_state(runtime_state)
        if prepared is not None and (
            str(runtime_state.get("model", "")) == expected_model
            and runtime_state.get("voice_directory") == str(prepared.voice_directory)
        ):
            roles = [
                str(role)
                for role in runtime_state.get("roles", [])
                if isinstance(role, str)
            ]
            if expected_role not in roles:
                raise StateConflict("当前角色音色缓存缺失，请重新完成语音配置")
            prompt_data = runtime.get_streamvox_prompt_data(role_name=expected_role, timeout=5.0)
            if not self.is_prompt_data_matching_character_voice(card, prompt_data):
                raise StateConflict("当前角色音色与已确认素材不匹配，请重新完成语音配置")

        try:
            runtime_state = runtime.switch_streamvox_config(
                model=expected_model,
                device=(
                    "gpu" if prepared.onnx_provider in {"directml", "cuda"}
                    else "cpu"
                ) if prepared is not None else getattr(current_settings, "streamvox_device", "auto"),
                llama_backend=prepared.llama_backend if prepared is not None else getattr(current_settings, "streamvox_llama_backend", None),
                role_name=expected_role,
                streamvox_seed=expected_seed,
                inference_options=expected_options,
                timeout=30.0,
            )
            applied_configuration = StreamVoxRuntimeConfiguration.from_runtime_state(
                runtime_state
            )
            return CharacterVoiceApplicationResult(
                payload=self.status(card),
                settings_patch=applied_configuration.user_settings_patch(),
                previous_runtime_configuration=previous_runtime_configuration,
                original_card=card,
                restore_card_on_rollback=False,
            )
        except BaseException:
            failed_application = CharacterVoiceApplicationResult(
                payload={},
                settings_patch={},
                previous_runtime_configuration=previous_runtime_configuration,
                original_card=card,
                restore_card_on_rollback=False,
            )
            try:
                self.rollback_application(failed_application)
            except Exception:
                logger.exception("角色语音应用失败后的运行状态补偿失败")
            raise

    def rollback_application(
        self,
        application: CharacterVoiceApplicationResult,
    ) -> None:
        """恢复一次未提交角色语音应用改动的运行时和角色卡。

        Args:
            application: `apply` 返回的补偿事实。

        Returns:
            无返回值。

        Raises:
            RuntimeError: 任一补偿动作失败时，在尝试完全部动作后抛出。
        """

        rollback_errors: list[Exception] = []
        try:
            runtime = self.assistant_process_supervisor.require_for_streamvox_management()
            application.previous_runtime_configuration.apply(runtime)
        except Exception as exception:
            rollback_errors.append(exception)

        if application.restore_card_on_rollback:
            try:
                self.hub.character_cards.save_card(application.original_card)
            except Exception as exception:
                rollback_errors.append(exception)

        if rollback_errors:
            raise RuntimeError("角色语音运行状态补偿失败") from rollback_errors[0]

    def calibrate_caption_timing(
        self,
        card: CharacterCardDefinition,
        *,
        current_settings: Settings,
    ) -> CharacterVoiceCalibrationResult:
        """合成标准文本并把当前角色音色的字幕语速写回角色卡。

        Args:
            card: 待校准的角色卡。
            current_settings: 统一设置锁保护下的最新设置。

        Returns:
            已写入 caption_timing 的角色卡和失败补偿信息。
        """

        application = self.apply(card, current_settings=current_settings)
        try:
            latest_card = self.hub.character_cards.load_card(card.meta.card_id)
            runtime = self.assistant_process_supervisor.require_for_streamvox_management()
            preview = runtime.preview_streamvox_role(
                role_name=latest_card.meta.voice_binding.role_name,
                text=CAPTION_CALIBRATION_TEXT,
                timeout=30.0,
            )
            wav_bytes = preview.get("wav_bytes", b"")
            if not isinstance(wav_bytes, bytes):
                raise RuntimeError("StreamVox preview response missing wav bytes")

            caption_timing = build_caption_timing_from_wav(wav_bytes)
            updated_card = latest_card.model_copy(
                deep=True,
                update={
                    "meta": latest_card.meta.model_copy(
                        update={
                            "voice_binding": latest_card.meta.voice_binding.model_copy(
                                update={"caption_timing": caption_timing}
                            )
                        }
                    )
                },
            )
            self.hub.character_cards.save_card(updated_card)
            return CharacterVoiceCalibrationResult(
                card=updated_card,
                application=replace(
                    application,
                    restore_card_on_rollback=True,
                ),
            )
        except BaseException:
            try:
                self.rollback_application(
                    replace(application, restore_card_on_rollback=True)
                )
            except Exception:
                logger.exception("字幕语速校准失败后的角色语音补偿失败")
            raise

    def capture_from_runtime(
        self,
        card: CharacterCardDefinition,
    ) -> CharacterCardDefinition:
        """把当前 StreamVox 运行语音写回当前激活角色卡。

        这是保留给同步调用方的便捷入口。需要和设置 revision 一起提交的
        API 应使用 :meth:`prepare_capture_from_runtime`，并在提交失败时调用
        :meth:`rollback_capture_from_runtime`。

        Args:
            card: 要写入捕获语音的角色卡。

        Returns:
            已保存捕获绑定的角色卡。
        """

        return self.prepare_capture_from_runtime(card).card

    def prepare_capture_from_runtime(
        self,
        card: CharacterCardDefinition,
    ) -> CharacterVoiceCaptureResult:
        """准备并持久化一次运行时音色捕获，返回可回滚的完整事实。

        Args:
            card: 要写入捕获语音的角色卡。

        Returns:
            捕获后的角色卡以及恢复旧角色资产所需的快照。

        Raises:
            StateConflict: 当前 StreamVox 运行态缺少可捕获的角色或 prompt。
            CharacterCardStoreError: 语音资产或角色卡无法写入。
        """

        runtime = self.assistant_process_supervisor.require_for_streamvox_management()
        runtime_state = runtime.get_streamvox_runtime_state(timeout=5.0)
        role_name = str(runtime_state.get("role_name", "")).strip()
        if not role_name:
            raise StateConflict("current StreamVox runtime has no role name")

        prompt_data = runtime.get_streamvox_prompt_data(role_name=role_name, timeout=5.0)
        if prompt_data is None:
            raise StateConflict("current StreamVox role has no prompt data")

        references = tuple(getattr(prompt_data, "references", ()))
        if not references:
            raise StateConflict("current StreamVox role has no prompt references")
        first_reference = references[0]
        prompt_text_items = list(getattr(prompt_data, "prompt_text", []))
        first_prompt_text = prompt_text_items[0] if prompt_text_items else None
        if not isinstance(first_prompt_text, str) or not first_prompt_text.strip():
            raise StateConflict("current StreamVox role prompt text is empty")

        previous_audio_snapshot = self.hub.character_card_assets.snapshot_voice_reference_audio(
            card.meta.card_id
        )
        try:
            reference_audio_path = self.hub.character_card_assets.save_captured_voice_reference_audio(
                card_id=card.meta.card_id,
                audio=first_reference.audio,
                sample_rate=int(getattr(prompt_data, "sample_rate", 0)),
            )
        except CharacterCardStoreError as exception:
            raise StateConflict(str(exception)) from exception

        runtime_options = runtime_state.get("inference_options", {})
        if not isinstance(runtime_options, dict):
            runtime_options = {}

        captured_binding = card.meta.voice_binding.model_copy(
            update={
                "streamvox_model": str(runtime_state.get("model", "")),
                "role_name": role_name,
                "reference_audio_path": reference_audio_path,
                "prompt_text": first_prompt_text.strip(),
                "seed": runtime_state.get("streamvox_seed"),
                "inference_options": dict(runtime_options),
                "audio_sha256": str(first_reference.audio_sha256),
            }
        )
        captured_binding = invalidate_changed_voice_caption_timing(card.meta.voice_binding, captured_binding)
        updated_card = card.model_copy(
            deep=True,
            update={
                "meta": card.meta.model_copy(
                    update={
                        "voice_binding": captured_binding,
                    }
                )
            },
        )
        try:
            self.hub.character_cards.save_card(updated_card)
        except BaseException:
            # 音频已经写入但 YAML 保存失败时，不能把孤儿 reference.wav 留在卡目录；
            # 先恢复资产，再把原始异常继续交给统一事务映射。
            try:
                self.hub.character_card_assets.restore_voice_reference_audio(
                    card.meta.card_id,
                    previous_audio_snapshot,
                )
            except Exception:
                logger.exception("运行时音色捕获的局部资产回滚失败")
            raise
        return CharacterVoiceCaptureResult(
            card=updated_card,
            original_card=card,
            previous_audio_snapshot=previous_audio_snapshot,
        )

    def rollback_capture_from_runtime(
        self,
        capture: CharacterVoiceCaptureResult,
    ) -> None:
        """恢复一次未提交运行时捕获修改的角色卡和音频资产。

        Args:
            capture: :meth:`prepare_capture_from_runtime` 返回的补偿事实。

        Returns:
            无返回值。

        Raises:
            RuntimeError: 角色卡或音频资产任一部分恢复失败。
        """

        rollback_errors: list[Exception] = []
        try:
            self.hub.character_cards.save_card(capture.original_card)
        except Exception as exception:
            rollback_errors.append(exception)
        try:
            self.hub.character_card_assets.restore_voice_reference_audio(
                capture.original_card.meta.card_id,
                capture.previous_audio_snapshot,
            )
        except Exception as exception:
            rollback_errors.append(exception)
        if rollback_errors:
            raise RuntimeError("运行时音色捕获回滚失败") from rollback_errors[0]
