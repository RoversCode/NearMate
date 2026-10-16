"""验证独立配置进程的提交、恢复与资源收口，不下载真实模型。"""

import json
from functools import partial
import os
import time
import wave
from pathlib import Path
from threading import Timer

import pytest

from companion.character_cards.models import CharacterCardDefinition, MetaSection, VoiceBindingSection, VoiceCaptionTimingSection
from companion.core.application.errors import StateConflict
from companion.core.execution_config import ExecutionConfiguration
from companion.core.settings import Settings
from companion.core.tts_setup_service import TtsSetupService
from companion.core.voice_setup_assets import sha256_file, published_model_matches, model_download_space_required, inspect_model_installation


def finish_preview(job, connection, cancel_event):
    """模拟隔离进程生成结果；保存与发行目录一致的微型模型文件。"""

    root = Path(job['model_directory']) / job['model']['model_id']
    root.mkdir(parents=True, exist_ok=True)
    (root / 'manifest.json').write_bytes(b'{}')
    (root / 'weights.data').write_bytes(b'model-external-data')
    with wave.open(job['preview_path'], 'wb') as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(24000)
        audio.writeframes(b'\0\0' * 100)
    connection.send({'phase': 'awaiting_confirmation', 'actual_backends': {'llama': 'cpu', 'onnx': 'cpu'},
                     'preview_sha256': sha256_file(Path(job['preview_path'])), 'normalized_audio_sha256': 'c' * 64})
    connection.close()


def unresponsive_worker(job, connection, cancel_event):
    """模拟原生推理卡住，证明父进程能强制关闭自己拥有的进程。"""

    connection.send({'phase': 'load'})
    time.sleep(30)


def finish_preview_after_download_progress(job, connection, cancel_event):
    """用真实子进程上报下载进度，再生成可确认的微型试听结果。

    Args:
        job: 父进程冻结的语音任务。
        connection: 向父进程返回进度与结果的管道。
        cancel_event: 父进程拥有的取消事件。

    Returns:
        无；结果通过管道返回。
    """
    connection.send({'phase': 'download', 'progress': {'phase': 'download', 'completed_bytes': 1, 'total_bytes': 2}})
    finish_preview(job, connection, cancel_event)


def report_failure_before_process_release(job, connection, cancel_event, *, terminal_phase):
    """模拟本地校验后失败，结果先于进程资源释放到达父进程。"""

    connection.send({'phase': 'verify', 'progress': {'phase': 'verify', 'current_file': 'weights.data'}})
    if terminal_phase == 'cancelled':
        cancel_event.set()
    connection.send({'phase': terminal_phase, 'error': '测试语音组件失败'})
    time.sleep(0.1)
    connection.close()


@pytest.fixture
def voice_fixture(tmp_path):
    """建立隔离角色、固定模型清单和可成功探测的 CPU 配置。"""

    settings = Settings(bootstrap_db_path=str(tmp_path / 'bootstrap.sqlite3'),
        user_settings_path=str(tmp_path / 'settings.json'), character_cards_dir=str(tmp_path / 'cards'),
        streamvox_model_dir=str(tmp_path / 'models'), voice_setup_path=str(tmp_path / 'voice-setup.json'),
        execution_config_path=str(tmp_path / 'execution-config.json'), model_catalog_path=str(tmp_path / 'catalog.json'))
    audio = tmp_path / 'cards' / 'test-card' / 'voice' / 'reference.wav'
    audio.parent.mkdir(parents=True)
    audio.write_bytes(b'reference-file')
    card = CharacterCardDefinition.model_construct(meta=MetaSection(card_id='test-card', name='测试', title='测试',
        voice_binding=VoiceBindingSection(reference_audio_path='voice/reference.wav', prompt_text='参考文字', role_name='test-role')))
    current = [card]
    manifest = tmp_path / 'manifest-source.json'
    weights = tmp_path / 'weights-source.data'
    manifest.write_bytes(b'{}')
    weights.write_bytes(b'model-external-data')
    pin = {'model_id': 'qwen3-tts-clone-0.6b-gguf', 'repository': 'ChengHee/test', 'revision': 'a' * 40,
           'manifest_sha256': sha256_file(manifest), 'size_bytes': 21,
           'files': [{'path': 'weights.data', 'size_bytes': weights.stat().st_size, 'sha256': sha256_file(weights)}]}
    Path(settings.model_catalog_path).write_text(json.dumps({'schema_version': 1, 'models': [pin]}))
    capabilities = {'backends': [{'kind': 'onnx', 'backend': 'cpu', 'available': True},
        {'kind': 'llama', 'backend': 'cpu', 'available': True}], 'recommendation': {'onnx_provider': 'cpu', 'llama_backend': 'cpu'}}
    services = []

    def create(worker=finish_preview):
        """使用真正派生进程和隔离状态构造服务。"""

        service = TtsSetupService(settings=settings, active_card_provider=lambda: current[0],
            save_card=lambda value: current.__setitem__(0, value), worker_target=worker,
            capability_probe=lambda: capabilities)
        services.append(service)
        return service

    yield settings, pin, current, audio, create
    for service in services:
        service.shutdown()


