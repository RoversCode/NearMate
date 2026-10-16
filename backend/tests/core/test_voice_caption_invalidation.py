"""验证捕获运行音色只在语音身份变化时丢弃字幕校准。"""

from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from companion.character_cards.experience_assets import CharacterCardExperienceAssets
from companion.character_cards.models import CharacterCardDefinition, MetaSection, VoiceBindingSection, VoiceCaptionTimingSection
from companion.core.character_voice_service import CharacterVoiceService


@pytest.mark.parametrize('mutation', ['same', 'model', 'role', 'audio', 'prompt', 'seed', 'options'])
def test_capture_voice_invalidates_only_changed_binding(mutation):
    """捕获相同音色保留语速，更换模型、素材或推理参数后必须重新校准。

    Args:
        mutation: 要模拟的运行音色变化。

    Returns:
        无；检查实际保存的角色卡和原卡是否符合变更边界。
    """
    calibration = VoiceCaptionTimingSection(ms_per_unit=200, calibration_text='校准文字',
        duration_ms=1000, unit_count=5, measured_at=100)
    binding = VoiceBindingSection(streamvox_model='s2-pro-4b-gguf', role_name='huohua',
        reference_audio_path='voice/reference.wav', prompt_text='参考文字', seed=42,
        inference_options={}, audio_sha256='a' * 64, caption_timing=calibration)
    card = CharacterCardDefinition.model_construct(meta=MetaSection(card_id='huohua', name='火花', title='测试',
        voice_binding=binding))
    state = {'model': binding.streamvox_model, 'role_name': binding.role_name,
        'streamvox_seed': binding.seed, 'inference_options': {}}
    reference = SimpleNamespace(audio=b'captured-audio', audio_sha256=binding.audio_sha256)
    prompt = SimpleNamespace(references=[reference], prompt_text=[binding.prompt_text], sample_rate=24000)
    if mutation == 'model':
        state['model'] = 'voxcpm2-gguf'
    elif mutation == 'role':
        state['role_name'] = 'new-role'
    elif mutation == 'audio':
        reference.audio_sha256 = 'b' * 64
    elif mutation == 'prompt':
        prompt.prompt_text = ['新的参考文字']
    elif mutation == 'seed':
        state['streamvox_seed'] = 7
    elif mutation == 'options':
        state['inference_options'] = {'speed': 1.2}
    runtime = Mock()
    runtime.get_streamvox_runtime_state.return_value = state
    runtime.get_streamvox_prompt_data.return_value = prompt
    supervisor = Mock()
    supervisor.require_for_streamvox_management.return_value = runtime
    hub = Mock()
    hub.character_card_assets.save_captured_voice_reference_audio.return_value = binding.reference_audio_path
    service = CharacterVoiceService(hub=hub, assistant_process_supervisor=supervisor)

    result = service.capture_from_runtime(card)

    hub.character_cards.save_card.assert_called_once_with(result)
    assert result.meta.voice_binding.caption_timing == (calibration if mutation == 'same' else None)
    assert card.meta.voice_binding.caption_timing == calibration


def test_runtime_capture_rollback_restores_reference_audio_snapshot(tmp_path):
    """设置事务失败后的捕获补偿必须恢复旧参考音频字节。"""

    calibration = VoiceCaptionTimingSection(ms_per_unit=200, calibration_text="校准文字",
        duration_ms=1000, unit_count=5, measured_at=100)
    binding = VoiceBindingSection(streamvox_model="s2-pro-4b-gguf", role_name="huohua",
        reference_audio_path="voice/reference.wav", prompt_text="参考文字", seed=42,
        inference_options={}, audio_sha256="a" * 64, caption_timing=calibration)
    card = CharacterCardDefinition.model_construct(
        meta=MetaSection(card_id="huohua", name="火花", title="测试", voice_binding=binding)
    )
    assets = CharacterCardExperienceAssets(tmp_path / "cards")
    old_audio = b"old-reference-audio"
    old_audio_path = assets.card_dir("huohua") / "voice" / "reference.wav"
    old_audio_path.parent.mkdir(parents=True, exist_ok=True)
    old_audio_path.write_bytes(old_audio)

    state = {
        "model": binding.streamvox_model,
        "role_name": binding.role_name,
        "streamvox_seed": binding.seed,
        "inference_options": {},
    }
    reference = SimpleNamespace(audio=np.asarray([0.1, -0.1], dtype=np.float32), audio_sha256=binding.audio_sha256)
    prompt = SimpleNamespace(references=[reference], prompt_text=[binding.prompt_text], sample_rate=24000)
    runtime = Mock()
    runtime.get_streamvox_runtime_state.return_value = state
    runtime.get_streamvox_prompt_data.return_value = prompt
    supervisor = Mock()
    supervisor.require_for_streamvox_management.return_value = runtime
    hub = SimpleNamespace(
        character_card_assets=assets,
        character_cards=Mock(),
    )
    service = CharacterVoiceService(hub=hub, assistant_process_supervisor=supervisor)

    capture = service.prepare_capture_from_runtime(card)
    assert old_audio_path.read_bytes() != old_audio
    service.rollback_capture_from_runtime(capture)

    assert old_audio_path.read_bytes() == old_audio
    hub.character_cards.save_card.assert_any_call(card)


def test_voice_status_degrades_when_prompt_rpc_becomes_unavailable() -> None:
    """读取 prompt 的管理 RPC 失败时，状态查询必须降级而不是抛出异常。

    角色卡激活、运行时捕获和字幕校准都会先提交统一设置事务，再读取一次
    语音匹配状态。此时助手进程可能正好热替换或超时，状态查询不能把已经提交
    的配置伪装成保存失败。
    """

    binding = VoiceBindingSection(
        streamvox_model="s2-pro-4b-gguf",
        role_name="huohua",
        prompt_text="参考文字",
        audio_sha256="a" * 64,
    )
    card = CharacterCardDefinition.model_construct(
        meta=MetaSection.model_construct(
            card_id="huohua",
            name="火花",
            title="测试",
            voice_binding=binding,
        )
    )
    runtime = Mock()
    runtime.get_streamvox_runtime_state.return_value = {
        "model": binding.streamvox_model,
        "role_name": binding.role_name,
        "roles": [binding.role_name],
    }
    runtime.get_streamvox_prompt_data.side_effect = RuntimeError(
        "StreamVox management request timed out: get_prompt_data"
    )
    supervisor = Mock()
    supervisor.require_for_streamvox_management.return_value = runtime
    assets = SimpleNamespace(
        has_voice_assets=lambda _card: True,
        read_voice_prompt_text=lambda _card: binding.prompt_text,
    )
    service = CharacterVoiceService(
        hub=SimpleNamespace(character_card_assets=assets),
        assistant_process_supervisor=supervisor,
    )

    status = service.status(card)

    assert status["voice_match_status"] == "runtime_unavailable"
    assert status["runtime"] is None
    assert status["expected"] == binding.model_dump(mode="json")
