from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast


ControlEventDomain = Literal[
    "application",
    "interaction",
    "materials",
    "turn_admission",
    "assistant_output",
    "activity",
    "reminder",
    "settings",
    "audio_devices",
    "shell",
]

CONTROL_EVENT_DOMAINS = frozenset(
    {
        "application",
        "interaction",
        "materials",
        "turn_admission",
        "assistant_output",
        "activity",
        "reminder",
        "settings",
        "audio_devices",
        "shell",
    }
)


class ControlEventContractError(RuntimeError):
    """表示公共控制事件名称或领域违反共享契约。

    Args:
        event_name: 发生问题的公共事件名称。
        expected_domain: 共享目录登记的领域；未知事件时为空。
        actual_domain: 调用方使用的领域。
    """

    def __init__(
        self,
        *,
        event_name: str,
        expected_domain: str | None,
        actual_domain: str,
    ) -> None:
        self.event_name = event_name
        self.expected_domain = expected_domain
        self.actual_domain = actual_domain
        super().__init__(
            "公共控制事件契约冲突 "
            f"event_name={event_name} expected_domain={expected_domain} "
            f"actual_domain={actual_domain}"
        )


@dataclass(frozen=True, slots=True)
class EventDefinition:
    """描述一条跨端公共事件及其唯一领域。

    Args:
        name: WebSocket 或 UI 命令使用的事件名称。
        domain: 对该事件语义负责的业务领域。
    """

    name: str
    domain: ControlEventDomain


@dataclass(frozen=True, slots=True)
class EventCatalog:
    """保存 Backend 与 Desktop 共用的双向事件定义。"""

    ui_to_core: tuple[EventDefinition, ...]
    core_to_ui: tuple[EventDefinition, ...]

    def domain_for(self, event_name: str) -> ControlEventDomain | None:
        """返回事件所属领域，未知事件返回空值。"""

        for definition in self.core_to_ui:
            if definition.name == event_name:
                return definition.domain
        return None

    def require_core_domain(
        self,
        event_name: str,
        domain: ControlEventDomain,
    ) -> None:
        """确认一条 Backend 公共事件由指定领域拥有。

        Args:
            event_name: 准备公开的事件名称。
            domain: 当前 Publisher 绑定的领域。

        Raises:
            ControlEventContractError: 事件未知或领域不一致时抛出。
        """

        expected_domain = next(
            (
                definition.domain
                for definition in self.core_to_ui
                if definition.name == event_name
            ),
            None,
        )
        if expected_domain == domain:
            return
        raise ControlEventContractError(
            event_name=event_name,
            expected_domain=expected_domain,
            actual_domain=domain,
        )


def load_event_catalog() -> EventCatalog:
    """加载并严格校验共享事件契约。

    Returns:
        名称与领域均已验证的双向公共事件目录。

    Raises:
        ValueError: JSON 结构、名称、领域或唯一性不合法时抛出。
    """

    from companion.core.runtime_paths import SHARED_ROOT
    payload = json.loads(
        (SHARED_ROOT / "control-events.json").read_text(encoding="utf-8")
    )
    return EventCatalog(
        ui_to_core=_read_definitions(payload.get("ui_to_core"), section="ui_to_core"),
        core_to_ui=_read_definitions(payload.get("core_to_ui"), section="core_to_ui"),
    )


def _read_definitions(value: object, *, section: str) -> tuple[EventDefinition, ...]:
    """把共享 JSON 的一个分区转换为不可变事件定义。

    Args:
        value: JSON 中尚未验证的分区值。
        section: 用于错误定位的分区名称。

    Returns:
        保持 JSON 顺序的事件定义。

    Raises:
        ValueError: 分区不是严格的对象数组时抛出。
    """

    if not isinstance(value, list):
        raise ValueError(f"共享事件目录 {section} 必须是数组")
    definitions: list[EventDefinition] = []
    seen_names: set[str] = set()
    for index, item in enumerate(value):
        if not isinstance(item, dict) or set(item) != {"name", "domain"}:
            raise ValueError(f"共享事件目录 {section}[{index}] 必须只包含 name/domain")
        name = item.get("name")
        domain = item.get("domain")
        if not isinstance(name, str) or not name.strip() or name != name.strip():
            raise ValueError(f"共享事件目录 {section}[{index}].name 非法")
        if domain not in CONTROL_EVENT_DOMAINS:
            raise ValueError(f"共享事件目录 {section}[{index}].domain 非法")
        if name in seen_names:
            raise ValueError(f"共享事件目录 {section} 存在重复事件 {name}")
        seen_names.add(name)
        definitions.append(
            EventDefinition(name=name, domain=cast(ControlEventDomain, domain))
        )
    return tuple(definitions)
