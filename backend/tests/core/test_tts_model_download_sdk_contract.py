"""通过已安装的公开 SDK 下载器核对文件任务，不连接远端或加载大模型。"""

from __future__ import annotations

import json
from pathlib import Path
from threading import Event
from unittest.mock import Mock

import pytest

from companion.core.tts_setup_worker import download_pinned_model_files, run_tts_model_download_job
from companion.core.voice_setup_assets import inspect_model_installation, sha256_file


@pytest.fixture
def pinned_download_fixture(tmp_path, monkeypatch):
    """仅替换远端获取，保留已安装 SDK 的复制、摘要校验和原子发布。"""

    import streamvox
    import streamvox.pinned_download

    model_id = 'qwen3-tts-clone-0.6b-gguf'
    snapshot = tmp_path / 'remote-snapshot'
    snapshot.mkdir()
    weights = snapshot / 'ckpt' / 'weights.bin'
    weights.parent.mkdir()
    weights.write_bytes(b'model-file')
    manifest = snapshot / 'manifest.json'
    manifest.write_text(json.dumps({'model': model_id, 'sources': ['local'],
        'assets': [{'path': 'ckpt/weights.bin', 'sha256': sha256_file(weights)}]}), encoding='utf-8')
    pin = {'model_id': model_id, 'repository': 'ChengHee/qwen3-tts-clone-0.6b-gguf',
        'revision': 'a' * 40, 'manifest_sha256': sha256_file(manifest), 'size_bytes': weights.stat().st_size,
        'files': [{'path': 'ckpt/weights.bin', 'size_bytes': weights.stat().st_size, 'sha256': sha256_file(weights)}]}
    transport = Mock(return_value=snapshot)
    monkeypatch.setattr(streamvox.pinned_download, 'download_pinned_snapshot', transport)
    engine = Mock(side_effect=AssertionError('纯文件下载不得建立语音引擎'))
    monkeypatch.setattr(streamvox, 'TTSEngine', engine)
    return {'model': pin, 'model_directory': str(tmp_path / 'models')}, snapshot, transport, engine


def test_download_worker_uses_real_sdk_and_keeps_file_task_separate(pinned_download_fixture):
    """文件任务完成只上报 completed，真实 SDK 校验后留下可复用的发布目录。"""

    job, snapshot, transport, engine = pinned_download_fixture
    connection = Mock()
    run_tts_model_download_job(job, connection, Event())
    assert connection.send.call_args.args[0] == {'phase': 'completed'}
    assert connection.close.called
    transport.assert_called_once()
    assert transport.call_args.kwargs['revision'] == job['model']['revision']
    assert transport.call_args.kwargs['expected_manifest_sha256'] == job['model']['manifest_sha256']
    root = Path(job['model_directory'])
    assert (root / job['model']['model_id'] / 'ckpt/weights.bin').read_bytes() == (snapshot / 'ckpt/weights.bin').read_bytes()
    assert inspect_model_installation(root, job['model'])['local_state'] == 'downloaded'
    assert all(call.args[0]['phase'] not in {'load', 'prepare_voice', 'preview', 'awaiting_confirmation'}
               for call in connection.send.call_args_list)
    engine.assert_not_called()

    # 再次准备同一份固定模型应离线校验，不再访问远端获取入口。
    transport.reset_mock()
    assert download_pinned_model_files(job, Mock(), Event()) == (root / job['model']['model_id']).resolve()
    transport.assert_not_called()


def test_failed_digest_keeps_existing_published_model(pinned_download_fixture):
    """远端文件与固定清单不符时不能覆盖原发布目录，也不能误报下载完成。"""

    job, snapshot, _, engine = pinned_download_fixture
    directory = Path(job['model_directory']) / job['model']['model_id']
    directory.mkdir(parents=True)
    (directory / 'ckpt').mkdir()
    (directory / 'manifest.json').write_bytes(b'old-manifest')
    (directory / 'ckpt/weights.bin').write_bytes(b'old-model')
    (snapshot / 'ckpt/weights.bin').write_bytes(b'x' * job['model']['files'][0]['size_bytes'])
    connection = Mock()
    run_tts_model_download_job(job, connection, Event())
    assert connection.send.call_args.args[0]['phase'] == 'failed'
    assert 'sha256' in connection.send.call_args.args[0]['error']
    assert (directory / 'ckpt/weights.bin').read_bytes() == b'old-model'
    assert (directory / 'manifest.json').read_bytes() == b'old-manifest'
    engine.assert_not_called()