def configure(service, settings):
    """明确确认 CPU 执行方式，不依赖本机自动选择。"""

    service.probe_capabilities()
    service.configure(ExecutionConfiguration(selection={'onnx_provider': 'cpu', 'llama_backend': 'cpu'},
                                            model_directory=settings.streamvox_model_dir))


def wait_for(service, expected):
    """等待进程终态，超时带上实际状态以便定位生命周期失败。"""

    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        status = service.status_payload()
        if status['phase'] == expected and (expected != 'awaiting_confirmation' or status['can_confirm']):
            return status
        time.sleep(0.05)
    raise AssertionError(service.status_payload())


def test_finished_preview_survives_restart_and_commits_sdk_audio_hash(voice_fixture):
    """重启后继续试听确认，卡上保存规范音频摘要而非原始文件摘要。"""

    settings, pin, current, audio, create = voice_fixture
    service = create()
    configure(service, settings)
    task = service.start(pin['model_id'])
    status = wait_for(service, 'awaiting_confirmation')
    assert status['has_confirmed_voice'] is False
    assert status['actual_backends'] == {'llama': 'cpu', 'onnx': 'cpu'}
    service.shutdown()
    restored = create()
    assert restored.status_payload()['can_confirm']
    assert restored.preview_bytes(task['task_id']).startswith(b'RIFF')
    restored.confirm(task['task_id'])
    assert restored.status_payload()['is_ready']
    assert restored.status_payload()['has_confirmed_voice'] is True
    assert current[0].meta.voice_binding.audio_sha256 == 'c' * 64
    assert current[0].meta.voice_binding.audio_sha256 != sha256_file(audio)
    current[0].meta.voice_binding.streamvox_model = 's2-pro-4b-gguf'
    assert restored.status_payload()['phase'] == 'repair_required'


@pytest.mark.skipif(os.name != 'nt', reason='只有 Windows 普通读取句柄会阻止目标文件替换')
def test_download_progress_survives_temporary_state_reader(voice_fixture, monkeypatch):
    """状态文件被短暂读取时，任务应等到句柄释放后继续准备试听。

    Args:
        voice_fixture: 使用真实语音配置子进程的隔离环境。
        monkeypatch: 在进度落盘前准确安排一次 Windows 文件占用。

    Returns:
        无；断言任务完成且最终状态已持久保存。
    """
    settings, pin, _, _, create = voice_fixture
    service = create(finish_preview_after_download_progress)
    configure(service, settings)
    state_path = Path(settings.voice_setup_path)
    original_replace = os.replace
    readers = []
    release_timers = []

    def replace_with_temporary_reader(source, destination):
        """模拟另一读取方恰好在下载进度落盘时持有旧快照。"""
        if Path(destination) != state_path or service._state.get('phase') != 'download' or readers:
            return original_replace(source, destination)
        # 写完临时文件才开始占用，并等第一次真实替换结束后再计时，慢磁盘也能稳定重现冲突。
        reader = state_path.open('r', encoding='utf-8')
        readers.append(reader)
        try:
            return original_replace(source, destination)
        finally:
            release = Timer(0.05, reader.close)
            release_timers.append(release)
            release.start()

    monkeypatch.setattr(os, 'replace', replace_with_temporary_reader)
    try:
        service.start(pin['model_id'])
        service._future.result(timeout=12)
        status = service.status_payload()
        assert readers, '工作进程必须真实上报下载进度并触发读取冲突'
        assert status['phase'] == 'awaiting_confirmation'
        assert status['can_confirm']
        assert json.loads(state_path.read_text(encoding='utf-8'))['phase'] == 'awaiting_confirmation'
    finally:
        for release in release_timers:
            release.join()
        for reader in readers:
            reader.close()


