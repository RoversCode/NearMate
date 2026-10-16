from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from companion.character_cards.store import CharacterCardStore
from companion.core.streamvox_example_console_service import (
    StreamVoxExampleConsoleService,
)
from companion.core.wake_response_console_service import (
    WakeResponseConsoleService,
)
from companion.observability.prompt_audit import PromptAuditRecorder


CARDS_DIR = Path(__file__).parents[2] / "data" / "character-cards"


class FailingCharacterAuthoringClient:
    """模拟生成失败并记录连接是否被关闭的短生命周期 Client。"""

    def __init__(self) -> None:
        """初始化尚未关闭的测试 Client。"""

        self.closed = False

    def complete_text(self, **_kwargs: Any) -> str:
        """固定模拟厂商调用失败。

        Args:
            **_kwargs: 业务服务传入的消息和调用意图。

        Raises:
            RuntimeError: 每次调用都固定抛出。
        """

        raise RuntimeError("character authoring failed")

    def close(self) -> None:
        """记录业务服务已经回收本次调用的连接资源。"""

        self.closed = True


def test_wake_response_generation_closes_client_when_generation_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """唤醒回复生成失败时也必须关闭临时 Character Authoring Client。

    Args:
        monkeypatch: 替换真实 Factory 的 pytest 工具。
    """

    client = FailingCharacterAuthoringClient()
    card = SimpleNamespace(
        meta=SimpleNamespace(
            name="测试角色",
            title="测试人设",
            user_call_name="用户",
        ),
        voiceprint=SimpleNamespace(
            example_dialogues=SimpleNamespace(items=[]),
            emotion_grading={},
            sentence_habits=[],
            speech_style="自然说话",
            catchphrases=[],
        ),
        model_constraints=SimpleNamespace(persona_forbidden_rules="无"),
    )
    service = object.__new__(WakeResponseConsoleService)
    service.hub = SimpleNamespace(
        settings=SimpleNamespace(
            llm=object(),
            user_self_intro="",
            user_extra_notes="",
        ),
        character_cards=SimpleNamespace(load_card=lambda _card_id: card),
    )
    service.prompt_audit_recorder = PromptAuditRecorder(
        enabled=False,
        output_dir="unused",
        max_runs=20,
        process_role="backend-main",
    )
    monkeypatch.setattr(
        "companion.core.wake_response_console_service.LlmClientFactory",
        lambda *_args, **_kwargs: SimpleNamespace(
            client=lambda _role_id: client
        ),
    )

    with pytest.raises(RuntimeError, match="character authoring failed"):
        service.generate_texts("card-1")

    assert client.closed is True


def test_streamvox_example_generation_closes_client_when_generation_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """StreamVox 示例生成失败时也必须关闭临时 Character Authoring Client。

    Args:
        monkeypatch: 替换服务 Client 构造入口的 pytest 工具。
    """

    client = FailingCharacterAuthoringClient()
    card = CharacterCardStore(CARDS_DIR).load_card("huohua")
    service = object.__new__(StreamVoxExampleConsoleService)
    service.hub = SimpleNamespace(
        character_cards=SimpleNamespace(load_card=lambda _card_id: card),
    )
    monkeypatch.setattr(service, "_client", lambda: client)

    with pytest.raises(RuntimeError, match="character authoring failed"):
        service.generate_examples(
            path_card_id=card.meta.card_id,
            card=card,
            target_model="generic",
        )

    assert client.closed is True
