from __future__ import annotations

import json
from typing import Any

from starlette.concurrency import run_in_threadpool

from companion.core.broadcast import BroadcastHub


class ApplicationStatusBroadcaster:
    """把初始化和 Bootstrap 的安全状态变化推送给控制端。"""

    def __init__(self, *, hub: BroadcastHub) -> None:
        """保存 Hub 引用，并从第一次发布开始建立状态指纹。"""

        self._hub = hub
        self._last_bootstrap_signature: str | None = None

    def current_events(self) -> list[dict[str, object]]:
        """返回状态连接首帧所需的运行时与 Bootstrap 事件。

        Returns:
            当前运行时初始化与 Bootstrap 事件；载荷不含密钥或内部路径。
        """

        bootstrap_status = _project_bootstrap_status(self._hub.bootstrap.status_payload())
        runtime = self._runtime_status()
        return [
            *([{"type": "runtime.initialization_changed", "payload": runtime}] if runtime is not None else []),
            {"type": "bootstrap.status_changed", "payload": bootstrap_status},
        ]

    async def publish_changed(self, *, force: bool = False) -> None:
        """只在 Bootstrap 状态实际变化时广播事件。

        Args:
            force: 是否忽略上次指纹，强制发送当前 Bootstrap 状态。
        Returns:
            无返回值；初始化阶段由其唯一所有者独立发布。
        """

        # 持久化密钥读取离开事件循环，避免状态连接冻结控制面。
        bootstrap_status = _project_bootstrap_status(
            await run_in_threadpool(self._hub.bootstrap.status_payload)
        )
        bootstrap_signature = _signature(bootstrap_status)

        if force or bootstrap_signature != self._last_bootstrap_signature:
            self._last_bootstrap_signature = bootstrap_signature
            await self._hub.publisher("application").publish_ephemeral(
                {"type": "bootstrap.status_changed", "payload": bootstrap_status}
            )

    def _runtime_status(self) -> dict[str, object] | None:
        """读取受管恢复快照；未安装初始化生命周期的窄测试不追加字段。"""
        provider = getattr(self._hub.bootstrap, "runtime_status_provider", None)
        payload = provider() if callable(provider) else None
        if not isinstance(payload, dict):
            return None
        keys = ("runtime_id", "operation_id", "revision", "state", "phase", "message", "retryable")
        return {key: payload[key] for key in keys if key in payload}


def _signature(payload: dict[str, Any]) -> str:
    """用稳定 JSON 生成状态指纹，避免嵌套字段顺序导致重复广播。"""

    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


_PUBLIC_BOOTSTRAP_STATUS_KEYS = (
    "llm",
    "voice",
    "has_qweather_api_key",
    "is_initialized",
)


def _project_bootstrap_status(value: object) -> dict[str, object]:
    """只复制 Bootstrap 对外契约字段，避免把内部状态广播给前端。

    BootstrapService.status_payload() 负责生成 HTTP 摘要；广播层仍要递归投影
    ``voice``，避免未来内部字段变更时把路径、能力探测或原始异常带进 WebSocket。
    顶层不能用模糊的字段子串过滤，否则 ``has_qweather_api_key`` 这种安全布尔摘要也会被误删。

    Args:
        value: Bootstrap 服务生成的完整公开状态候选。

    Returns:
        只包含四个固定顶层字段的控制通道载荷。
    """

    if not isinstance(value, dict):
        return {}

    projected = {
        key: value[key]
        for key in _PUBLIC_BOOTSTRAP_STATUS_KEYS
        if key in value
    }
    llm = projected.get("llm")
    if isinstance(llm, dict):
        projected["llm"] = {
            "is_ready": llm.get("is_ready") is True,
            "missing": [item for item in llm.get("missing", []) if isinstance(item, str)]
            if isinstance(llm.get("missing"), list)
            else [],
            "errors": [item for item in llm.get("errors", []) if isinstance(item, str)]
            if isinstance(llm.get("errors"), list)
            else [],
        }
    else:
        projected["llm"] = {"is_ready": False, "missing": [], "errors": []}
    voice = projected.get("voice")
    if isinstance(voice, dict):
        projected["voice"] = {
            "provider": voice.get("provider")
            if isinstance(voice.get("provider"), str)
            else "local_streamvox",
            "is_ready": voice.get("is_ready") is True,
            "phase": voice.get("phase")
            if isinstance(voice.get("phase"), str)
            else "unconfigured",
            "model_id": voice.get("model_id")
            if isinstance(voice.get("model_id"), str)
            else None,
            "role_name": voice.get("role_name")
            if isinstance(voice.get("role_name"), str)
            else None,
            "has_confirmed_voice": voice.get("has_confirmed_voice") is True,
        }
    else:
        projected["voice"] = {
            "provider": "local_streamvox",
            "is_ready": False,
            "phase": "unconfigured",
            "model_id": None,
            "role_name": None,
            "has_confirmed_voice": False,
        }
    return projected
