from __future__ import annotations

from pathlib import Path

import pytest

from companion.filesystem.safety import FileSystemNodeSnapshot
from companion.foreground_tools.background_agent.execution import (
    BackgroundAgentToolExecutor,
)
from companion.foreground_tools.background_agent.materials import (
    MAX_RECENT_MATERIAL_PATHS,
    recent_materials_from_paths,
)
from companion.llm.foreground_models import ForegroundToolCall
from companion.materials import recent_validation as validation_module
from companion.realtime.assistant_events import RECENT_MATERIAL_OBSERVATION_FIELD


def _recent_record(
    path: Path,
    *,
    node_kind: str = "file",
) -> dict[str, object]:
    """构造 Foreground 已冻结的一项近期路径能力。"""

    return {
        "display_name": path.name,
        "path": str(path),
        "material_kind": "dragged_folder" if node_kind == "directory" else "dragged_file",
        "node_kind": node_kind,
        "media_class": "text" if node_kind == "file" else "mixed_or_document",
    }


def test_recent_file_uses_current_content_and_gets_new_task_identity(
    tmp_path: Path,
) -> None:
    """近期文件按当前路径内容重挂，不复用旧身份也不拒绝空文件。"""

    material_file = tmp_path / "app.py"
    material_file.write_text("旧内容", encoding="utf-8")
    foreground_context = {"recent_materials": [_recent_record(material_file)]}
    material_file.write_text("", encoding="utf-8")

    materials, failure = recent_materials_from_paths(
        foreground_context,
        [str(material_file)],
    )

    assert failure is None
    assert len(materials) == 1
    assert materials[0].material_id.startswith("recent-")
    assert materials[0].local_path == str(material_file)
    assert materials[0].material_kind == "dragged_file"
    assert materials[0].media_class == "text"
    assert materials[0].node_kind == "file"


def test_recent_directory_reads_its_current_tree(tmp_path: Path) -> None:
    """近期目录允许内容变化，只要求根节点仍是安全可读目录。"""

    project = tmp_path / "project"
    project.mkdir()
    (project / "new.py").write_text("print('new')", encoding="utf-8")

    materials, failure = recent_materials_from_paths(
        {"recent_materials": [_recent_record(project, node_kind="directory")]},
        [str(project)],
    )

    assert failure is None
    assert len(materials) == 1
    assert materials[0].material_kind == "dragged_folder"
    assert materials[0].node_kind == "directory"


def test_unregistered_path_is_rejected_without_leaking_path() -> None:
    """模型猜测的路径必须整体拒绝，工具载荷只暴露名称和自然说明。"""

    materials, failure = recent_materials_from_paths(
        {"recent_materials": []},
        ["C:/private/secret.txt"],
    )

    assert materials == ()
    assert failure is not None
    assert failure.reason == "recent_material_not_registered"
    assert failure.to_tool_payload() == {
        "display_name": "secret.txt",
        "detail": "“secret.txt”已经不在我当前记得的材料里了，请重新提供。",
    }
    assert "C:/private" not in str(failure.to_tool_payload())


def test_missing_recent_file_returns_stable_failure(tmp_path: Path) -> None:
    """近期路径消失时返回 missing，不生成任何任务级材料。"""

    missing = tmp_path / "gone.txt"

    materials, failure = recent_materials_from_paths(
        {"recent_materials": [_recent_record(missing)]},
        [str(missing)],
    )

    assert materials == ()
    assert failure is not None
    assert failure.reason == "recent_material_missing"
    assert str(missing) not in str(failure.to_tool_payload())


@pytest.mark.parametrize(
    ("registered_kind", "create_current"),
    [
        ("file", lambda path: path.mkdir()),
        ("directory", lambda path: path.write_text("文件", encoding="utf-8")),
    ],
)
def test_recent_material_type_change_is_rejected(
    tmp_path: Path,
    registered_kind: str,
    create_current,
) -> None:
    """文件和目录互换时必须拒绝，不能扩大或改变原授权形状。"""

    candidate = tmp_path / "same-name"
    create_current(candidate)

    materials, failure = recent_materials_from_paths(
        {
            "recent_materials": [
                _recent_record(candidate, node_kind=registered_kind)
            ]
        },
        [str(candidate)],
    )

    assert materials == ()
    assert failure is not None
    assert failure.reason == "recent_material_type_changed"


def test_link_or_reparse_root_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """近期路径根变成链接或重解析点时按 unsafe 整体拒绝。"""

    candidate = tmp_path / "linked.txt"
    candidate.write_text("内容", encoding="utf-8")
    monkeypatch.setattr(
        validation_module,
        "inspect_filesystem_node",
        lambda _path: FileSystemNodeSnapshot(
            node_kind="file",
            is_linked_or_reparse=True,
            size=2,
        ),
    )

    materials, failure = recent_materials_from_paths(
        {"recent_materials": [_recent_record(candidate)]},
        [str(candidate)],
    )

    assert materials == ()
    assert failure is not None
    assert failure.reason == "recent_material_unsafe"


def test_unreadable_recent_node_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """根节点存在但无法打开时返回 unreadable，不占用 Background。"""

    candidate = tmp_path / "locked.txt"
    candidate.write_text("内容", encoding="utf-8")
    monkeypatch.setattr(
        validation_module,
        "ensure_filesystem_node_readable",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            PermissionError("denied")
        ),
    )

    materials, failure = recent_materials_from_paths(
        {"recent_materials": [_recent_record(candidate)]},
        [str(candidate)],
    )

    assert materials == ()
    assert failure is not None
    assert failure.reason == "recent_material_unreadable"


