from __future__ import annotations

from types import SimpleNamespace
from dataclasses import replace
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image
from companion.environment.models import FrozenEnvironmentSnapshot

from companion.core.application.assistant_request_context_preparer import (
    AssistantRequestContextPreparationError,
    AssistantRequestContextPreparer,
)
from companion.materials.models import MaterialAttachment, MaterialWorkloadProfile, TurnMaterialContext


def _attachment(material_id: str, display_name: str) -> MaterialAttachment:
    """构造可区分归属的测试材料。"""

    return MaterialAttachment(
        material_id=material_id,
        turn_id=f"turn-{material_id}",
        kind="dragged_file",
        source="test",
        display_name=display_name,
        path=f"C:/{display_name}",
        created_at="2026-07-21T00:00:00+08:00",
        node_kind="file",
        media_class="text",
    )


def _profile(*, text_bytes: int) -> MaterialWorkloadProfile:
    """构造合格材料对应的测试画像。"""

    return MaterialWorkloadProfile(
        task_result_material_eligible=True,
        local_file_count=1,
        visual_unit_count=0,
        text_byte_count=text_bytes,
        local_total_bytes=text_bytes,
        contains_mixed_material=False,
        ignored_file_count=0,
        blocking_reasons=(),
    )


class InterleavingTurnMaterials:
    """模拟 latest 已变化、但旧上下文仍然有效的材料服务。"""

    def __init__(self) -> None:
        self.contexts = {
            "context-a": TurnMaterialContext(
                context_id="context-a",
                turn_id="turn-a",
                created_at="2026-07-21T00:00:00+08:00",
                attachments=[_attachment("material-a", "a.txt")],
            ),
            "context-b": TurnMaterialContext(
                context_id="context-b",
                turn_id="turn-b",
                created_at="2026-07-21T00:00:01+08:00",
                attachments=[_attachment("material-b", "b.txt")],
            ),
        }
        self.latest_context_id = "context-b"
        self.active_context_calls: list[str | None] = []
        self.frozen_context_ids: list[str] = []
        self.activated_context_id = "context-a"

    def active_context(self, context_id: str | None = None) -> TurnMaterialContext | None:
        """按显式编号读取；空编号故意返回 latest。"""

        self.active_context_calls.append(context_id)
        return self.contexts.get(context_id or self.latest_context_id)

    def freeze_active_context(
        self,
        context_id: str,
    ) -> tuple[TurnMaterialContext, MaterialWorkloadProfile] | None:
        """原子返回与指定编号绑定的上下文和画像。"""

        self.frozen_context_ids.append(context_id)
        context = self.contexts.get(context_id)
        if context is None:
            return None
        return context, _profile(text_bytes=10 if context_id == "context-a" else 20)

    def activate_turn(self, *, turn_id: str, created_at: str) -> TurnMaterialContext:
        """模拟 ASR 冻结 A 后另一个 turn 立刻成为 latest。"""

        _ = turn_id, created_at
        self.latest_context_id = "context-b"
        return self.contexts[self.activated_context_id]

def _service(turn_materials: InterleavingTurnMaterials) -> AssistantRequestContextPreparer:
    """构造只覆盖请求材料身份的上下文服务。"""

    service = AssistantRequestContextPreparer(
        hub=SimpleNamespace(
            turn_materials=turn_materials,
            recent_materials=SimpleNamespace(snapshot=lambda: ()),
            environment=SimpleNamespace(
                claim_snapshot_by_id=lambda _snapshot_id: None,
                wait_and_claim_for_turn=lambda _turn_id, **_kwargs: None,
                current_payload=lambda: None,
            ),
            current_prompt_profile_payload=lambda: {
                "character_card": "card-a",
                "user_call_name": "用户",
                "user_self_intro": "",
                "user_extra_notes": "",
            },
        ),
        realtime_input_runtime_owner=SimpleNamespace(
            current_interaction_state=lambda: SimpleNamespace(
                interaction_session_id="interaction-a"
            )
        ),
    )
    service.prepare_notice_context = lambda **_kwargs: {
        "notice_batch": [],
    }
    return service


