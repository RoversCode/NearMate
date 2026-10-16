"""管理首次配置和模型切换的任务、持久状态与角色语音提交。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from importlib.metadata import version
import json
import multiprocessing
import os
from pathlib import Path
import shutil
import subprocess
from threading import RLock
import time
from typing import Callable
from uuid import uuid4

from companion.background_agent.config_commit_lock import BackgroundAgentConfigCommitLock
from companion.core.application.errors import StateConflict
from companion.core.execution_config import (
    ExecutionConfiguration,
    ExecutionSelection,
    atomic_write_json,
    read_execution_configuration,
)
from companion.core.execution_probe import probe_execution_capabilities
from companion.core.runtime_profile import normalize_execution_selection
from companion.core.settings import Settings
from companion.core.tts_setup_worker import run_tts_setup_job, run_tts_model_download_job
from companion.core.voice_caption_calibration import invalidate_changed_voice_caption_timing
from companion.core.voice_setup_assets import MODEL_NAMES, inspect_model_installation, read_model_catalog, sha256_file, validate_model_directory, voice_context


RUNNING_PHASES = frozenset({'queued', 'download', 'copy', 'verify', 'publish', 'load', 'prepare_voice', 'preview', 'release'})
RETRYABLE_PHASES = frozenset({'failed', 'cancelled', 'interrupted', 'repair_required'})
PREVIEW_TEXT = '我在这里，听见你说话了。'


class TtsSetupService:
    """持有唯一语音配置子进程，确认素材与执行方式后才发布语音绑定。"""

    def __init__(self, *, settings: Settings, active_card_provider: Callable, save_card: Callable,
                 worker_target: Callable = run_tts_setup_job,
                 capability_probe: Callable = probe_execution_capabilities,
                 download_worker_target: Callable = run_tts_model_download_job) -> None:
        """建立服务并恢复任务事实，启动时不加载 TTS 模型。

        Args:
            settings: 用户配置、模型与角色资源路径。
            active_card_provider: 获取当前角色卡。
            save_card: 持久保存角色卡语音绑定。
            worker_target: 独立进程入口，测试可替换为受控进程。
            capability_probe: 有超时限制的后端探测器。
            download_worker_target: 仅下载模型文件的进程入口，不准备角色音色。
        Returns:
            无返回值。
        """

        self.settings = settings
        self._active_card_provider = active_card_provider
        self._save_card = save_card
        self._worker_target = worker_target
        self._download_worker_target = download_worker_target
        self._capability_probe = capability_probe
        self._capabilities: dict | None = None
        self._lock = RLock()
        self._commit_lock = BackgroundAgentConfigCommitLock(settings.user_settings_path)
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='nearmate-voice-owner')
        self._process = None
        self._cancel_event = None
        self._future = None
        self._future_owner = None
        self._closing = False
        self._state_path = Path(settings.voice_setup_path)
        self._download_state_path = self._state_path.with_name(f'{self._state_path.stem}-model-download.json')
        self._configuration_path = Path(settings.execution_config_path)
        self._catalog = read_model_catalog(Path(settings.model_catalog_path))
        try:
            self._state = json.loads(self._state_path.read_text(encoding='utf-8'))
            if not isinstance(self._state, dict):
                raise ValueError('任务状态不是对象')
        except (OSError, ValueError):
            self._state = {'phase': 'unconfigured', 'is_ready': False}
        model_id = self._state.get('model_id')
        if model_id is not None and (not isinstance(model_id, str) or model_id not in MODEL_NAMES):
            self._state = {'phase': 'unconfigured', 'is_ready': False}
        elif self._state.get('phase') in RUNNING_PHASES:
            self._state.update(phase='interrupted', is_ready=False, error='上次语音配置被中断，可以重试并续传')
            self._persist()
        elif self._state.get('phase') in {'ready', 'awaiting_confirmation'} and self._state.get('schema_version') != 2:
            self._state.update(phase='repair_required', is_ready=False, error='旧语音配置需要重新确认执行方式和角色素材')
            self._persist()
        try:
            self._download_state = json.loads(self._download_state_path.read_text(encoding='utf-8'))
            if not isinstance(self._download_state, dict) or self._download_state.get('model_id') not in MODEL_NAMES:
                raise ValueError('模型下载状态无效')
        except (OSError, ValueError):
            self._download_state = {'phase': 'idle'}
        if self._download_state.get('phase') in RUNNING_PHASES:
            self._download_state.update(phase='interrupted', error='上次模型下载被中断，可以继续下载')
            self._persist_download()
        self._apply_configuration_to_settings()

    def configuration_payload(self) -> dict:
        """返回持久的执行选择与允许用户更换的模型根目录。"""

        saved = read_execution_configuration(self._configuration_path)
        selection = self._effective_selection(saved.selection) if saved else None
        return {'confirmed': saved is not None, 'selection': selection.model_dump() if selection else None,
                'model_directory': saved.model_directory if saved else self.settings.streamvox_model_dir,
                'capabilities': self._capabilities}

    def probe_capabilities(self) -> dict:
        """运行设备探测，失败记录仅描述可用性，不提交用户选择。"""

        result = self._capability_probe()
        with self._lock:
            self._capabilities = result
        return result

    def configure(self, configuration: ExecutionConfiguration) -> dict:
        """校验用户选择并持久保存，变更后必须重新准备试听。

        Args:
            configuration: 用户已选择的执行方式和模型目录。
        Returns:
            持久化后的执行配置。
        """

        with self._lock, self._commit_lock.hold():
            self._require_idle()
            if self._capabilities is None:
                raise StateConflict('请先检测本机执行方式')
            available = {(item['kind'], item['backend']) for item in self._capabilities['backends'] if item['available']}
            selection = configuration.selection
            if ('onnx', selection.onnx_provider) not in available or ('llama', selection.llama_backend) not in available:
                raise StateConflict('所选执行方式不可用，请查看检测原因并修复或重新选择')
            root = validate_model_directory(configuration.model_directory)
            configuration = configuration.model_copy(update={'model_directory': str(root)})
            previous = read_execution_configuration(self._configuration_path)
            if previous == configuration:
                return self.configuration_payload()
            atomic_write_json(self._configuration_path, configuration.model_dump())
            self._state = {'schema_version': 2, 'phase': 'unconfigured', 'is_ready': False,
                           'confirmed_context_fingerprint': self._state.get('confirmed_context_fingerprint')}
            self._persist()
            self._apply_configuration_to_settings()
            return self.configuration_payload()

    def catalog_payload(self) -> dict:
        """展示模型文件状态和当前选用关系，不把下载成功当成运行加载成功。

        Args:
            无。
        Returns:
            五个固定模型的本地文件状态，以及独立的模型下载任务快照。
        """

        models = []
        sdk_version = version('streamvox')
        with self._lock:
            configuration = read_execution_configuration(self._configuration_path)
            root = Path(configuration.model_directory) if configuration else None
            current_card = self._active_card_provider()
            current_model = None
            if self._state.get('confirmed_context_fingerprint'):
                current_model = current_card.meta.voice_binding.streamvox_model if current_card else self._state.get('model_id')
            for model_id, name in MODEL_NAMES.items():
                pin = self._catalog.get(model_id)
                verification = self._state if self._state.get('model_id') == model_id else self._download_state
                installation = inspect_model_installation(root, pin, verification) if pin else {
                    'local_state': 'unavailable', 'installed_size_bytes': None,
                    'total_size_bytes': None, 'integrity_status': 'not_verified'}
                models.append({'model_id': model_id, 'display_name': name, 'sdk_version': sdk_version,
                           'repository': pin['repository'] if pin else None,
                           'estimated_size_bytes': pin['size_bytes'] if pin else None,
                           'bundle_version': pin['revision'] if pin else None,
                           'manifest_sha256': pin['manifest_sha256'] if pin else None,
                           'download_available': pin is not None,
                           'verification_status': '数值与实机未验证' if pin else '发行模型清单缺失，请修复安装资源',
                           'backend_note': 'DirectML 模式下音频解码固定使用 CPU' if model_id == 'moss-tts-nano-onnx' else None,
                           'is_current': model_id == current_model, **installation})
            return {'provider': 'local_streamvox', 'models': models, 'configured_model': self._state.get('model_id'),
                    'download_task': self.download_status_payload()}

    def status_payload(self) -> dict:
        """返回完整进度与动作资格，只有配置进程释放后才公开可操作的终态。

        Args:
            无。
        Returns:
            不含参考素材路径的语音任务快照，保留真实错误与下载进度。
        """

        with self._lock:
            if self._state.get('phase') in {'ready', 'awaiting_confirmation'}:
                try:
                    self._verify_context()
                except (OSError, ValueError, TypeError, KeyError, StateConflict) as error:
                    self._state.update(phase='repair_required', is_ready=False, error=str(error))
                    self._persist()
            result = {key: self._state.get(key) for key in ('model_id', 'role_name', 'task_id', 'progress', 'error', 'actual_backends')}
            phase = self._state.get('phase', 'unconfigured')
            worker_running = self._future is not None and not self._future.done()
            running = self._future_owner == 'voice' and worker_running
            # 失败事实可能先于线程和原生进程释放落盘；这段窗口仍需轮询，不能卡在无法重试的终态。
            if running and phase in RETRYABLE_PHASES:
                phase = 'release'
            if isinstance(result['progress'], dict):
                # 旧版本校验事件只有文件名；在读取边界补齐计数字段，同时保留它真实所属的阶段。
                result['progress'] = {'phase': phase, 'completed_bytes': 0, 'total_bytes': None, **result['progress']}
            result.update(provider='local_streamvox', is_ready=phase == 'ready' and self._state.get('is_ready') is True, phase=phase,
                          can_cancel=running or phase == 'awaiting_confirmation',
                          can_retry=not worker_running and phase in RETRYABLE_PHASES and self._state.get('model_id') in MODEL_NAMES,
                          can_confirm=not worker_running and phase == 'awaiting_confirmation')
            # 切换模型会暂时撤销当前语音的运行资格，但保留这个事实让控制台区分首次配置和既有用户的语音重配置。
            result['has_confirmed_voice'] = isinstance(self._state.get('confirmed_context_fingerprint'), str) and bool(
                self._state.get('confirmed_context_fingerprint')
            )
            result['execution'] = self.configuration_payload()
            result['fingerprint'] = self._state.get('context', {}).get('fingerprint') if result['is_ready'] else None
            return result

    def invalidate(self) -> None:
        """在关闭正式助手前关闭语音门禁，保留已经下载的模型和缓存。"""

        with self._lock:
            self._require_idle()
            self._state.update(is_ready=False, phase='repair_required', error='请准备并确认当前角色语音')
            self._persist()

    def start(self, model_id: str) -> dict:
        """冻结角色素材后启动独立配置进程，正式助手的收口由调用方先完成。

        Args:
            model_id: 固定发行目录中的模型编号。
        Returns:
            唯一语音配置任务的当前快照。
        """

        with self._lock, self._commit_lock.hold():
            self._require_idle()
            configuration = read_execution_configuration(self._configuration_path)
            if configuration is None:
                raise StateConflict('请先确认执行方式和模型目录')
            model = self._catalog.get(model_id)
            if model is None:
                raise ValueError('模型不在已固定版本的发行目录中')
            # 已安装模型可能在几乎满盘时重新准备音色；由工作进程完成哈希核验后再判断下载空间。
            root = validate_model_directory(configuration.model_directory, 16 * 1024 * 1024)
            card = self._active_card_provider()
            configuration = configuration.model_copy(
                update={'selection': self._effective_selection(configuration.selection)}
            )
            context = voice_context(card, Path(self.settings.character_cards_dir), model, configuration)
            directory = self._state_path.parent / 'voices' / context['fingerprint']
            directory.mkdir(parents=True, exist_ok=True)
            source = Path(context['reference_audio'])
            snapshot = directory / f'reference{source.suffix}'
            shutil.copyfile(source, snapshot)
            if sha256_file(snapshot) != context['reference_audio_sha256']:
                raise StateConflict('参考音频在准备过程中发生变化，请重试')
            role_name = card.meta.voice_binding.role_name.strip() or card.meta.card_id
            job = {'model': model, 'model_directory': str(root), 'context': context,
                   'voice_directory': str(directory), 'reference_snapshot': str(snapshot),
                   'preview_path': str(directory / 'preview.wav'), 'preview_text': PREVIEW_TEXT, 'role_name': role_name}
            spawn = multiprocessing.get_context('spawn')
            receiver, sender = spawn.Pipe(duplex=False)
            self._cancel_event = spawn.Event()
            self._process = spawn.Process(target=self._worker_target, args=(job, sender, self._cancel_event), name='nearmate-voice-setup')
            confirmed_context_fingerprint = self._state.get('confirmed_context_fingerprint')
            self._state = {'schema_version': 2, 'provider': 'local_streamvox', 'is_ready': False, 'phase': 'queued',
                           'task_id': uuid4().hex, 'model_id': model_id, 'role_name': role_name, 'context': context,
                           'confirmed_context_fingerprint': confirmed_context_fingerprint,
                           'error': None, 'progress': {'phase': 'queued', 'completed_bytes': 0, 'total_bytes': None}}
            self._persist()
            try:
                self._process.start()
            except BaseException:
                receiver.close()
                sender.close()
                self._state.update(phase='failed', error='语音配置进程启动失败')
                self._persist()
                raise
            sender.close()
            self._future_owner = 'voice'
            self._future = self._executor.submit(self._monitor, self._process, receiver, self._cancel_event)
            return self.status_payload()

    def download_status_payload(self) -> dict:
        """返回仅下载模型的进度，完成后没有语音试听或绑定确认资格。

        Args:
            无。
        Returns:
            与语音任务形状兼容的文件下载状态。
        """

        with self._lock:
            state = self._download_state
            worker_running = self._future is not None and not self._future.done()
            running = self._future_owner == 'download' and worker_running
            phase = state.get('phase', 'idle')
            if running and phase in RETRYABLE_PHASES | {'completed'}:
                phase = 'release'
            progress = state.get('progress')
            if isinstance(progress, dict):
                progress = {'completed_bytes': 0, 'total_bytes': None, **progress, 'phase': phase}
            return {'provider': 'local_streamvox', 'is_ready': False, 'phase': phase,
                    'task_id': state.get('task_id'), 'model_id': state.get('model_id'), 'role_name': None,
                    'progress': progress, 'error': state.get('error'), 'can_confirm': False,
                    'can_cancel': running,
                    'can_retry': not worker_running and phase in RETRYABLE_PHASES and state.get('model_id') in MODEL_NAMES}

    def start_model_download(self, model_id: str) -> dict:
        """只下载未使用的模型，保留当前已确认语音和正式助手。

        Args:
            model_id: 发行固定的目标模型编号。
        Returns:
            新下载任务的编号、进度与动作资格。
        """

        with self._lock, self._commit_lock.hold():
            self._require_idle()
            configuration = read_execution_configuration(self._configuration_path)
            if configuration is None:
                raise StateConflict('请先确认执行方式和模型目录')
            model = self._catalog.get(model_id)
            if model is None:
                raise ValueError('模型不在已固定版本的发行目录中')
            if self.status_payload()['is_ready'] and model_id == self._state.get('model_id'):
                raise StateConflict('当前模型正在使用，无需重新下载；修复时请明确重新准备语音')
            root = validate_model_directory(configuration.model_directory, 16 * 1024 * 1024)
            job = {'model': model, 'model_directory': str(root)}
            spawn = multiprocessing.get_context('spawn')
            receiver, sender = spawn.Pipe(duplex=False)
            self._cancel_event = spawn.Event()
            self._process = spawn.Process(target=self._download_worker_target,
                args=(job, sender, self._cancel_event), name='nearmate-model-download')
            self._download_state = {'phase': 'queued', 'task_id': uuid4().hex, 'model_id': model_id,
                'model_directory': str(root), 'context': {'model_revision': model['revision'],
                'manifest_sha256': model['manifest_sha256']}, 'error': None,
                'progress': {'phase': 'queued', 'completed_bytes': 0, 'total_bytes': None}}
            self._persist_download()
            try:
                self._process.start()
            except BaseException:
                receiver.close()
                sender.close()
                self._download_state.update(phase='failed', error='模型下载进程启动失败')
                self._persist_download()
                raise
            sender.close()
            self._future_owner = 'download'
            self._future = self._executor.submit(self._monitor, self._process, receiver, self._cancel_event, download_only=True)
            return self.download_status_payload()

    def cancel_model_download(self, task_id: str) -> dict:
        """取消当前下载任务，保留 SDK 的已下载缓存。

        Args:
            task_id: 当前下载任务编号，拒绝旧页面取消新任务。
        Returns:
            取消后的权威进度；进程退出前仍显示资源释放阶段。
        """

        with self._lock:
            if self._download_state.get('task_id') != task_id or not self.download_status_payload()['can_cancel']:
                raise StateConflict('模型下载任务不存在或不可取消')
            self._cancel_event.set()
            return self.download_status_payload()

    def retry_model_download(self, task_id: str) -> dict:
        """从缓存继续失败下载，不影响当前语音绑定。

        Args:
            task_id: 当前可重试下载任务编号。
        Returns:
            新下载任务状态。
        """

        with self._lock:
            if self._download_state.get('task_id') != task_id or not self.download_status_payload()['can_retry']:
                raise StateConflict('模型下载任务不可重试或已过期')
            return self.start_model_download(self._download_state['model_id'])

    def cancel(self, task_id: str) -> dict:
        """取消当前任务或放弃未确认试听，下载缓存保留以便续传。"""

        with self._lock:
            self._require_task(task_id)
            if self._future_owner == 'voice' and self._cancel_event is not None:
                self._cancel_event.set()
            if self._state.get('phase') == 'awaiting_confirmation':
                self._state.update(phase='cancelled', is_ready=False, error='已放弃本次试听')
                self._persist()
            return self.status_payload()

    def retry(self, task_id: str) -> dict:
        """只允许失败或中断任务重试，重新冻结当前素材与执行配置。"""

        with self._lock:
            self._require_task(task_id)
            self._require_idle()
            if self._state.get('phase') not in RETRYABLE_PHASES:
                raise StateConflict('当前任务不可重试')
            model_id = self._state['model_id']
        return self.start(model_id)

    def preview_bytes(self, task_id: str) -> bytes:
        """读取与本次素材摘要匹配的试听文件。

        Args:
            task_id: 用户当前正在试听的配置任务编号。
        Returns:
            已验证为当前素材的 WAV 文件字节。
        """

        with self._lock:
            self._require_task(task_id)
            if self.status_payload()['phase'] != 'awaiting_confirmation':
                raise StateConflict('试听尚未准备完成或素材已经失效')
            return self._preview_path().read_bytes()

    def confirm(self, task_id: str) -> dict:
        """在相同角色、素材与执行方式下提交试听结果，不复用其他配置的缓存。

        Args:
            task_id: 用户实际试听并确认的当前任务编号。
        Returns:
            已提交语音绑定和确认事实的任务快照。
        """

        with self._lock, self._commit_lock.hold():
            self._require_idle()
            self._require_task(task_id)
            if self._state.get('phase') != 'awaiting_confirmation':
                raise StateConflict('请先等待试听生成并释放临时推理资源')
            self._verify_context()
            card = self._active_card_provider()
            binding = card.meta.voice_binding.model_copy(update={'streamvox_model': self._state['model_id'],
                'role_name': self._state['role_name'], 'audio_sha256': self._state['normalized_audio_sha256']})
            binding = invalidate_changed_voice_caption_timing(card.meta.voice_binding, binding)
            fingerprint = self._state['context']['fingerprint']
            # 相同绑定也可能换了原始素材、模型版本或执行方式；只有同一本机确认上下文可沿用旧语速。
            if self._state.get('confirmed_context_fingerprint') != fingerprint:
                binding = binding.model_copy(update={'caption_timing': None})
            self._save_card(card.model_copy(deep=True, update={'meta': card.meta.model_copy(update={'voice_binding': binding})}))
            self._state.update(phase='ready', is_ready=True, error=None, confirmed_context_fingerprint=fingerprint)
            self._persist()
            return self.status_payload()

    def shutdown(self) -> None:
        """停止配置进程及其子进程，等待监控收口后再释放线程池。"""

        with self._lock:
            self._closing = True
            if self._cancel_event is not None:
                self._cancel_event.set()
        self._executor.shutdown(wait=True, cancel_futures=True)

    def cancel_for_runtime_transition(self) -> None:
        """替换运行时前取消任务，下载缓存保留供下次配置使用。

        Returns:
            无返回值；正在运行的任务会由监控线程收口。
        """

        with self._lock:
            if self._cancel_event is not None:
                self._cancel_event.set()

    def _monitor(self, process, receiver, cancel_event, *, download_only: bool = False) -> None:
        """监控唯一工作进程，下载事实和语音绑定写入各自的持久状态。"""

        completion = None
        failure = None
        cancellation_at = None
        pipe_closed = False
        state = self._download_state if download_only else self._state
        persist = self._persist_download if download_only else self._persist
        try:
            while process.is_alive() or not pipe_closed:
                now = time.monotonic()
                if cancel_event.is_set():
                    cancellation_at = cancellation_at or now
                    if now - cancellation_at >= 2 and process.is_alive():
                        self._terminate_process_tree(process)
                if pipe_closed:
                    time.sleep(0.1)
                    continue
                try:
                    has_message = receiver.poll(0.1)
                except OSError as error:
                    if getattr(error, 'winerror', None) not in {109, 232}:
                        raise
                    # Windows 在发送端正常关闭后也会抛出管道结束；任务成败由结果与退出码决定。
                    pipe_closed = True
                    continue
                if has_message:
                    try:
                        message = receiver.recv()
                    except (EOFError, BrokenPipeError):
                        pipe_closed = True
                        continue
                    phase = message.get('phase')
                    if phase == ('completed' if download_only else 'awaiting_confirmation'):
                        completion = message
                        phase = 'release'
                    if phase in {'failed', 'cancelled'}:
                        failure = message.get('error') or '语音配置失败'
                        phase = 'release'
                    with self._lock:
                        state['phase'] = phase
                        # 任务状态只发布一种完整进度形状；非下载阶段用未知总量，不能沿用旧下载百分比。
                        state['progress'] = {'completed_bytes': 0, 'total_bytes': None,
                                                  **(message.get('progress') or {}), 'phase': phase}
                        if failure is not None:
                            state['error'] = failure
                        persist()
                elif not process.is_alive():
                    break
            process.join(timeout=3)
            if process.is_alive():
                self._terminate_process_tree(process)
                failure = failure or '配置进程未释放资源，请重试'
            with self._lock:
                if cancel_event.is_set() or self._closing:
                    state.update(phase='interrupted' if self._closing else 'cancelled', is_ready=False,
                                       error=failure or '语音配置已取消，下载缓存已保留')
                elif failure or completion is None or process.exitcode != 0:
                    state.update(phase='failed', is_ready=False, error=failure or f'配置进程异常退出：{process.exitcode}')
                else:
                    if download_only:
                        state.update(phase='completed', is_ready=False, error=None)
                    else:
                        state.update(phase='awaiting_confirmation', is_ready=False, error=None,
                                       actual_backends=completion['actual_backends'], preview_sha256=completion['preview_sha256'],
                                       normalized_audio_sha256=completion['normalized_audio_sha256'])
                    self._freeze_model_files(state)
                persist()
        except Exception as error:
            with self._lock:
                state.update(phase='failed', is_ready=False, error=str(error))
                persist()
        finally:
            if process.is_alive():
                self._terminate_process_tree(process)
            receiver.close()
            process.close()

    def _terminate_process_tree(self, process) -> None:
        """只终止当前服务创建的进程树，避免 SDK 的派生解码进程遗留。"""

        if os.name == 'nt' and process.pid is not None:
            taskkill = Path(os.environ.get('SystemRoot', 'C:/Windows')) / 'System32' / 'taskkill.exe'
            subprocess.run([str(taskkill), '/PID', str(process.pid), '/T', '/F'], capture_output=True, timeout=10, check=False,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        elif process.is_alive():
            process.terminate()
        process.join(timeout=5)
        if process.is_alive():
            raise RuntimeError('语音配置进程未能退出，请关闭 NearMate 后重试')

    def _verify_context(self) -> None:
        """发现素材、模型文件或执行选择变化时拒绝旧试听与语音就绪状态。"""

        configuration = read_execution_configuration(self._configuration_path)
        model = self._catalog.get(self._state.get('model_id'))
        if configuration is None or model is None or not isinstance(self._state.get('context'), dict):
            raise StateConflict('模型或执行配置失效，请重新准备语音')
        card = self._active_card_provider()
        binding = card.meta.voice_binding
        if self._state.get('phase') == 'ready' and (
            binding.streamvox_model != self._state['model_id'] or binding.role_name != self._state['role_name']
            or binding.audio_sha256 != self._state.get('normalized_audio_sha256')
        ):
            raise StateConflict('当前角色语音绑定已变化，请重新准备并确认语音')
        cards_root = Path(self.settings.character_cards_dir)
        current = voice_context(card, cards_root, model, configuration)
        current_fingerprints = {current['fingerprint']}
        effective_selection = self._effective_selection(configuration.selection)
        if effective_selection != configuration.selection:
            # 升级发行档位时，旧确认指纹仍记录原始 CUDA 选择；同时接受迁移前后的指纹，避免误伤已经确认的音色。
            migrated = voice_context(
                card,
                cards_root,
                model,
                configuration.model_copy(update={'selection': effective_selection}),
            )
            current_fingerprints.add(migrated['fingerprint'])
        if self._state['context'].get('fingerprint') not in current_fingerprints:
            raise StateConflict('角色素材或执行方式已变化，请重新准备语音')
        if sha256_file(self._preview_path()) != self._state.get('preview_sha256'):
            raise StateConflict('试听文件已损坏，请重新准备语音')
        model_root = Path(configuration.model_directory) / model['model_id']
        if sha256_file(model_root / 'manifest.json') != model['manifest_sha256']:
            raise StateConflict('本地模型清单已变化，请修复模型')
        inventory = self._state.get('model_files', [])
        if {item['path'] for item in inventory} != {item['path'] for item in model['files']}:
            raise StateConflict('模型文件记录不完整，请重新校验并准备语音')
        if not self._state.get('normalized_audio_sha256'):
            raise StateConflict('语音缓存缺少音频摘要，请重新准备语音')
        for item in inventory:
            path = (model_root / item['path']).resolve()
            if not path.is_relative_to(model_root.resolve()):
                raise StateConflict('模型文件路径无效')
            stat = path.stat()
            if stat.st_size != item['size_bytes'] or stat.st_mtime_ns != item['mtime_ns']:
                raise StateConflict('本地模型文件已变化，请重新校验并准备语音')

    def _freeze_model_files(self, state: dict) -> None:
        """保存本次已由 SDK 校验文件的大小与修改时间，用于轻量就绪巡检。"""

        configuration = read_execution_configuration(self._configuration_path)
        if configuration is None:
            raise StateConflict('执行配置已失效')
        model = self._catalog[state['model_id']]
        root = (Path(configuration.model_directory) / model['model_id']).resolve()
        files = []
        for item in model['files']:
            path = (root / item['path']).resolve()
            if not path.is_relative_to(root):
                raise StateConflict('模型清单路径越界')
            stat = path.stat()
            if stat.st_size != item['size_bytes']:
                raise StateConflict('模型文件大小与固定清单不符')
            files.append({'path': item['path'], 'size_bytes': stat.st_size, 'mtime_ns': stat.st_mtime_ns})
        state['model_files'] = files
        state['verified_model_directory'] = str(root)

    def _preview_path(self) -> Path:
        """只从合法上下文摘要计算试听路径，不信任持久文件提供的任意路径。"""

        fingerprint = self._state.get('context', {}).get('fingerprint', '')
        if not isinstance(fingerprint, str) or len(fingerprint) != 64 or any(character not in '0123456789abcdef' for character in fingerprint):
            raise StateConflict('语音缓存标识无效，请重新准备语音')
        return self._state_path.parent / 'voices' / fingerprint / 'preview.wav'

    def _require_task(self, task_id: str) -> None:
        """拒绝过期页面对另一项任务发起操作。"""

        if not task_id or self._state.get('task_id') != task_id:
            raise StateConflict('语音配置任务不存在或已过期')

    def _require_idle(self) -> None:
        """资源仍在使用时不允许改目录、启动新任务或提交试听。"""

        if self._closing or (self._future is not None and not self._future.done()):
            raise StateConflict('语音配置任务仍在运行或释放资源，请稍后再试')

    def _apply_configuration_to_settings(self) -> None:
        """让正式助手沿用已经确认的模型根目录和独立执行方式。"""

        saved = read_execution_configuration(self._configuration_path)
        if saved is not None:
            self.settings.streamvox_model_dir = saved.model_directory
            selection = self._effective_selection(saved.selection)
            self.settings.streamvox_device = 'cpu' if selection.onnx_provider == 'cpu' else 'gpu'
            self.settings.streamvox_llama_backend = selection.llama_backend

    @staticmethod
    def _effective_selection(selection: ExecutionSelection) -> ExecutionSelection:
        """返回当前安装 profile 可以实际加载的执行选择。"""

        if os.environ.get('NEARMATE_PACKAGED') != '1' and not os.environ.get('NEARMATE_RUNTIME_PROFILE'):
            return selection
        onnx_provider, llama_backend = normalize_execution_selection(
            selection.onnx_provider,
            selection.llama_backend,
        )
        return selection.model_copy(update={
            'onnx_provider': onnx_provider,
            'llama_backend': llama_backend,
        })

    def _persist(self) -> None:
        """提交当前任务快照，历史下载缓存由 SDK 单独管理。"""

        atomic_write_json(self._state_path, self._state)

    def _persist_download(self) -> None:
        """独立提交模型下载进度，不覆盖当前角色已确认的语音事实。"""

        atomic_write_json(self._download_state_path, self._download_state)