@pytest.mark.parametrize('terminal_phase', ['failed', 'cancelled'])
def test_sparse_progress_and_failure_remain_pollable_until_process_release(voice_fixture, monkeypatch, terminal_phase):
    """缺少字节数的校验事件补齐契约，失败释放期间不能提前展示不可操作的终态。"""

    settings, pin, _, _, create = voice_fixture
    service = create(partial(report_failure_before_process_release, terminal_phase=terminal_phase))
    configure(service, settings)
    snapshots = []
    persist = service._persist

    def capture_persisted_snapshot():
        """在真实任务状态落盘边界观察 HTTP 将拿到的同一份快照。"""
        persist()
        snapshots.append(service.status_payload())

    monkeypatch.setattr(service, '_persist', capture_persisted_snapshot)
    service.start(pin['model_id'])
    service._future.result(timeout=12)
    verification = next(item for item in snapshots if item['phase'] == 'verify')
    assert verification['progress'] == {'phase': 'verify', 'current_file': 'weights.data', 'completed_bytes': 0, 'total_bytes': None}
    releasing = [item for item in snapshots if item['error'] == '测试语音组件失败']
    assert releasing and all(item['phase'] == 'release' and not item['can_retry'] for item in releasing)
    status = service.status_payload()
    assert status['phase'] == terminal_phase
    assert status['error'] == '测试语音组件失败'
    assert status['can_retry']
    assert not status['can_cancel']


@pytest.mark.parametrize('mutation', ['reference', 'external_weights', 'role_name', 'inventory'])
def test_stale_assets_cannot_be_confirmed(voice_fixture, mutation):
    """原始素材、外部权重、音色身份或校验记录改变后拒绝旧试听。"""

    settings, pin, current, audio, create = voice_fixture
    service = create()
    configure(service, settings)
    task = service.start(pin['model_id'])
    wait_for(service, 'awaiting_confirmation')
    if mutation == 'reference':
        audio.write_bytes(b'changed-reference')
    elif mutation == 'external_weights':
        (Path(settings.streamvox_model_dir) / pin['model_id'] / 'weights.data').write_bytes(b'broken')
    elif mutation == 'role_name':
        current[0].meta.voice_binding.role_name = 'another-role'
    else:
        service._state['model_files'] = []
    with pytest.raises((ValueError, StateConflict)):
        service.confirm(task['task_id'])
    assert service.status_payload()['phase'] == 'repair_required'


def test_cancel_terminates_unresponsive_owned_process(voice_fixture):
    """取消不能只改界面状态，卡住的推理进程必须退出才允许重试。"""

    settings, pin, _, _, create = voice_fixture
    service = create(unresponsive_worker)
    configure(service, settings)
    task = service.start(pin['model_id'])
    wait_for(service, 'load')
    with pytest.raises(StateConflict):
        service.configure(ExecutionConfiguration(selection={'onnx_provider': 'cpu', 'llama_backend': 'cpu'},
                                                 model_directory=settings.streamvox_model_dir))
    service.cancel(task['task_id'])
    status = wait_for(service, 'cancelled')
    service.shutdown()
    assert not status['is_ready']
    assert service._future.done()




def test_complete_model_reuses_disk_and_detects_same_size_corruption(voice_fixture):
    """已发布文件可离线复用；同大小篡改也必须由摘要检查发现。"""

    settings, pin, _, _, create = voice_fixture
    service = create()
    configure(service, settings)
    service.start(pin['model_id'])
    wait_for(service, 'awaiting_confirmation')
    root = Path(settings.streamvox_model_dir)
    assert published_model_matches(root, pin, lambda: None, lambda name: None)
    target = root / pin['model_id'] / 'weights.data'
    stat = target.stat()
    target.write_bytes(b'x' * target.stat().st_size)
    os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    assert not published_model_matches(root, pin, lambda: None, lambda name: None)
    inventory = inspect_model_installation(root, pin, service._state)
    assert inventory['local_state'] == 'downloaded'
    assert inventory['integrity_status'] == 'not_verified'
    cache = root / '.streamvox-download-cache' / pin['model_id'] / pin['revision'] / pin['manifest_sha256']
    cache.mkdir(parents=True)
    before = model_download_space_required(root, pin)
    (cache / 'weights.data.part').write_bytes(b'part')
    assert model_download_space_required(root, pin) == before - 4


