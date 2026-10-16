from __future__ import annotations

from collections.abc import Callable
from queue import Queue
from threading import Event, Thread
from types import SimpleNamespace
from typing import Any

import numpy as np

from companion.audio.providers.tts_provider import (
    StreamVoxProviderConfig,
    StreamVoxTtsProvider,
)
from companion.realtime.assistant_events import StreamVoxManagementRequest
from companion.realtime.assistant_process import _process_streamvox_management_command


class BlockingStreamVoxEngine:
    """模拟持有原生推理资源的可阻塞 StreamVox engine。"""

    def __init__(self, *, blocking: bool) -> None:
        """初始化测试 engine。

        Args:
            blocking: 是否等待测试显式释放后再产生音频。
        """

        self.runtime = SimpleNamespace(sample_rate=24_000)
        self.blocking = blocking
        self.stream_started = Event()
        self.release_stream = Event()
        self.shutdown_called = Event()

    def list_roles(self) -> list[str]:
        """返回已存在的默认角色。"""

        return ["current-card-role"]

    def stream(self, _text: str, **_kwargs: object):  # type: ignore[no-untyped-def]
        """等待测试释放后产生一个非空音频块。"""

        self.stream_started.set()
        if self.blocking and not self.release_stream.wait(timeout=2.0):
            raise TimeoutError("测试未及时释放 StreamVox 音频流")
        yield np.array([0.25], dtype=np.float32)

    def shutdown(self) -> None:
        """记录 engine 已进入关闭流程。"""

        self.shutdown_called.set()


class RecordingManagementProvider:
    """记录配置切换调用顺序的管理命令测试 provider。"""

    def __init__(self, calls: list[str]) -> None:
        """绑定共享调用记录。

        Args:
            calls: 按发生顺序保存管理动作的列表。
        """

        self.calls = calls

    def switch_config(
        self,
        *,
        before_switch: Callable[[], None],
        **kwargs: object,
    ) -> dict[str, object]:
        """执行预检后回调，记录配置切换并返回最小运行状态。"""

        before_switch()
        self.calls.append("switch_config")
        return {"model": kwargs["model"]}


class RecordingGenerationRunner:
    """记录当前 generation 取消动作的管理命令测试 runner。"""

    def __init__(self, calls: list[str]) -> None:
        """绑定共享调用记录。

        Args:
            calls: 按发生顺序保存管理动作的列表。
        """

        self.calls = calls

    def cancel_current_generation(self, *, reason: str) -> None:
        """记录 generation 取消及其原因。"""

        self.calls.append(f"cancel:{reason}")


def test_switch_config_waits_until_active_stream_releases_engine() -> None:
    """模型切换不得关闭仍在流式推理中的旧 engine。"""

    old_engine = BlockingStreamVoxEngine(blocking=True)
    next_engine = BlockingStreamVoxEngine(blocking=False)
    provider = StreamVoxTtsProvider(
        engine=old_engine,
        config=StreamVoxProviderConfig(model="qwen3-tts-clone-0.6b-gguf", role_name="current-card-role"),
    )
    provider._build_engine = lambda **_kwargs: next_engine  # type: ignore[method-assign]

    stream_errors: list[BaseException] = []
    switch_errors: list[BaseException] = []
    switch_started = Event()
    switch_finished = Event()

    def consume_stream() -> None:
        """消费完整音频流，并把线程异常带回主测试线程。"""

        try:
            list(provider.stream_segment("测试", "generation-a"))
        except BaseException as exc:  # pragma: no cover - 仅用于传递线程异常
            stream_errors.append(exc)

    def switch_model() -> None:
        """尝试切换模型，并把线程异常带回主测试线程。"""

        switch_started.set()
        try:
            provider.switch_config(
                model="qwen3-tts-clone-0.6b-gguf",
                device="cpu",
                llama_backend="cpu",
                role_name="current-card-role",
            )
        except BaseException as exc:  # pragma: no cover - 仅用于传递线程异常
            switch_errors.append(exc)
        finally:
            switch_finished.set()

    stream_thread = Thread(target=consume_stream)
    switch_thread = Thread(target=switch_model)
    stream_thread.start()
    assert old_engine.stream_started.wait(timeout=1.0)
    switch_thread.start()
    assert switch_started.wait(timeout=1.0)

    # 旧流未释放时切换线程必须保持阻塞，且绝不能提前关闭底层 engine。
    assert not switch_finished.wait(timeout=0.05)
    assert not old_engine.shutdown_called.is_set()

    old_engine.release_stream.set()
    stream_thread.join(timeout=1.0)
    switch_thread.join(timeout=1.0)

    assert not stream_thread.is_alive()
    assert not switch_thread.is_alive()
    assert stream_errors == []
    assert switch_errors == []
    assert old_engine.shutdown_called.is_set()
    assert provider.config.model == "qwen3-tts-clone-0.6b-gguf"
    assert provider.config.device == "cpu"


def test_switch_management_cancels_generation_before_provider_change() -> None:
    """配置切换命令必须先取消当前 generation，再进入 provider。"""

    calls: list[str] = []
    response_queue: Queue[Any] = Queue()
    child_runtime = SimpleNamespace(
        tts_provider=RecordingManagementProvider(calls),
        runner=RecordingGenerationRunner(calls),
    )
    request = StreamVoxManagementRequest(
        request_id="switch-a",
        kind="switch_config",
        payload={
            "model": "voxcpm2-gguf",
            "device": "auto",
            "llama_backend": None,
            "role_name": "current-card-role",
            "streamvox_seed": None,
            "inference_options": {},
        },
        issued_at=1.0,
    )

    _process_streamvox_management_command(
        request,
        response_queue=response_queue,
        child_runtime=child_runtime,
    )

    response = response_queue.get_nowait()
    assert calls == ["cancel:streamvox_config_switch", "switch_config"]
    assert response.ok is True
    assert response.payload == {"model": "voxcpm2-gguf"}


def test_invalid_switch_management_does_not_cancel_generation() -> None:
    """配置预检失败时不得取消仍可继续运行的当前 generation。"""

    calls: list[str] = []
    response_queue: Queue[Any] = Queue()
    provider = StreamVoxTtsProvider(
        engine=BlockingStreamVoxEngine(blocking=False),
        config=StreamVoxProviderConfig(model="qwen3-tts-clone-0.6b-gguf", role_name="current-card-role"),
    )
    child_runtime = SimpleNamespace(
        tts_provider=provider,
        runner=RecordingGenerationRunner(calls),
    )
    request = StreamVoxManagementRequest(
        request_id="switch-invalid",
        kind="switch_config",
        payload={
            "model": "",
            "device": "auto",
            "llama_backend": None,
            "role_name": "current-card-role",
            "streamvox_seed": None,
            "inference_options": {},
        },
        issued_at=1.0,
    )

    _process_streamvox_management_command(
        request,
        response_queue=response_queue,
        child_runtime=child_runtime,
    )

    response = response_queue.get_nowait()
    assert calls == []
    assert response.ok is False
    assert "must not be blank" in str(response.error)
