"""用已安装 SDK 的真实流式出口核对试听与正式语音的参数和字节。"""

import hashlib
from io import BytesIO
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock
import wave

import numpy as np
import pytest

from companion.audio.providers.tts_provider import StreamVoxProviderConfig, StreamVoxTtsProvider
from companion.core.tts_setup_worker import run_tts_setup_job


def sdk_engine_with_test_adapter(model: str, recorded: list[dict]):
    """只替换模型计算，保留已安装 SDK 的真实 stream 方法。

    Args:
        model: 本次测试的模型标识。
        recorded: 收集合成适配器实际收到的参数。
    Returns:
        不加载大模型、通过真实 SDK 输出浮点波形的引擎。
    """

    from streamvox import TTSEngine

    engine = TTSEngine.__new__(TTSEngine)
    engine.prompt_cache = SimpleNamespace(roles={'current-card-role': object()}, load=lambda *args: object())
    engine.model = model
    engine.runtime = SimpleNamespace(sample_rate=24_000, gguf_backend='cpu', onnx_backend='cpu')
    engine.llama_backend = 'cpu'
    engine.onnx_provider = 'cpu'

    def synthesize(_runtime, _text, _prompt, **kwargs):
        """记录真实 stream 下沉的参数，输出覆盖正负幅度与裁剪边界的波形。"""
        recorded.append(kwargs)
        yield np.array([-2.0, -1.0, -0.25, 0.0, 0.25, 1.0, 2.0], dtype=np.float32)

    engine.adapter = SimpleNamespace(normalize_synthesize_kwargs=lambda **kwargs: kwargs, synthesize=synthesize)
    engine.make_prompt = Mock()
    engine.del_roles = Mock()
    engine.get_prompt_data = Mock(return_value=SimpleNamespace(references=[SimpleNamespace(audio_sha256='c' * 64)]))
    engine.shutdown = Mock()
    return engine


def test_installed_sdk_stream_really_returns_float32():
    """实际执行 SDK 的 stream，避免再次根据遗留注解把波形误判为字节。"""
    engine = sdk_engine_with_test_adapter('qwen3-tts-clone-0.6b-gguf', [])
    chunk = next(iter(engine.stream('测试', role_name='current-card-role')))
    assert isinstance(chunk, np.ndarray)
    assert chunk.dtype == np.float32
    assert chunk.tolist() == [-2.0, -1.0, -0.25, 0.0, 0.25, 1.0, 2.0]


@pytest.mark.parametrize(('model', 'options'), [
    ('qwen3-tts-clone-0.6b-gguf', {}),
    ('qwen3-tts-clone-1.7b-gguf', {'language': 'english', 'icl': True}),
    ('s2-pro-4b-gguf', {}),
    ('moss-tts-nano-onnx', {'mode': 1}),
    ('voxcpm2-gguf', {'mode': 1, 'control_text': '轻声'}),
])
def test_setup_preview_matches_formal_sdk_parameters_and_pcm(tmp_path, monkeypatch, model, options):
    """五模型的配置试听与正式试听应给 SDK 相同参数，并写出完全相同的 PCM16。"""
    import streamvox
    import streamvox.download

    recorded = []
    engine = sdk_engine_with_test_adapter(model, recorded)
    model_directory = tmp_path / 'models' / model
    model_directory.mkdir(parents=True)
    manifest = model_directory / 'manifest.json'
    manifest.write_bytes(b'{}')
    monkeypatch.setattr(streamvox, 'TTSEngine', lambda **kwargs: engine)
    def report_local_model_verification(_root, _pin, _check_cancelled, report):
        """本地缓存命中也会报告逐文件校验，保留这条真实进度出口。"""
        report('weights.gguf')
        return True

    monkeypatch.setattr('companion.core.tts_setup_worker.published_model_matches', report_local_model_verification)
    monkeypatch.setattr('companion.core.native_runtime_env.configure_native_runtime_environment', lambda: None)

    download = Mock(side_effect=AssertionError('已经校验的本地模型不应再次下载'))
    monkeypatch.setattr(streamvox.download, 'download_model', download)
    connection = Mock()
    preview_path = tmp_path / 'preview.wav'
    job = {'model': {'model_id': model, 'manifest_sha256': hashlib.sha256(manifest.read_bytes()).hexdigest()},
           'model_directory': str(model_directory.parent), 'voice_directory': str(tmp_path / 'voices'),
           'role_name': 'current-card-role', 'reference_snapshot': str(tmp_path / 'reference.wav'),
           'preview_path': str(preview_path), 'preview_text': '测试',
           'context': {'execution': {'onnx_provider': 'cpu', 'llama_backend': 'cpu'},
                       'prompt_text': '参考文本', 'inference_options': options, 'seed': 17}}

    run_tts_setup_job(job, connection, Event())
    assert connection.send.call_args.args[0]['phase'] == 'awaiting_confirmation'
    verification = next(call.args[0]['progress'] for call in connection.send.call_args_list
                        if call.args[0].get('progress', {}).get('current_file') == 'weights.gguf')
    assert verification == {'phase': 'verify', 'current_file': 'weights.gguf', 'completed_bytes': 0, 'total_bytes': None}
    engine.del_roles.assert_called_once_with('current-card-role')
    download.assert_not_called()
    provider = StreamVoxTtsProvider(engine=engine, config=StreamVoxProviderConfig(
        model=model, role_name='current-card-role', streamvox_seed=17, inference_options=options))
    formal_preview = provider.preview_role(role_name='current-card-role', text='测试')
    assert len(recorded) == 2
    assert recorded[0] == recorded[1]
    with wave.open(str(preview_path), 'rb') as preview, wave.open(BytesIO(formal_preview['wav_bytes']), 'rb') as formal:
        preview_pcm = preview.readframes(preview.getnframes())
        assert preview_pcm == formal.readframes(formal.getnframes())
        # 数值直接固定为现役算法结果，避免测试只拿同一个函数与自己比较。
        assert np.frombuffer(preview_pcm, dtype=np.int16).tolist() == [-32767, -32767, -8191, 0, 8191, 32767, 32767]