def test_catalog_reports_downloaded_and_missing_models_from_actual_files(voice_fixture):
    """目录按真实文件展示下载状态，不把角色试听状态当成所有模型的下载状态。"""

    settings, pin, _, _, create = voice_fixture
    service = create()
    configure(service, settings)
    model = next(item for item in service.catalog_payload()['models'] if item['model_id'] == pin['model_id'])
    assert model['local_state'] == 'not_downloaded'
    directory = Path(settings.streamvox_model_dir) / pin['model_id']
    directory.mkdir()
    (directory / 'manifest.json').write_bytes(b'{}')
    model = next(item for item in service.catalog_payload()['models'] if item['model_id'] == pin['model_id'])
    assert model['local_state'] == 'incomplete'
    (directory / 'weights.data').write_bytes(b'model-external-data')
    model = next(item for item in service.catalog_payload()['models'] if item['model_id'] == pin['model_id'])
    assert model['local_state'] == 'downloaded'
    assert model['integrity_status'] == 'not_verified'
    assert model['installed_size_bytes'] == len(b'model-external-data')
    assert not model['is_current']
    assert service.catalog_payload()['configured_model'] is None
    (directory / 'weights.data').write_bytes(b'broken')
    assert next(item for item in service.catalog_payload()['models'] if item['model_id'] == pin['model_id'])['local_state'] == 'incomplete'


def finish_model_download(job, connection, cancel_event):
    """只发布微型模型文件，不创建语音、试听或角色绑定。"""

    directory = Path(job['model_directory']) / job['model']['model_id']
    directory.mkdir(parents=True, exist_ok=True)
    (directory / 'manifest.json').write_bytes(b'{}')
    (directory / 'weights.data').write_bytes(b'model-external-data')
    connection.send({'phase': 'completed'})
    connection.close()


def test_downloading_another_model_keeps_confirmed_voice_and_bootstrap_ready(voice_fixture):
    """只下载其他模型必须保留已确认语音，重启后也能继续使用当前角色。"""

    settings, pin, current, _, create = voice_fixture
    service = create()
    configure(service, settings)
    task = service.start(pin['model_id'])
    wait_for(service, 'awaiting_confirmation')
    service.confirm(task['task_id'])
    original_state = Path(settings.voice_setup_path).read_bytes()
    original_binding = current[0].meta.voice_binding.model_copy(deep=True)
    another_pin = {**pin, 'model_id': 's2-pro-4b-gguf'}
    service._catalog[another_pin['model_id']] = another_pin
    service._download_worker_target = finish_model_download

    download = service.start_model_download(another_pin['model_id'])
    assert service.status_payload()['is_ready']
    assert service.status_payload()['has_confirmed_voice'] is True
    assert service.status_payload()['model_id'] == pin['model_id']
    service._future.result(timeout=12)
    assert service.download_status_payload()['phase'] == 'completed'
    assert service.download_status_payload()['task_id'] == download['task_id']
    assert not service.download_status_payload()['can_confirm']
    assert service.status_payload()['is_ready']
    assert current[0].meta.voice_binding == original_binding
    assert Path(settings.voice_setup_path).read_bytes() == original_state
    model = next(item for item in service.catalog_payload()['models'] if item['model_id'] == another_pin['model_id'])
    assert model['local_state'] == 'downloaded'
    assert model['integrity_status'] == 'verified'
    assert not model['is_current']
    with pytest.raises(StateConflict):
        service.confirm(download['task_id'])
    Path(settings.model_catalog_path).write_text(json.dumps({'schema_version': 1, 'models': [pin, another_pin]}))
    service.shutdown()
    restored = create()
    assert restored.status_payload()['is_ready']
    assert restored.download_status_payload()['phase'] == 'completed'


def test_inventory_read_errors_stay_unknown_instead_of_missing(voice_fixture, monkeypatch):
    """没有目录读取权限时提示未知，不把读取失败猜成未下载。"""

    settings, pin, _, _, _ = voice_fixture
    root = Path(settings.streamvox_model_dir)
    directory = root / pin['model_id']
    directory.mkdir(parents=True)
    original_stat = Path.stat

    def denied_stat(path, *args, **kwargs):
        """只阻断目标模型目录的真实文件状态读取。"""

        if path == directory:
            raise PermissionError('测试读取权限被拒绝')
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'stat', denied_stat)
    assert inspect_model_installation(root, pin)['local_state'] == 'unknown'


