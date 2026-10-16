from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from companion.core.api.routers.bootstrap import probe_execution_configuration


@pytest.mark.anyio
async def test_tts_probe_route_exposes_structured_capabilities() -> None:
    """语音配置页调用的探测路径应真实委托到 TTS 服务。"""

    expected = {
        "backends": [{"kind": "onnx", "backend": "cpu", "available": True}],
        "recommendation": {"onnx_provider": "cpu", "llama_backend": "cpu"},
    }
    calls: list[bool] = []

    def probe() -> dict[str, object]:
        """记录探测调用并返回固定结果。"""

        calls.append(True)
        return expected

    container = SimpleNamespace(
        tts_setup=SimpleNamespace(probe_capabilities=probe),
    )

    result = await probe_execution_configuration(container)

    assert result == expected
    assert calls == [True]