def test_text_request_uses_one_explicit_material_context_after_latest_changes() -> None:
    """A 后创建 B 时，文本请求的编号、摘要、画像和附件仍必须全部来自 A。"""

    turn_materials = InterleavingTurnMaterials()
    prepared = _service(turn_materials).prepare(
        request_id="request-a",
        request_source="text_input",
        turn_material_context_id="context-a",
    )

    snapshot = prepared.snapshot
    foreground_context = snapshot.foreground_context
    assert snapshot.turn_material_context_id == "context-a"
    assert foreground_context["active_turn_material_context"]["context_id"] == "context-a"
    assert foreground_context["turn_material_summary"]["items"][0]["display_name"] == "a.txt"
    assert foreground_context["material_workload_profile"]["text_byte_count"] == 10
    assert not hasattr(snapshot, "turn_material_context")
    assert not hasattr(snapshot, "material_workload_profile")
    assert turn_materials.frozen_context_ids == ["context-a"]
    assert turn_materials.active_context_calls == []


def test_request_preparation_freezes_pixels_from_claimed_turn(tmp_path: Path) -> None:
    """请求入队前就完成图片冻结，后续材料变化不能影响来源 Recovery。"""

    output = BytesIO()
    Image.new("RGB", (8, 8), "green").save(output, format="PNG")
    image = tmp_path / "a.png"
    image.write_bytes(output.getvalue())
    materials = InterleavingTurnMaterials()
    materials.contexts["context-a"].attachments[:] = [
        replace(_attachment("image-a", "a.png"), path=str(image), media_class="mixed_or_document"),
    ]
    service = _service(materials)
    screen = FrozenEnvironmentSnapshot("claimed", "2026-09-13T10:00:00+08:00", "编辑器", "image/png", output.getvalue(), None)
    service.hub.environment.claim_snapshot_by_id = lambda _snapshot_id: screen
    snapshot = service.prepare(request_id="request-a", request_source="text_input", turn_material_context_id="context-a", environment_snapshot_id="claimed").snapshot
    pixels = tuple(image.data_url for image in snapshot.frozen_visual_input.images)
    image.write_bytes(b"changed")
    assert len(pixels) == 2
    assert tuple(image.data_url for image in snapshot.frozen_visual_input.images) == pixels
    assert "data:image" not in str(snapshot.foreground_context)
    assert materials.frozen_context_ids == ["context-a"]


def test_request_context_preparation_does_not_print_frozen_context(capsys) -> None:
    """冻结后的 Foreground 上下文不能再直接倾倒到 Backend 终端。"""

    turn_materials = InterleavingTurnMaterials()
    _service(turn_materials).prepare(
        request_id="request-a",
        request_source="text_input",
        turn_material_context_id="context-a",
    )

    terminal_text = capsys.readouterr().out
    assert "这次请求实际拿到的上下文" not in terminal_text
    assert "context-a" not in terminal_text


def test_final_asr_carries_frozen_context_id_even_when_latest_changes() -> None:
    """final ASR 冻结 A 后即使 latest 变成 B，同一次请求也只能继续绑定 A。"""

    turn_materials = InterleavingTurnMaterials()
    prepared = _service(turn_materials).prepare_final_asr_request_context(
        request_id="request-a",
        source_turn_id="voice-turn-a",
    )

    snapshot = prepared.snapshot
    foreground_context = snapshot.foreground_context
    assert snapshot.turn_material_context_id == "context-a"
    assert foreground_context["active_turn_material_context"]["context_id"] == "context-a"
    assert foreground_context["turn_material_summary"]["items"][0]["display_name"] == "a.txt"
    assert foreground_context["material_workload_profile"]["text_byte_count"] == 10
    assert not hasattr(snapshot, "turn_material_context")
    assert not hasattr(snapshot, "material_workload_profile")
    assert turn_materials.frozen_context_ids == ["context-a"]
    assert turn_materials.active_context_calls == []