def test_download_cancel_and_expiry_do_not_change_ready_voice(voice_fixture):
    """用户取消或运行时替换只结束下载进程，不覆盖原已确认语音事实。"""

    settings, pin, current, _, create = voice_fixture
    service = create()
    configure(service, settings)
    task = service.start(pin['model_id'])
    wait_for(service, 'awaiting_confirmation')
    service.confirm(task['task_id'])
    saved_voice = Path(settings.voice_setup_path).read_bytes()
    service._catalog['s2-pro-4b-gguf'] = {**pin, 'model_id': 's2-pro-4b-gguf'}
    service._download_worker_target = unresponsive_worker

    for cancel_reason in ('user', 'runtime_transition'):
        download = service.start_model_download('s2-pro-4b-gguf')
        assert service.status_payload()['is_ready']
        assert not service.status_payload()['can_cancel']
        with pytest.raises(StateConflict, match='运行或释放'):
            service.start(pin['model_id'])
        if cancel_reason == 'user':
            with pytest.raises(StateConflict):
                service.cancel_model_download('stale')
            service.cancel_model_download(download['task_id'])
        else:
            service.cancel_for_runtime_transition()
        service._future.result(timeout=12)
        assert service.download_status_payload()['phase'] == 'cancelled'
        assert service.download_status_payload()['can_retry']
        assert service.status_payload()['is_ready']
        assert current[0].meta.voice_binding.streamvox_model == pin['model_id']
        assert Path(settings.voice_setup_path).read_bytes() == saved_voice


@pytest.mark.parametrize('mutation', ['same', 'model_directory', 'reference', 'prompt', 'seed', 'options', 'model_revision', 'execution'])
def test_caption_calibration_only_survives_reconfirmation_of_same_voice(voice_fixture, mutation):
    """相同语音重新准备保留校准，素材、推理参数、模型版本或执行方式变化会清除旧语速。

    Args:
        voice_fixture: 具有真实配置子进程的隔离语音环境。
        mutation: 首次确认后发生的一种上下文变化。

    Returns:
        无；检查角色卡上的校准是否仍可使用。
    """
    settings, pin, current, audio, create = voice_fixture
    service = create()
    configure(service, settings)
    first_task = service.start(pin['model_id'])
    wait_for(service, 'awaiting_confirmation')
    service.confirm(first_task['task_id'])
    calibration = VoiceCaptionTimingSection(ms_per_unit=200, calibration_text='校准文字',
        duration_ms=1000, unit_count=5, measured_at=100)
    current[0].meta.voice_binding.caption_timing = calibration
    if mutation == 'model_directory':
        service.configure(ExecutionConfiguration(selection={'onnx_provider': 'cpu', 'llama_backend': 'cpu'},
            model_directory=str(Path(settings.streamvox_model_dir).with_name('moved-models'))))
    elif mutation == 'reference':
        audio.write_bytes(b'new-reference-audio')
    elif mutation == 'prompt':
        current[0].meta.voice_binding.prompt_text = '新的参考文字'
    elif mutation == 'seed':
        current[0].meta.voice_binding.seed = 7
    elif mutation == 'options':
        current[0].meta.voice_binding.inference_options = {'speed': 1.2}
    elif mutation == 'model_revision':
        service._catalog[pin['model_id']]['revision'] = 'b' * 40
        Path(settings.model_catalog_path).write_text(json.dumps({'schema_version': 1,
            'models': list(service._catalog.values())}), encoding='utf-8')
    elif mutation == 'execution':
        service._capabilities['backends'].append({'kind': 'onnx', 'backend': 'directml', 'available': True})
        service.configure(ExecutionConfiguration(selection={'onnx_provider': 'directml', 'llama_backend': 'cpu'},
            model_directory=settings.streamvox_model_dir))
    # 正式入口在启动准备前关闭门禁，单纯关闭门禁不应抹掉仍有效的校准身份。
    service.invalidate()
    second_task = service.start(pin['model_id'])
    wait_for(service, 'awaiting_confirmation')
    service.shutdown()
    restored = create()
    restored.confirm(second_task['task_id'])
    assert current[0].meta.voice_binding.caption_timing == (calibration if mutation in {'same', 'model_directory'} else None)


def test_first_confirmation_discards_seed_caption_calibration(voice_fixture):
    """没有本机已确认上下文的种子校准不能跨入首次确认。

    Args:
        voice_fixture: 隔离语音环境。

    Returns:
        无；首次确认后不再携带旧模型校准。
    """
    settings, pin, current, _, create = voice_fixture
    current[0].meta.voice_binding.caption_timing = VoiceCaptionTimingSection(
        ms_per_unit=174.194, calibration_text='旧校准', duration_ms=8640, unit_count=49.6, measured_at=100)
    service = create()
    configure(service, settings)
    task = service.start(pin['model_id'])
    wait_for(service, 'awaiting_confirmation')
    service.confirm(task['task_id'])
    assert current[0].meta.voice_binding.caption_timing is None
