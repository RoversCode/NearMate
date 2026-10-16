"""验证唤醒回复控制接口等待同步工作时，实时媒体事件循环仍能运行。"""

import asyncio
import threading
from types import SimpleNamespace

import pytest

from companion.core.api.routers import wake_response
from companion.core.api.schemas import (
    WakeResponseConfigRequest,
    WakeResponsePreviewRequest,
    WakeResponseSynthesizeRequest,
)


@pytest.mark.parametrize(
    ("route_name", "service_method_name", "payload"),
    [
        ("generate_character_card_wake_response_config", "generate_texts", None),
        (
            "save_character_card_wake_response_config",
            "save_payload",
            WakeResponseConfigRequest(
                buckets={"morning": [{"id": "greeting", "text": "早上好"}]}
            ),
        ),
        (
            "preview_character_card_wake_response",
            "synthesize_wav",
            WakeResponsePreviewRequest(text="早上好"),
        ),
        (
            "synthesize_character_card_wake_response_item",
            "synthesize_item",
            WakeResponseSynthesizeRequest(bucket="morning", text="早上好"),
        ),
    ],
    ids=["generate", "save", "preview", "synthesize"],
)
@pytest.mark.asyncio
async def test_sync_wake_response_work_does_not_block_media_event_loop(
    route_name, service_method_name, payload
):
    """在生成或合成尚未结束时，让媒体同处的事件循环继续取得调度。

    Args:
        route_name: 需要验证的实际控制接口函数名。
        service_method_name: 接口调用的同步服务方法名。
        payload: 对应接口的请求对象，生成文本接口无需请求正文。

    Returns:
        无返回值；事件循环被同步工作阻塞时断言失败。
    """

    work_entered = threading.Event()
    release_work = threading.Event()
    work_finished = threading.Event()
    service_result = (
        b"RIFF-test-audio"
        if service_method_name == "synthesize_wav"
        else {"buckets": {}}
    )

    def blocking_service_call(*_args, **_kwargs):
        """模拟需要同步等待子进程或远端响应的控制工作。

        Args:
            *_args: 实际接口传入的位置参数。
            **_kwargs: 实际接口传入的关键字参数。

        Returns:
            当前接口所需的音频字节或配置结果。
        """

        work_entered.set()
        try:
            release_work.wait()
            return service_result
        finally:
            work_finished.set()

    service = SimpleNamespace(**{service_method_name: blocking_service_call})
    route_kwargs = {
        "card_id": "huohua",
        "container": SimpleNamespace(wake_response=service),
    }
    if payload is not None:
        route_kwargs["payload"] = payload
    if service_method_name == "synthesize_item":
        route_kwargs["item_id"] = "greeting"

    # 旧实现会阻塞事件循环，兜底释放必须来自独立线程，否则红测试本身也会挂死。
    watchdog = threading.Timer(2, release_work.set)
    watchdog.daemon = True
    watchdog.start()
    route_task = asyncio.create_task(getattr(wake_response, route_name)(**route_kwargs))
    try:
        entered = await asyncio.wait_for(
            asyncio.to_thread(work_entered.wait, 2.5), timeout=3
        )
        assert entered, "控制工作应已开始"
        # 同步工作仍被持有时本协程能运行，才说明媒体事件循环没有被控制接口占住。
        assert not work_finished.is_set(), "同步控制工作阻塞了实时媒体事件循环"
        assert not route_task.done()
    finally:
        release_work.set()
        watchdog.cancel()
        result = await asyncio.wait_for(route_task, timeout=2)

    if service_method_name == "synthesize_wav":
        assert result.body == service_result
        assert result.media_type == "audio/wav"
    else:
        assert result == service_result
