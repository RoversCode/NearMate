from __future__ import annotations

import json
import socket
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping
from companion.core.runtime_paths import SHARED_ROOT


SERVICE_NAMES = ("backend", "console", "desktop", "livekit")


@dataclass(frozen=True)
class DevPort:
    """表示一个开发服务要绑定的主机和端口。"""

    host: str
    port: int


def load_dev_ports(path: Path | None = None) -> Mapping[str, DevPort]:
    """读取共享开发端口文件并检查四个服务是否都配置正确。

    Args:
        path: 端口契约文件路径；默认是当前运行时的共享端口文件。

    Returns:
        以服务名为键的不可变端口配置映射。

    Raises:
        ValueError: 文件结构、主机或端口不符合约定时抛出。
        OSError: 文件无法读取时向上抛出。
    """

    ports_path = path or (SHARED_ROOT / "dev-ports.json")
    payload = json.loads(ports_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("开发端口契约必须是对象。")
    unexpected_services = set(payload) - set(SERVICE_NAMES)
    if unexpected_services:
        raise ValueError(f"开发端口契约包含未知服务：{', '.join(sorted(unexpected_services))}。")

    result: dict[str, DevPort] = {}
    seen_addresses: set[tuple[str, int]] = set()
    for service_name in SERVICE_NAMES:
        service_payload = payload.get(service_name)
        if not isinstance(service_payload, dict):
            raise ValueError(f"开发端口契约缺少服务：{service_name}。")
        host = service_payload.get("host")
        port = service_payload.get("port")
        if not isinstance(host, str) or not host.strip():
            raise ValueError(f"开发端口契约中的 {service_name}.host 必须是非空字符串。")
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError(f"开发端口契约中的 {service_name}.port 必须是 1 到 65535 的整数。")
        endpoint = (host.strip(), port)
        if endpoint in seen_addresses:
            raise ValueError(f"开发端口契约中的服务使用了同一地址：{endpoint[0]}:{endpoint[1]}。")
        seen_addresses.add(endpoint)
        result[service_name] = DevPort(*endpoint)

    return MappingProxyType(result)


def ensure_port_available(host: str, port: int) -> None:
    """在服务构建和启动前确认控制端口可以绑定。

    Args:
        host: 服务要监听的主机地址。
        port: 服务要监听的 TCP 端口。

    Returns:
        端口可以绑定时不返回任何值。

    Raises:
        OSError: 端口已被占用或主机地址无效时抛出原始系统错误。
    """

    address_family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(address_family, socket.SOCK_STREAM) as probe:
        probe.bind((host, port))
        probe.listen(1)