def test_final_asr_context_error_preserves_created_material_identity() -> None:
    """资料构造失败时必须把已创建 context id 交给输入线程执行回收。"""

    turn_materials = InterleavingTurnMaterials()
    service = _service(turn_materials)
    service._freeze_request_context = lambda **_kwargs: (_ for _ in ()).throw(
        RuntimeError("payload failed")
    )

    with pytest.raises(AssistantRequestContextPreparationError) as captured:
        service.prepare_final_asr_request_context(
            request_id="request-a",
            source_turn_id="voice-turn-a",
        )

    assert captured.value.turn_material_context_id == "context-a"


def test_foreground_context_disables_both_global_preview_fallbacks() -> None:
    """统一兜底开关关闭时不得读取环境或材料的全局最新状态。"""

    turn_materials = InterleavingTurnMaterials()
    service = _service(turn_materials)
    service.hub.environment.current_payload = lambda: pytest.fail(
        "用户请求不应读取全局环境预览"
    )

    foreground_context = service.build_foreground_context(
        allow_global_preview_fallback=False,
    )

    assert foreground_context["environment_snapshot"] is None
    assert foreground_context["active_turn_material_context"] is None
    assert turn_materials.active_context_calls == []


def test_user_request_freezes_current_session_pending_work_interactions() -> None:
    """普通用户轮冻结当前会话仍有效的权限和资料请求。"""

    turn_materials = InterleavingTurnMaterials()
    service = _service(turn_materials)
    service.pending_work_interactions_provider = lambda session_id: [
        {
            "request_id": "question-a",
            "activity_id": "activity-a",
            "kind": "question",
            "questions": [{"question": f"{session_id} 要输出什么格式？"}],
        }
    ]

    prepared = service.prepare(
        request_id="request-a",
        request_source="text_input",
        turn_material_context_id="context-a",
    )

    assert prepared.snapshot.foreground_context["pending_work_interactions"] == [
        {
            "request_id": "question-a",
            "activity_id": "activity-a",
            "kind": "question",
            "questions": [{"question": "interaction-a 要输出什么格式？"}],
        }
    ]


def test_request_derivation_only_uses_cognitive_result_notices() -> None:
    """只有 Cognitive 成果回复才能成为后续替代投影的来源。"""

    turn_materials = InterleavingTurnMaterials()
    service = _service(turn_materials)
    service.prepare_notice_context = lambda **_kwargs: {
        "notice_batch": [
            {
                "source": "cognitive",
                "intent": "cognitive_result",
                "activity_id": "cognitive-a",
            },
            {
                "source": "cognitive",
                "intent": "progress_report",
                "activity_id": "cognitive-progress",
            },
            {
                "source": "background_agent",
                "intent": "work_result",
                "activity_id": "background-a",
            },
            {
                "source": "cognitive",
                "intent": "cognitive_result",
                "activity_id": " cognitive-a ",
            },
        ],
    }

    prepared = service.prepare(
        request_id="request-a",
        request_source="text_input",
        turn_material_context_id="context-a",
    )

    assert prepared.snapshot.derived_from_activity_ids == ("cognitive-a",)


@pytest.mark.parametrize("context_id", [None, "", "missing-context"])
def test_user_request_rejects_missing_or_unknown_material_context_id(
    context_id: str | None,
) -> None:
    """真实用户请求不得在材料身份缺失时回退到 latest。"""

    turn_materials = InterleavingTurnMaterials()
    with pytest.raises(RuntimeError, match="turn material context"):
        _service(turn_materials).prepare(
            request_id="request-a",
            request_source="text_input",
            turn_material_context_id=context_id,
        )

    assert None not in turn_materials.active_context_calls
    if context_id:
        assert turn_materials.frozen_context_ids == [context_id]
    else:
        assert turn_materials.frozen_context_ids == []
