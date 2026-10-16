"""验证正式语音只消费当前角色已确认的本地模型与音色。"""

import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from companion.audio.providers.tts_provider import StreamVoxProviderConfig, StreamVoxTtsProvider
from companion.core.streamvox_runtime_config import read_prepared_streamvox_voice
from companion.core.application.errors import StateConflict
from companion.core.character_voice_service import CharacterVoiceService
from companion.realtime.assistant_process import _build_assistant_child_runtime


@pytest.fixture
def prepared_voice(tmp_path):
    """写入与首次语音配置相同结构的确认状态。"""
    model = 'qwen3-tts-clone-0.6b-gguf'
    model_directory = tmp_path / 'models' / model
    model_directory.mkdir(parents=True)
    manifest_bytes = json.dumps({'model': model, 'assets': []}).encode()
    (model_directory / 'manifest.json').write_bytes(manifest_bytes)
    selection = {'onnx_provider': 'directml', 'llama_backend': 'cpu'}
    reference_audio = tmp_path / 'reference.wav'
    reference_audio.write_bytes(b'current-card-reference')
    context = {'card_id': 'current-card', 'role_name': 'current-card-role', 'model_id': model, 'model_revision': 'a' * 40,
               'manifest_sha256': hashlib.sha256(manifest_bytes).hexdigest(),
               'reference_audio_sha256': hashlib.sha256(reference_audio.read_bytes()).hexdigest(),
               'prompt_text': '当前角色参考文字',
               'seed': None, 'inference_options': {}, 'execution': selection}
    encoded = json.dumps(context, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
    fingerprint = hashlib.sha256(encoded).hexdigest()
    context.update(fingerprint=fingerprint, reference_audio=str(tmp_path / 'reference.wav'))
    execution_path = tmp_path / 'execution-config.json'
    execution_path.write_text(json.dumps({'schema_version': 1, 'selection': selection,
                                         'model_directory': str(model_directory.parent)}))
    setup_path = tmp_path / 'voice-setup.json'
    setup_path.write_text(json.dumps({'schema_version': 2, 'phase': 'ready', 'is_ready': True,
                                     'model_id': model, 'role_name': 'current-card-role', 'context': context}))
    voice_directory = tmp_path / 'voices' / fingerprint
    voice_directory.mkdir(parents=True)
    return StreamVoxProviderConfig(model=model, role_name='current-card-role',
                                   device='gpu', llama_backend='cpu', onnx_provider='directml',
                                   model_directory=model_directory, voice_directory=voice_directory,
                                   execution_config_path=execution_path, voice_setup_path=setup_path)


def read_config(config):
    """读取夹具对应的正式状态。"""
    return read_prepared_streamvox_voice(execution_config_path=config.execution_config_path,
                                        voice_setup_path=config.voice_setup_path,
                                        model=config.model, role_name=config.role_name)


def test_formal_engine_uses_local_path_and_confirmed_backends(prepared_voice, monkeypatch):
    """真实构造路径必须启用哈希校验，并在导入 SDK 前固定缓存目录。"""
    observed = {}
    engine = SimpleNamespace(runtime=SimpleNamespace(sample_rate=24_000), list_roles=lambda: ['current-card-role'],
                             actual_backends={'llama': 'cpu', 'onnx': 'directml', 'onnx_sessions': {}})

    def create_engine(**kwargs):
        observed.update(kwargs)
        observed['voice_home'] = os.environ['STREAMVOX_HOME']
        return engine

    monkeypatch.setitem(sys.modules, 'streamvox', SimpleNamespace(TTSEngine=create_engine))

    provider = StreamVoxTtsProvider(config=prepared_voice)
    assert observed['model'] == str(prepared_voice.model_directory)
    assert observed['voice_home'] == str(prepared_voice.voice_directory)
    assert observed['verify_model_sha256'] is True
    assert observed['onnx_provider'] == 'directml'
    assert observed['llama_backend'] == 'cpu'
    assert provider.runtime_state()['actual_backends'] == engine.actual_backends


def test_formal_engine_rejects_missing_confirmation_before_import(monkeypatch):
    """缺少确认状态不能回到按模型名自动下载的入口。"""
    create_engine = Mock()
    monkeypatch.setitem(sys.modules, 'streamvox', SimpleNamespace(TTSEngine=create_engine))
    with pytest.raises(RuntimeError, match='已确认'):
        StreamVoxTtsProvider(config=StreamVoxProviderConfig(role_name='current-card-role'))
    create_engine.assert_not_called()


def test_source_development_can_use_legacy_streamvox_loading(monkeypatch):
    """源码开发态没有首次配置文件时，允许沿用按模型名加载的旧入口。"""
    engine = SimpleNamespace(
        runtime=SimpleNamespace(sample_rate=24_000),
        list_roles=lambda: ['demo_role'],
        make_prompt=Mock(),
    )
    create_engine = Mock(return_value=engine)
    monkeypatch.delenv('NEARMATE_PACKAGED', raising=False)
    monkeypatch.setitem(sys.modules, 'streamvox', SimpleNamespace(TTSEngine=create_engine))


    provider = StreamVoxTtsProvider(
        config=StreamVoxProviderConfig(
            role_name='demo_role',
            allow_development_fallback=True,
        )
    )

    assert provider.runtime_state()['role_name'] == 'demo_role'
    create_engine.assert_called_once()
    assert create_engine.call_args.kwargs['model'] == 'qwen3-tts-clone-0.6b-gguf'
    assert create_engine.call_args.kwargs['verify_model_sha256'] is False


def test_missing_role_does_not_create_demo_voice():
    """旧全局缓存里有其他音色，也不能替代当前角色。"""
    engine = SimpleNamespace(runtime=SimpleNamespace(sample_rate=24_000), list_roles=lambda: ['old-role'], make_prompt=Mock())
    with pytest.raises(RuntimeError, match='尚未准备'):
        StreamVoxTtsProvider(engine=engine, config=StreamVoxProviderConfig(role_name='current-card-role'))
    engine.make_prompt.assert_not_called()


def test_unconfirmed_model_switch_preserves_running_engine():
    """预检必须在取消当前回复和释放旧模型之前完成。"""
    engine = SimpleNamespace(runtime=SimpleNamespace(sample_rate=24_000), list_roles=lambda: ['current-card-role'], shutdown=Mock())
    provider = StreamVoxTtsProvider(engine=engine, config=StreamVoxProviderConfig(role_name='current-card-role'))
    before_switch = Mock()
    with pytest.raises(RuntimeError, match='先完成新模型'):
        provider.switch_config(model='s2-pro-4b-gguf', device='cpu', llama_backend='cpu',
                               role_name='new-card-role', before_switch=before_switch)
    before_switch.assert_not_called()
    engine.shutdown.assert_not_called()


def test_changed_execution_invalidates_voice_cache(prepared_voice):
    """已生成的音色不能借新后端选择继续显示就绪。"""
    payload = json.loads(prepared_voice.execution_config_path.read_text())
    payload['selection']['onnx_provider'] = 'cpu'
    prepared_voice.execution_config_path.write_text(json.dumps(payload))
    with pytest.raises(RuntimeError, match='执行方式已改变'):
        read_config(prepared_voice)


def test_packaged_universal_migrates_stale_cuda_voice_confirmation(prepared_voice, monkeypatch):
    """通用包升级后，仍可复用已确认音色，但必须改用随包后端。"""

    stale_selection = {'onnx_provider': 'cuda', 'llama_backend': 'cuda'}
    execution_payload = json.loads(prepared_voice.execution_config_path.read_text())
    execution_payload['selection'] = stale_selection
    prepared_voice.execution_config_path.write_text(json.dumps(execution_payload))

    setup_payload = json.loads(prepared_voice.voice_setup_path.read_text())
    context = setup_payload['context']
    context['execution'] = stale_selection
    unsigned_context = {
        key: value for key, value in context.items() if key not in {'fingerprint', 'reference_audio'}
    }
    encoded = json.dumps(unsigned_context, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
    fingerprint = hashlib.sha256(encoded).hexdigest()
    context['fingerprint'] = fingerprint
    prepared_voice.voice_setup_path.write_text(json.dumps(setup_payload))
    (prepared_voice.voice_setup_path.parent / 'voices' / fingerprint).mkdir(parents=True)

    monkeypatch.setenv('NEARMATE_PACKAGED', '1')
    monkeypatch.setenv('NEARMATE_RUNTIME_PROFILE', 'universal')
    migrated = read_config(prepared_voice)

    assert migrated.onnx_provider == 'directml'
    assert migrated.llama_backend == 'vulkan'


def test_packaged_universal_accepts_migrated_context_with_legacy_execution_config(prepared_voice, monkeypatch):
    """新确认状态写入 Universal 后，旧执行配置仍能在下次启动复用音色。"""

    stale_selection = {'onnx_provider': 'cuda', 'llama_backend': 'cuda'}
    execution_payload = json.loads(prepared_voice.execution_config_path.read_text())
    execution_payload['selection'] = stale_selection
    prepared_voice.execution_config_path.write_text(json.dumps(execution_payload))

    setup_payload = json.loads(prepared_voice.voice_setup_path.read_text())
    context = setup_payload['context']
    context['execution'] = {'onnx_provider': 'directml', 'llama_backend': 'vulkan'}
    unsigned_context = {
        key: value for key, value in context.items() if key not in {'fingerprint', 'reference_audio'}
    }
    encoded = json.dumps(unsigned_context, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
    fingerprint = hashlib.sha256(encoded).hexdigest()
    context['fingerprint'] = fingerprint
    prepared_voice.voice_setup_path.write_text(json.dumps(setup_payload))
    (prepared_voice.voice_setup_path.parent / 'voices' / fingerprint).mkdir(parents=True)

    monkeypatch.setenv('NEARMATE_PACKAGED', '1')
    monkeypatch.setenv('NEARMATE_RUNTIME_PROFILE', 'universal')
    migrated = read_config(prepared_voice)

    assert migrated.onnx_provider == 'directml'
    assert migrated.llama_backend == 'vulkan'


def test_changed_local_manifest_requires_repair(prepared_voice):
    """本地目录有同名模型也必须匹配已确认的清单摘要。"""
    (prepared_voice.model_directory / 'manifest.json').write_text('{}')
    with pytest.raises(RuntimeError, match='清单与已确认版本'):
        read_config(prepared_voice)


def test_context_tampering_cannot_reuse_old_cache(prepared_voice):
    """修改参考文字后旧指纹必须失效。"""
    payload = json.loads(prepared_voice.voice_setup_path.read_text())
    payload['context']['prompt_text'] = 'changed'
    prepared_voice.voice_setup_path.write_text(json.dumps(payload))
    with pytest.raises(RuntimeError, match='缓存指纹不一致'):
        read_config(prepared_voice)


def test_context_without_role_name_is_not_a_valid_confirmation(prepared_voice):
    """同版本旧状态缺少音色归属时，不能借已有指纹继续运行。"""
    payload = json.loads(prepared_voice.voice_setup_path.read_text())
    payload['context'].pop('role_name')
    prepared_voice.voice_setup_path.write_text(json.dumps(payload))
    with pytest.raises(RuntimeError, match='上下文的模型或音色不匹配'):
        read_config(prepared_voice)


@pytest.mark.parametrize('changed_directory', ['model_directory', 'voice_directory'])
def test_changed_confirmed_directory_rebuilds_same_model(prepared_voice, changed_directory):
    """模型名和后端没变，目录变化也必须让新引擎接管新的模型或音色缓存。"""
    old_engine = SimpleNamespace(runtime=SimpleNamespace(sample_rate=24_000),
        list_roles=lambda: ['current-card-role'], shutdown=Mock())
    next_engine = SimpleNamespace(runtime=SimpleNamespace(sample_rate=24_000),
        list_roles=lambda: ['current-card-role'])
    old_config = replace(prepared_voice, **{changed_directory: Path('previous-directory')})
    provider = StreamVoxTtsProvider(engine=old_engine, config=old_config)
    provider._build_engine = Mock(return_value=next_engine)
    provider.switch_config(model=prepared_voice.model, device='gpu', llama_backend='cpu',
                           role_name=prepared_voice.role_name, inference_options={})
    old_engine.shutdown.assert_called_once()
    provider._build_engine.assert_called_once()
    assert provider.config.model_directory == prepared_voice.model_directory
    assert provider.config.voice_directory == prepared_voice.voice_directory


@pytest.fixture
def character_voice_application(prepared_voice):
    """建立持有真实确认文件、但不启动原生模型的角色服务。"""
    card = SimpleNamespace(meta=SimpleNamespace(card_id='current-card', voice_binding=SimpleNamespace(
        streamvox_model=prepared_voice.model, role_name=prepared_voice.role_name,
        prompt_text='当前角色参考文字', seed=None, inference_options={}, audio_sha256='sdk-audio-hash',
    )))
    runtime_state = {'model': prepared_voice.model, 'role_name': prepared_voice.role_name,
                     'device': 'gpu', 'llama_backend': 'cpu', 'roles': [prepared_voice.role_name],
                     'voice_directory': str(prepared_voice.voice_directory),
                     'streamvox_seed': None, 'inference_options': {}}
    runtime = Mock()
    runtime.get_streamvox_runtime_state.return_value = runtime_state
    runtime.switch_streamvox_config.return_value = runtime_state
    runtime.get_streamvox_prompt_data.return_value = SimpleNamespace(
        prompt_text=['当前角色参考文字'], references=[SimpleNamespace(audio_sha256='sdk-audio-hash')])
    supervisor = Mock()
    supervisor.require_for_streamvox_management.return_value = runtime
    assets = SimpleNamespace(resolve_voice_reference_audio=lambda _: prepared_voice.voice_setup_path.parent / 'reference.wav',
                             read_voice_prompt_text=lambda _: '当前角色参考文字')
    service = CharacterVoiceService(hub=SimpleNamespace(character_card_assets=assets),
                                    assistant_process_supervisor=supervisor)
    service.status = Mock(return_value={'voice_match_status': 'matched'})
    settings = SimpleNamespace(execution_config_path=prepared_voice.execution_config_path,
                               voice_setup_path=prepared_voice.voice_setup_path)
    return service, card, settings, runtime, supervisor


def test_character_apply_rejects_unprepared_model_before_runtime(character_voice_application):
    """跨模型目标未确认时不能触发切换或失败补偿。"""
    service, card, settings, runtime, supervisor = character_voice_application
    card.meta.voice_binding.streamvox_model = 's2-pro-4b-gguf'
    with pytest.raises(StateConflict, match='不匹配'):
        service.apply(card, current_settings=settings)
    supervisor.require_for_streamvox_management.assert_not_called()
    runtime.switch_streamvox_config.assert_not_called()


def test_character_apply_rejects_changed_material_before_runtime(character_voice_application):
    """已确认模型相同也不能复用另一份参考文字。"""
    service, card, settings, runtime, supervisor = character_voice_application
    card.meta.voice_binding.prompt_text = '新的参考文字'
    with pytest.raises(StateConflict, match='素材或推理参数已改变'):
        service.apply(card, current_settings=settings)
    supervisor.require_for_streamvox_management.assert_not_called()
    runtime.create_streamvox_role.assert_not_called()


def test_character_apply_rejects_missing_role_without_rebuilding(character_voice_application):
    """缓存丢失必须回到配置流程，不能在正常助手内偷偷重建。"""
    service, card, settings, runtime, _ = character_voice_application
    runtime.get_streamvox_runtime_state.return_value['roles'] = []
    with pytest.raises(StateConflict, match='音色缓存缺失'):
        service.apply(card, current_settings=settings)
    runtime.switch_streamvox_config.assert_not_called()
    runtime.create_streamvox_role.assert_not_called()
    runtime.delete_streamvox_role.assert_not_called()


def test_character_apply_switches_directly_to_confirmed_role(character_voice_application):
    """已确认跨模型切换直接使用当前角色音色和独立后端。"""
    service, card, settings, runtime, _ = character_voice_application
    runtime.get_streamvox_runtime_state.return_value = {
        **runtime.get_streamvox_runtime_state.return_value, 'model': 's2-pro-4b-gguf', 'role_name': 'previous-role'}
    service.apply(card, current_settings=settings)
    runtime.switch_streamvox_config.assert_called_once_with(
        model=card.meta.voice_binding.streamvox_model, role_name='current-card-role', device='gpu',
        llama_backend='cpu', streamvox_seed=None, inference_options={}, timeout=30.0)
    runtime.create_streamvox_role.assert_not_called()


def test_character_apply_loads_new_voice_cache_for_same_model(character_voice_application):
    """同模型重新准备角色音色后，旧缓存里的同名角色不阻止加载新缓存。"""
    service, card, settings, runtime, _ = character_voice_application
    runtime.get_streamvox_runtime_state.return_value['voice_directory'] = 'previous-voice-cache'
    runtime.get_streamvox_prompt_data.return_value = None
    service.apply(card, current_settings=settings)
    runtime.switch_streamvox_config.assert_called_once()
    runtime.get_streamvox_prompt_data.assert_not_called()


def test_child_rejects_stale_material_before_allocating_llm(prepared_voice, monkeypatch):
    """子进程重读当前素材，确认失效时不得开始创建模型客户端。"""
    monkeypatch.setenv('NEARMATE_PACKAGED', '1')
    llm_factory = Mock()
    monkeypatch.setattr('companion.realtime.assistant_process.LlmClientFactory', llm_factory)
    config = SimpleNamespace(streamvox_execution_config_path=str(prepared_voice.execution_config_path),
        streamvox_voice_setup_path=str(prepared_voice.voice_setup_path), streamvox_model=prepared_voice.model,
        streamvox_role_name=prepared_voice.role_name,
        streamvox_reference_audio_path=str(prepared_voice.voice_setup_path.parent / 'reference.wav'),
        streamvox_prompt_text='变更后的参考文字', streamvox_seed=None, streamvox_inference_options={})
    with pytest.raises(RuntimeError, match='参考素材或推理参数已改变'):
        _build_assistant_child_runtime(config)
    llm_factory.assert_not_called()


def test_old_default_voice_http_entry_is_removed():
    """公开路由不再承诺创建默认演示音色。"""
    from companion.core.api.routers.streamvox import router

    assert '/api/streamvox/default-role/ensure' not in {route.path for route in router.routes}
