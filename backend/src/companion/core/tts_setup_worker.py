"""在独立进程中下载模型、准备当前角色音色并生成试听。"""

from __future__ import annotations

import os
from pathlib import Path
import wave

import numpy as np

from companion.audio.streamvox_audio import streamvox_float_audio_to_pcm16
from companion.audio.streamvox_inference_options import normalize_streamvox_inference_options
from companion.core.voice_setup_assets import sha256_file, published_model_matches, model_download_space_required, validate_model_directory


def download_pinned_model_files(job: dict, connection, cancel_event) -> Path:
    """校验或下载固定模型文件，不创建引擎、不占用当前角色语音。

    Args:
        job: 发行固定模型与用户已确认的保存目录。
        connection: 向父进程发送下载和文件校验进度。
        cancel_event: 用户取消、退出或运行时替换的取消标记。
    Returns:
        通过发行摘要校验的已发布模型目录。
    """

    from streamvox.download import download_model

    def check_cancelled() -> None:
        """下载和大文件核验均及时响应取消，避免失效结果继续发布。"""

        if cancel_event.is_set():
            raise InterruptedError('模型文件准备已取消')

    def report(phase: str, **fields) -> None:
        """发送完整进度形状，文件完成后仍需等进程释放。"""

        check_cancelled()
        connection.send({'phase': phase, **fields})

    def progress(value) -> None:
        """下载器完成只说明权重准备完成，不代表角色语音可以切换。"""

        phase = 'verify' if value.phase == 'complete' else value.phase
        report(phase, progress={'phase': phase, 'completed_bytes': value.completed_bytes,
                               'total_bytes': value.total_bytes, 'current_file': value.current_file,
                               'fraction': value.fraction})

    pin = job['model']
    root = Path(job['model_directory'])
    report('verify')
    model_directory = (root / pin['model_id']).resolve()
    if not published_model_matches(root, pin, check_cancelled,
            lambda name: report('verify', progress={'phase': 'verify', 'current_file': name,
                                                  'completed_bytes': 0, 'total_bytes': None})):
        validate_model_directory(str(root), model_download_space_required(root, pin))
        report('download')
        result = download_model(model=pin['model_id'], source='modelscope', target=job['model_directory'],
                                revision=pin['revision'], expected_manifest_sha256=pin['manifest_sha256'],
                                cache_dir=str(root / '.streamvox-download-cache'),
                                progress_callback=progress, cancel_event=cancel_event)
        model_directory = Path(result.target_dir).resolve()
    if model_directory != (root / pin['model_id']).resolve():
        raise RuntimeError('下载器返回了任务目录之外的模型')
    if sha256_file(model_directory / 'manifest.json') != pin['manifest_sha256']:
        raise RuntimeError('模型清单不符合本次发行固定版本')
    check_cancelled()
    return model_directory


def run_tts_model_download_job(job: dict, connection, cancel_event) -> None:
    """只获取模型文件，完成后退出，不改变语音绑定与试听资格。

    Args:
        job: 固定的发行模型和保存目录。
        connection: 父进程的进度管道。
        cancel_event: 父进程持有的取消事件。
    Returns:
        无；文件完成或失败通过管道返回。
    """

    try:
        download_pinned_model_files(job, connection, cancel_event)
        connection.send({'phase': 'completed'})
    except Exception as error:
        connection.send({'phase': 'cancelled' if cancel_event.is_set() else 'failed', 'error': str(error)})
    finally:
        connection.close()


def run_tts_setup_job(job: dict, connection, cancel_event) -> None:
    """执行冻结任务，通过管道返回进度，进程退出即释放全部推理资源。

    Args:
        job: 父进程冻结的模型版本、角色素材、执行方式和受管目录。
        connection: 仅用于向父进程发送结构化进度与结果的管道。
        cancel_event: 取消事件，由父进程在用户取消或运行时替换时置位。
    Returns:
        无返回值；完成或失败通过管道发送。
    """

    engine = None
    try:
        os.environ["STREAMVOX_HOME"] = job["voice_directory"]
        from companion.core.native_runtime_env import configure_native_runtime_environment
        configure_native_runtime_environment()

        from streamvox import TTSEngine

        def emit(phase: str, **fields) -> None:
            """每次阶段交接前响应取消，避免取消后继续建立新模型资源。"""

            if cancel_event.is_set():
                raise InterruptedError("语音配置已取消")
            connection.send({"phase": phase, **fields})

        pin = job["model"]
        model_directory = download_pinned_model_files(job, connection, cancel_event)
        emit("load")
        selection = job["context"]["execution"]
        engine = TTSEngine(model=str(model_directory),
                           device="cpu" if selection["onnx_provider"] == "cpu" else "gpu",
                           onnx_provider=selection["onnx_provider"], llama_backend=selection["llama_backend"],
                           verify_model_sha256=True)
        emit("prepare_voice")
        # 重试会回到同一素材目录；上次可能已建好音色却未完成试听，先清理该任务专属角色。
        if job['role_name'] in engine.list_roles():
            engine.del_roles(job['role_name'])
        engine.make_prompt(role_name=job["role_name"], audio_path=job["reference_snapshot"],
                           prompt_text=job["context"]["prompt_text"], persist=True)
        prompt_data = engine.get_prompt_data(job["role_name"])
        if prompt_data is None or not prompt_data.references or not prompt_data.references[0].audio_sha256:
            raise RuntimeError("模型未保存可核验的角色参考音频")
        normalized_audio_sha256 = prompt_data.references[0].audio_sha256
        emit("preview")
        output = Path(job["preview_path"])
        temporary = output.with_suffix(".partial.wav")
        options = normalize_streamvox_inference_options(pin["model_id"], job["context"]["inference_options"])
        if job["context"]["seed"] is not None:
            options["seed"] = job["context"]["seed"]
        sample_count = 0
        with wave.open(str(temporary), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(int(engine.runtime.sample_rate))
            for chunk in engine.stream(job["preview_text"], role_name=job["role_name"], **options):
                if cancel_event.is_set():
                    raise InterruptedError("语音配置已取消")
                # SDK 实际返回 float32 波形；试听必须使用正式语音相同的 PCM16 量化规则。
                if not isinstance(chunk, np.ndarray) or chunk.dtype != np.float32:
                    raise RuntimeError("StreamVox 返回了非法 float32 音频块")
                pcm16 = streamvox_float_audio_to_pcm16(chunk)
                audio.writeframesraw(pcm16)
                sample_count += len(pcm16) // 2
        if sample_count == 0:
            raise RuntimeError("模型没有生成可试听音频")
        os.replace(temporary, output)
        actual_backends = engine.actual_backends
        engine.shutdown()
        engine = None
        emit("awaiting_confirmation", actual_backends=actual_backends,
             preview_sha256=sha256_file(output), normalized_audio_sha256=normalized_audio_sha256,
             sample_count=sample_count)
    except Exception as error:
        connection.send({"phase": "cancelled" if cancel_event.is_set() else "failed", "error": str(error)})
    finally:
        if engine is not None:
            engine.shutdown()
        connection.close()
