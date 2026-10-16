"""验证正式运行时与首次语音任务共用同一配置门禁。"""

from types import SimpleNamespace
from unittest.mock import Mock, AsyncMock

import pytest

from companion.core.application.runtime_reconfiguration import RuntimeReconfigurationCoordinator
from companion.core.settings import Settings
from companion.llm.config import LlmSettings


def coordinator(tmp_path):
    """建立能记录资源关闭顺序的协调器，模型进程由独立生命周期测试覆盖。"""

    settings = Settings(user_settings_path=str(tmp_path / 'settings.json'))
    settings.llm = LlmSettings.model_validate({'schema_version': 2,
        'credentials': {'deepseek': {'base_url': 'https://api.deepseek.com/v1', 'api_key': 'test'},
                        'qwen': {'base_url': 'https://dashscope.aliyuncs.com/compatible-mode/v1', 'api_key': 'test'}},
        'role_bindings': {'foreground': 'deepseek:deepseek-flash:chat', 'summary': 'deepseek:deepseek-flash:chat',
                         'cognitive': 'qwen:qwen3.6-plus:chat', 'character_authoring': None}})
    steps = []
    voice = SimpleNamespace(invalidate=lambda: steps.append('closed_gate'),
        start=Mock(side_effect=lambda model: steps.append('worker_started') or {'model_id': model}),
        catalog_payload=lambda: {'models': [{'model_id': 'model', 'download_available': True}]})
    hub = SimpleNamespace(bootstrap=SimpleNamespace(
                        status_payload=lambda: {'voice': {'is_ready': False}}))
    controller = RuntimeReconfigurationCoordinator(settings=settings, hub=hub,
        realtime_input_runtime_owner=SimpleNamespace(shutdown=lambda: steps.append('input_stopped')),
        assistant_process_supervisor=SimpleNamespace(detach_for_shutdown=lambda: None, shutdown=lambda: steps.append('assistant_stopped')),
        activity_service=object(), background_agent_interactions=object(), interrupt_runtime_activities=lambda epoch: [],
        release_runtime_memory_compaction=lambda epoch: False, restart_livekit_media_session=AsyncMock(),
        tts_setup=voice, stop_voice_setup_consumers=AsyncMock(side_effect=lambda: steps.append('media_stopped')))
    return controller, voice, steps


@pytest.mark.asyncio
async def test_worker_waits_for_all_formal_resources_to_exit(tmp_path):
    """关闭语音门禁后先停媒体，再释放输入与正式助手，最后才开始新模型任务。"""

    controller, voice, steps = coordinator(tmp_path)
    await controller.start_voice_setup(model_id='model')
    assert steps[0] == 'closed_gate'
    assert steps[-1] == 'worker_started'
    assert set(steps[1:-1]) == {'media_stopped', 'input_stopped', 'assistant_stopped'}


@pytest.mark.asyncio
async def test_failed_cleanup_blocks_worker_and_retry_finishes_cleanup(tmp_path):
    """关闭资源失败时保留关闭任务，修复后重试才能建立配置进程。"""

    controller, voice, steps = coordinator(tmp_path)
    failures = [RuntimeError('媒体仍在运行'), None]

    async def stop_media():
        """模拟第一次媒体资源未能关闭。"""
        error = failures.pop(0)
        if error:
            raise error

    controller.stop_voice_setup_consumers = stop_media
    with pytest.raises(RuntimeError, match='仍在运行'):
        await controller.start_voice_setup(model_id='model')
    voice.start.assert_not_called()
    assert controller._voice_cleanup_steps
    await controller.start_voice_setup(model_id='model')
    voice.start.assert_called_once_with('model')
    assert not controller._voice_cleanup_steps


@pytest.mark.asyncio
async def test_model_download_keeps_gate_and_formal_runtime_untouched(tmp_path):
    """纯文件下载只取得配置提交锁，不关闭门禁、媒体、输入或正式助手。"""

    controller, voice, steps = coordinator(tmp_path)
    voice.start_model_download = Mock(return_value={'phase': 'queued', 'task_id': 'download'})
    voice.retry_model_download = Mock(return_value={'phase': 'queued', 'task_id': 'retry'})
    assert await controller.start_voice_model_download(model_id='model') == {'phase': 'queued', 'task_id': 'download'}
    assert await controller.start_voice_model_download(retry_task_id='old') == {'phase': 'queued', 'task_id': 'retry'}
    assert steps == []
    voice.start.assert_not_called()
    voice.start_model_download.assert_called_once_with('model')
    voice.retry_model_download.assert_called_once_with('old')


@pytest.mark.asyncio
async def test_llm_save_does_not_build_runtime_before_voice_confirmation(tmp_path, monkeypatch):
    """LLM 已就绪仍不能越过尚未完成的语音步骤启动模型。"""

    controller, _, _ = coordinator(tmp_path)
    monkeypatch.setenv("NEARMATE_PACKAGED", "1")
    controller._build_runtime_replacement = Mock()
    assert await controller._prepare_runtime_replacement() is None
    controller._build_runtime_replacement.assert_not_called()