def test_duplicate_recent_paths_create_one_task_material(tmp_path: Path) -> None:
    """模型重复填写同一路径时只生成一份任务级材料。"""

    material_file = tmp_path / "dup.txt"
    material_file.write_text("内容", encoding="utf-8")

    materials, failure = recent_materials_from_paths(
        {"recent_materials": [_recent_record(material_file)]},
        [str(material_file), str(material_file)],
    )

    assert failure is None
    assert len(materials) == 1


class AcceptingActivityExecutor:
    """记录 prepare 调用并模拟后台槽位接受任务。"""

    def __init__(self) -> None:
        """初始化空 prepare 调用记录。"""

        self.prepare_calls: list[dict[str, object]] = []

    def prepare(self, **kwargs: object) -> tuple[bool, list[dict[str, object]]]:
        """保存执行事实并返回唯一 Activity 编号。"""

        self.prepare_calls.append(dict(kwargs))
        return True, [
            {
                "type": "background_agent.prepared",
                "payload": {"activity_id": "background-a"},
            }
        ]


def _execute_start(
    executor: BackgroundAgentToolExecutor,
    arguments: dict[str, object],
    foreground_context: dict[str, object],
) -> dict[str, object]:
    """以普通通道执行一次派单调用。"""

    return executor.execute_start_background_work(
        execution_lane="ordinary",
        tool_call=ForegroundToolCall(
            tool_call_id="call-a",
            name="start_background_work",
            arguments=arguments,
        ),
        interaction_session_id="session-a",
        foreground_context=foreground_context,
        environment_snapshot=None,
        protocol_transaction_id="txn-a",
    )


def test_start_background_work_reattaches_all_valid_recent_materials(
    tmp_path: Path,
) -> None:
    """全部近期路径通过机械检查后才进入 canonical prepare。"""

    material_file = tmp_path / "app.py"
    material_file.write_text("内容", encoding="utf-8")
    activity_executor = AcceptingActivityExecutor()

    result = _execute_start(
        BackgroundAgentToolExecutor(activity_executor),  # type: ignore[arg-type]
        {
            "goal": "修改代码",
            "deliverable": True,
            "material_paths": [str(material_file)],
        },
        {"recent_materials": [_recent_record(material_file)]},
    )

    assert result["state"] == "accepted"
    assert result[RECENT_MATERIAL_OBSERVATION_FIELD] == {
        "outcome": "accepted",
        "paths": [str(material_file)],
    }
    materials = activity_executor.prepare_calls[0]["materials"]
    assert [material.local_path for material in materials] == [str(material_file)]


def test_any_invalid_recent_material_rejects_before_prepare(tmp_path: Path) -> None:
    """多项材料中任一失效时整单拒绝，Background prepare 保持零调用。"""

    valid = tmp_path / "valid.txt"
    valid.write_text("内容", encoding="utf-8")
    missing = tmp_path / "missing.txt"
    activity_executor = AcceptingActivityExecutor()

    result = _execute_start(
        BackgroundAgentToolExecutor(activity_executor),  # type: ignore[arg-type]
        {
            "goal": "合并材料",
            "deliverable": False,
            "material_paths": [str(valid), str(missing)],
        },
        {
            "recent_materials": [
                _recent_record(valid),
                _recent_record(missing),
            ]
        },
    )

    assert result["state"] == "rejected"
    assert result[RECENT_MATERIAL_OBSERVATION_FIELD] == {
        "outcome": "validation_failed",
        "paths": [],
    }
    assert result["material_failure"]["display_name"] == "missing.txt"
    assert "reason" not in result
    assert str(missing) not in str(result)
    assert activity_executor.prepare_calls == []


def test_current_turn_path_is_removed_before_recent_validation(tmp_path: Path) -> None:
    """本轮已自动附挂的路径即使误填参数，也不查询近期白名单。"""

    material_file = tmp_path / "same.txt"
    material_file.write_text("内容", encoding="utf-8")
    activity_executor = AcceptingActivityExecutor()
    foreground_context = {
        "active_turn_material_context": {
            "context_id": "context-a",
            "attachments": [
                {
                    "material_id": "material-current",
                    "display_name": "same.txt",
                    "kind": "dragged_file",
                    "path": str(material_file),
                    "node_kind": "file",
                    "media_class": "text",
                }
            ],
        },
        "recent_materials": [],
    }

    result = _execute_start(
        BackgroundAgentToolExecutor(activity_executor),  # type: ignore[arg-type]
        {
            "goal": "处理材料",
            "deliverable": True,
            "material_paths": [str(material_file)],
        },
        foreground_context,
    )

    assert result["state"] == "accepted"
    materials = activity_executor.prepare_calls[0]["materials"]
    assert [material.material_id for material in materials] == [
        "material-current"
    ]


def test_start_background_work_rejects_invalid_material_paths() -> None:
    """material_paths 结构非法时整单拒绝，不触碰后台槽位。"""

    activity_executor = AcceptingActivityExecutor()
    executor = BackgroundAgentToolExecutor(activity_executor)  # type: ignore[arg-type]
    too_many = [f"{index}.txt" for index in range(MAX_RECENT_MATERIAL_PATHS + 1)]

    for bad_value in ("not-a-list", ["valid.txt", "  "], too_many):
        result = _execute_start(
            executor,
            {"goal": "处理材料", "deliverable": True, "material_paths": bad_value},
            {},
        )
        assert result["state"] == "rejected"
        assert result["reason"] == "invalid_material_paths"
    assert activity_executor.prepare_calls == []


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
