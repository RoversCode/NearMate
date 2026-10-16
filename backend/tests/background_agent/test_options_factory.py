from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from threading import Event, Thread

import pytest
from claude_agent_sdk.types import HookMatcher

from companion.background_agent.contracts import AgentMaterial, AgentTask
from companion.background_agent.agent_session import AgentSession
from companion.background_agent.interaction_bridge import (
    AgentInteractionAnswer,
    InteractionBridge,
)
from companion.background_agent.model_config import AgentModelConfig
from companion.background_agent.options_factory import (
    build_agent_options,
    build_ask_user_question_updated_input,
)
from companion.background_agent.permission_gate import ActivityPermissionGate
from companion.background_agent.skill_registry import SkillExecutionSnapshot


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Junction 专属回归")
def test_sdk_environment_preserves_lexical_config_path_for_junction(
    tmp_path: Path,
) -> None:
    """SDK 环境不能提前解析 Junction，避免把越界目标伪装成可信身份。

    Args:
        tmp_path: Pytest 为本用例创建的独立临时目录。

    Returns:
        无返回值。
    """

    import _winapi

    external_root = tmp_path / "external-config"
    external_root.mkdir()
    lexical_config_root = tmp_path / "runtime" / "activities" / "activity-a"
    lexical_config_root.parent.mkdir(parents=True)
    _winapi.CreateJunction(str(external_root), str(lexical_config_root))
    try:
        environment = AgentModelConfig(
            environment={"ANTHROPIC_API_KEY": "test-key"},
            config_dir=lexical_config_root,
            model="claude-sonnet-4-5",
        ).to_sdk_environment()
    finally:
        lexical_config_root.rmdir()

    assert environment["CLAUDE_CONFIG_DIR"] == str(lexical_config_root.absolute())
    assert environment["CLAUDE_CONFIG_DIR"] != str(external_root.resolve())


def test_ask_user_question_answer_exposes_new_material_manifest(tmp_path: Path) -> None:
    """新增材料不仅要授权读取，还必须把名称和真实路径告诉 Claude。"""

    readable_path = tmp_path / "补充报告.pdf"
    readable_path.write_bytes(b"pdf")
    missing_path = tmp_path / "已失效附件.txt"
    questions = [
        {
            "question": "请提供补充文件。",
            "header": "补充文件",
            "options": [],
            "multiSelect": False,
        }
    ]
    answer = AgentInteractionAnswer(
        request_id="request-a",
        outcome="answer",
        answers={"请提供补充文件。": "请按附件继续。"},
        user_text="文件已附上，请重点检查利润变化。",
        materials=(
            AgentMaterial(
                material_id="material-readable",
                display_name="补充报告.pdf",
                material_kind="dragged_file",
                media_class="mixed_or_document",
                local_path=str(readable_path),
                node_kind="file",
            ),
            AgentMaterial(
                material_id="material-missing",
                display_name="已失效附件.txt",
                material_kind="dragged_file",
                media_class="text",
                local_path=str(missing_path),
                node_kind="file",
            ),
        ),
    )

    updated_input = build_ask_user_question_updated_input(
        questions=questions,
        answer=answer,
    )

    assert updated_input["questions"] == questions
    assert updated_input["answers"] == answer.answers
    response = str(updated_input["response"])
    assert "请提供补充文件。：请按附件继续。" in response
    assert "文件已附上，请重点检查利润变化。" in response
    assert "如果所需信息仍缺失或不明确，必须优先检查这些材料是否包含所需信息" in response
    assert "补充报告.pdf" in response
    assert json.dumps(str(readable_path.resolve()), ensure_ascii=False) in response
    assert "已失效附件.txt" in response
    assert "没有可读取的本地路径" in response


def test_ask_user_question_answer_without_materials_keeps_structured_shape() -> None:
    """没有新增材料时不额外注入 response，保持既有结构化答案契约。"""

    questions = [{"question": "是否继续？"}]
    answer = AgentInteractionAnswer(
        request_id="request-a",
        outcome="answer",
        answers={"是否继续？": "继续"},
        user_text="继续",
    )

    assert build_ask_user_question_updated_input(
        questions=questions,
        answer=answer,
    ) == {
        "questions": questions,
        "answers": {"是否继续？": "继续"},
    }


def test_build_agent_options_keeps_bash_behind_permission_callback(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    material = tmp_path / "material.txt"
    material.write_text("资料", encoding="utf-8")
    task = AgentTask(
        activity_id="activity-a",
        interaction_session_id="session-a",
        goal="转换音频",
        deliverable=True,
        requirements="输出 mp3",
        turn_materials=(
            AgentMaterial(
                material_id="material-a",
                display_name="material.txt",
                material_kind="dragged_file",
                media_class="text",
                local_path=str(material),
                node_kind="file",
            ),
        ),
        material_catalog=(
            AgentMaterial(
                material_id="material-a",
                display_name="material.txt",
                material_kind="dragged_file",
                media_class="text",
                local_path=str(material),
                node_kind="file",
            ),
        ),
        workspace_root=str(workspace),
        agent_config_dir=str(tmp_path / "runtime" / "activities" / "activity-a"),
        task_output_dir=str(workspace / "tasks" / "activity-a" / "outputs"),
        permission_mode="acceptEdits",
        skill_snapshot=SkillExecutionSnapshot(
            enabled_skill_ids=("ffmpeg-convert",),
            revision=1,
        ),
    )
    system_prompt = tmp_path / "system.md"
    system_prompt.write_text("规则", encoding="utf-8")

    interaction_bridge = InteractionBridge(
        activity_id=task.activity_id,
        request_notifier=lambda _request: None,
    )
    permission_gate = ActivityPermissionGate(
        interaction_bridge=interaction_bridge,
        resolution_notifier=lambda _request, _outcome, _suppress_notice: None,
    )
    session = AgentSession(workspace_root=workspace)
    turn_capability = session.bind_turn(
        activity_id=task.activity_id,
        bridge=interaction_bridge,
        permission_gate=permission_gate,
        material_catalog=task.material_catalog,
    )
    options = build_agent_options(
        task=task,
        model_config=AgentModelConfig(
            environment={"ANTHROPIC_API_KEY": "test-key", "IGNORED": "value"},
            config_dir=Path(task.agent_config_dir),
            model="claude-sonnet-4-5",
            effort_level="high",
        ),
        system_prompt_path=system_prompt,
        session=session,
        turn_capability=turn_capability,
        max_turns=0,
    )

    assert options.permission_mode == "acceptEdits"
    assert options.skills == ["ffmpeg-convert"]
    assert options.setting_sources == ["user"]
    assert "Skill" not in options.allowed_tools
    assert "Bash" not in options.allowed_tools
    assert options.can_use_tool is not None
    assert options.env["ANTHROPIC_API_KEY"] == "test-key"
    assert options.env["CLAUDE_CONFIG_DIR"] == str(
        Path(task.agent_config_dir).absolute()
    )
    assert options.model == "claude-sonnet-4-5"
    assert options.effort == "high"
    assert options.max_turns is None
    assert "IGNORED" not in options.env
    assert isinstance(options.hooks["PreToolUse"][0], HookMatcher)
    assert options.system_prompt == {
        "type": "preset",
        "preset": "claude_code",
        "append": "规则",
    }
    assert options.output_format == {
        "type": "json_schema",
        "schema": {
            "type": "object",
            "properties": {
                "display_content": {
                    "type": "string",
                    "minLength": 1,
                    "description": "给用户查看的完整成果正文。",
                },
                "delivery_brief": {
                    "type": "string",
                    "minLength": 1,
                    "description": "一至两句中立成果事实，不使用角色口吻。",
                },
            },
            "required": ["display_content", "delivery_brief"],
            "additionalProperties": False,
        },
    }
    assert options.enable_file_checkpointing is True
    assert options.extra_args == {"replay-user-messages": None}


def test_build_agent_options_preserves_explicit_full_access_mode(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    task = AgentTask(
        activity_id="activity-a",
        interaction_session_id="session-a",
        goal="执行任务",
        deliverable=False,
        requirements="",
        turn_materials=(),
        material_catalog=(),
        workspace_root=str(workspace),
        agent_config_dir=str(tmp_path / "runtime" / "activities" / "activity-a"),
        task_output_dir=str(workspace / "tasks" / "activity-a" / "outputs"),
        permission_mode="bypassPermissions",
    )
    prompt = tmp_path / "system.md"
    prompt.write_text("规则", encoding="utf-8")

    interaction_bridge = InteractionBridge(
        activity_id=task.activity_id,
        request_notifier=lambda _request: None,
    )
    permission_gate = ActivityPermissionGate(
        interaction_bridge=interaction_bridge,
        resolution_notifier=lambda _request, _outcome, _suppress_notice: None,
    )
    session = AgentSession(workspace_root=workspace)
    turn_capability = session.bind_turn(
        activity_id=task.activity_id,
        bridge=interaction_bridge,
        permission_gate=permission_gate,
        material_catalog=task.material_catalog,
    )
    options = build_agent_options(
        task=task,
        model_config=AgentModelConfig(
            environment={},
            config_dir=tmp_path / "runtime",
            model="claude-sonnet-4-5",
        ),
        system_prompt_path=prompt,
        session=session,
        turn_capability=turn_capability,
    )

    assert options.permission_mode == "bypassPermissions"
    assert options.skills == []


@pytest.mark.asyncio
async def test_file_hook_resolves_relative_paths_from_agent_workspace(tmp_path: Path) -> None:
    """Claude Code 的相对文件路径必须按任务工作区解释。"""

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    current_scene_path = workspace / "tasks" / "activity-a" / "inputs" / "current-scene.png"
    current_scene_path.parent.mkdir(parents=True)
    current_scene_path.write_bytes(b"frozen-scene")
    task = AgentTask(
        activity_id="activity-a",
        interaction_session_id="session-a",
        goal="读取工作区文件",
        deliverable=False,
        requirements="",
        turn_materials=(),
        material_catalog=(),
        workspace_root=str(workspace),
        agent_config_dir=str(tmp_path / "runtime" / "activities" / "activity-a"),
        task_output_dir=str(workspace / "tasks" / "activity-a" / "outputs"),
        permission_mode="acceptEdits",
        current_scene_path=str(current_scene_path),
    )
    prompt = tmp_path / "system.md"
    prompt.write_text("规则", encoding="utf-8")
    interaction_bridge = InteractionBridge(
        activity_id=task.activity_id,
        request_notifier=lambda _request: None,
    )
    permission_gate = ActivityPermissionGate(
        interaction_bridge=interaction_bridge,
        resolution_notifier=lambda _request, _outcome, _suppress_notice: None,
    )
    session = AgentSession(workspace_root=workspace)
    turn_capability = session.bind_turn(
        activity_id=task.activity_id,
        bridge=interaction_bridge,
        permission_gate=permission_gate,
        material_catalog=task.material_catalog,
        current_scene_path=str(current_scene_path),
    )
    options = build_agent_options(
        task=task,
        model_config=AgentModelConfig(
            environment={},
            config_dir=tmp_path / "runtime",
            model="claude-sonnet-4-5",
        ),
        system_prompt_path=prompt,
        session=session,
        turn_capability=turn_capability,
    )
    hook = options.hooks["PreToolUse"][0].hooks[0]

    allowed = await hook(
        {"tool_name": "Read", "tool_input": {"file_path": "tasks/input.txt"}},
        None,
        None,
    )
    denied = await hook(
        {"tool_name": "Read", "tool_input": {"file_path": "../outside.txt"}},
        None,
        None,
    )
    supplemental_material = tmp_path / "supplemental.txt"
    supplemental_material.write_text("补充资料", encoding="utf-8")
    session.extend_materials(
        (
            AgentMaterial(
                material_id="material-supplemental",
                display_name="supplemental.txt",
                material_kind="dragged_file",
                media_class="text",
                local_path=str(supplemental_material),
                node_kind="file",
            ),
        )
    )
    supplemental_allowed = await hook(
        {
            "tool_name": "Read",
            "tool_input": {"file_path": str(supplemental_material)},
        },
        None,
        None,
    )
    managed_config_denied = await hook(
        {
            "tool_name": "Write",
            "tool_input": {
                "file_path": str(Path(task.agent_config_dir) / "settings.json")
            },
        },
        None,
        None,
    )
    current_scene_read = await hook(
        {
            "tool_name": "Read",
            "tool_input": {"file_path": str(current_scene_path)},
        },
        None,
        None,
    )
    current_scene_edit_denied = await hook(
        {
            "tool_name": "Edit",
            "tool_input": {"file_path": str(current_scene_path)},
        },
        None,
        None,
    )

    assert allowed == {}
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert supplemental_allowed == {}
    assert managed_config_denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "超出了" in managed_config_denied["hookSpecificOutput"][
        "permissionDecisionReason"
    ]
    assert current_scene_read == {}
    assert current_scene_edit_denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "现场输入" in current_scene_edit_denied["hookSpecificOutput"][
        "permissionDecisionReason"
    ]


def _build_material_file_hook(
    tmp_path: Path,
    *,
    materials: tuple[AgentMaterial, ...],
    current_scene_path: Path | None = None,
) -> tuple[object, AgentSession, Path]:
    """构造只用于验证文件路径能力的真实 PreToolUse Hook。

    Args:
        tmp_path: 当前测试的临时目录。
        materials: 需要冻结到 AgentSession 的材料能力。
        current_scene_path: 可选的本轮截图路径。

    Returns:
        SDK Hook、持有能力的 AgentSession 和任务工作区。
    """

    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    prompt = tmp_path / "system.md"
    prompt.write_text("规则", encoding="utf-8")
    task = AgentTask(
        activity_id="activity-material-boundary",
        interaction_session_id="session-a",
        goal="验证材料路径能力",
        deliverable=False,
        requirements="",
        turn_materials=materials,
        material_catalog=materials,
        workspace_root=str(workspace),
        agent_config_dir=str(tmp_path / "runtime" / "activity-material-boundary"),
        task_output_dir=str(workspace / "tasks" / "activity-material-boundary" / "outputs"),
        permission_mode="acceptEdits",
        current_scene_path=(
            str(current_scene_path) if current_scene_path is not None else None
        ),
    )
    bridge = InteractionBridge(
        activity_id=task.activity_id,
        request_notifier=lambda _request: None,
    )
    session = AgentSession(workspace_root=workspace)
    turn_capability = session.bind_turn(
        activity_id=task.activity_id,
        bridge=bridge,
        permission_gate=ActivityPermissionGate(
            interaction_bridge=bridge,
            resolution_notifier=lambda _request, _outcome, _suppress_notice: None,
        ),
        material_catalog=materials,
        current_scene_path=(
            str(current_scene_path) if current_scene_path is not None else None
        ),
    )
    options = build_agent_options(
        task=task,
        model_config=AgentModelConfig(
            environment={},
            config_dir=tmp_path / "runtime",
            model="claude-sonnet-4-5",
        ),
        system_prompt_path=prompt,
        session=session,
        turn_capability=turn_capability,
    )
    return options.hooks["PreToolUse"][0].hooks[0], session, workspace


@pytest.mark.asyncio
async def test_file_capability_allows_exact_read_write_but_not_fake_child(
    tmp_path: Path,
) -> None:
    """普通文件默认可读写，但绝不能被当成目录根扩权。"""

    material_file = tmp_path / "app.py"
    material_file.write_text("print('hello')", encoding="utf-8")
    hook, _session, _workspace = _build_material_file_hook(
        tmp_path,
        materials=(
            AgentMaterial(
                material_id="material-file",
                display_name="app.py",
                material_kind="dragged_file",
                media_class="text",
                local_path=str(material_file),
                node_kind="file",
            ),
        ),
    )

    for tool_name, path_field in (
        ("Read", "file_path"),
        ("Write", "file_path"),
        ("Edit", "file_path"),
        ("NotebookEdit", "notebook_path"),
    ):
        assert await hook(
            {
                "tool_name": tool_name,
                "tool_input": {path_field: str(material_file)},
            },
            None,
            None,
        ) == {}

    denied = await hook(
        {
            "tool_name": "Write",
            "tool_input": {"file_path": str(material_file / "fake-child.txt")},
        },
        None,
        None,
    )

    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"


@pytest.mark.asyncio
async def test_directory_capability_allows_safe_tree_and_rejects_link_escape(
    tmp_path: Path,
) -> None:
    """目录材料允许当前安全树读写，但链接父链和递归扫描必须拒绝。"""

    material_directory = tmp_path / "project"
    material_directory.mkdir()
    source_file = material_directory / "main.py"
    source_file.write_text("print('hello')", encoding="utf-8")
    outside_directory = tmp_path / "outside"
    outside_directory.mkdir()
    (outside_directory / "secret.txt").write_text("secret", encoding="utf-8")
    linked_directory = material_directory / "linked"
    if sys.platform == "win32":
        import _winapi

        _winapi.CreateJunction(str(outside_directory), str(linked_directory))
    else:
        linked_directory.symlink_to(outside_directory, target_is_directory=True)

    try:
        hook, _session, _workspace = _build_material_file_hook(
            tmp_path,
            materials=(
                AgentMaterial(
                    material_id="material-directory",
                    display_name="project",
                    material_kind="dragged_folder",
                    media_class="mixed_or_document",
                    local_path=str(material_directory),
                    node_kind="directory",
                ),
            ),
        )

        assert await hook(
            {
                "tool_name": "Read",
                "tool_input": {"file_path": str(source_file)},
            },
            None,
            None,
        ) == {}
        assert await hook(
            {
                "tool_name": "Write",
                "tool_input": {"file_path": str(material_directory / "new.py")},
            },
            None,
            None,
        ) == {}
        linked_child_denied = await hook(
            {
                "tool_name": "Read",
                "tool_input": {"file_path": str(linked_directory / "secret.txt")},
            },
            None,
            None,
        )
        recursive_denied = await hook(
            {
                "tool_name": "Glob",
                "tool_input": {"path": str(material_directory), "pattern": "**/*"},
            },
            None,
            None,
        )

        assert linked_child_denied["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert recursive_denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    finally:
        if linked_directory.exists() or linked_directory.is_symlink():
            if sys.platform == "win32":
                linked_directory.rmdir()
            else:
                linked_directory.unlink()


@pytest.mark.asyncio
async def test_current_scene_hardlink_alias_remains_read_only(tmp_path: Path) -> None:
    """截图即使通过同一文件的硬链接别名访问，也只能读取不能写入。"""

    scene_path = tmp_path / "scene.png"
    scene_path.write_bytes(b"scene")
    hook, _session, workspace = _build_material_file_hook(
        tmp_path,
        materials=(),
        current_scene_path=scene_path,
    )
    scene_alias = workspace / "scene-alias.png"
    os.link(scene_path, scene_alias)

    assert await hook(
        {
            "tool_name": "Read",
            "tool_input": {"file_path": str(scene_alias)},
        },
        None,
        None,
    ) == {}
    denied = await hook(
        {
            "tool_name": "Write",
            "tool_input": {"file_path": str(scene_alias)},
        },
        None,
        None,
    )

    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "现场输入" in denied["hookSpecificOutput"]["permissionDecisionReason"]


@pytest.mark.asyncio
async def test_retired_turn_callbacks_cannot_use_new_turn_materials(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """旧回调即使与 Turn 切换交错，也不能读取 B 的材料能力。"""

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    prompt = tmp_path / "system.md"
    prompt.write_text("规则", encoding="utf-8")
    material_a = tmp_path / "material-a.txt"
    material_b = tmp_path / "material-b.txt"
    material_a.write_text("A", encoding="utf-8")
    material_b.write_text("B", encoding="utf-8")
    session = AgentSession(workspace_root=workspace)

    def build_turn(activity_id: str, material_path: Path):
        task = AgentTask(
            activity_id=activity_id,
            interaction_session_id="session-a",
            goal="执行任务",
            deliverable=False,
            requirements="",
            turn_materials=(
                AgentMaterial(
                    material_id=f"material-{activity_id}",
                    display_name=material_path.name,
                    material_kind="dragged_file",
                    media_class="text",
                    local_path=str(material_path),
                    node_kind="file",
                ),
            ),
            material_catalog=(
                AgentMaterial(
                    material_id=f"material-{activity_id}",
                    display_name=material_path.name,
                    material_kind="dragged_file",
                    media_class="text",
                    local_path=str(material_path),
                    node_kind="file",
                ),
            ),
            workspace_root=str(workspace),
            agent_config_dir=str(tmp_path / "runtime" / "activities" / activity_id),
            task_output_dir=str(workspace / "tasks" / activity_id / "outputs"),
            permission_mode="acceptEdits",
        )
        bridge = InteractionBridge(
            activity_id=activity_id,
            request_notifier=lambda _request: None,
        )
        gate = ActivityPermissionGate(
            interaction_bridge=bridge,
            resolution_notifier=lambda _request, _outcome, _suppress_notice: None,
        )
        turn_capability = session.bind_turn(
            activity_id=activity_id,
            bridge=bridge,
            permission_gate=gate,
            material_catalog=task.material_catalog,
        )
        return build_agent_options(
            task=task,
            model_config=AgentModelConfig(
                environment={},
                config_dir=tmp_path / "runtime",
                model="claude-sonnet-4-5",
            ),
            system_prompt_path=prompt,
            session=session,
            turn_capability=turn_capability,
        )

    options_a = build_turn("activity-a", material_a)
    old_hook = options_a.hooks["PreToolUse"][0].hooks[0]
    path_snapshot_entered = Event()
    release_path_snapshot = Event()
    original_capabilities = session._material_capability.capabilities

    def pause_material_capability_snapshot() -> tuple[AgentMaterial, ...]:
        """暂停 A 的能力快照，稳定制造 token 校验与 B 绑定的交错。"""

        path_snapshot_entered.set()
        if not release_path_snapshot.wait(timeout=2.0):
            raise RuntimeError("测试没有释放 A 的材料路径快照")
        return original_capabilities()

    monkeypatch.setattr(
        session._material_capability,
        "capabilities",
        pause_material_capability_snapshot,
    )
    old_hook_result: dict[str, object] = {}

    def run_old_hook() -> None:
        """在独立事件循环执行 A 的迟到 Hook。"""

        old_hook_result["value"] = asyncio.run(
            old_hook(
                {"tool_name": "Read", "tool_input": {"file_path": str(material_b)}},
                "first-late-id",
                None,
            )
        )

    old_hook_thread = Thread(target=run_old_hook)
    old_hook_thread.start()
    assert path_snapshot_entered.wait(timeout=2.0) is True

    options_b_holder: dict[str, object] = {}
    turn_b_bound = Event()

    def bind_turn_b() -> None:
        """尝试绑定 B；A 的授权快照完成前必须被 Session 状态锁挡住。"""

        options_b_holder["value"] = build_turn("activity-b", material_b)
        turn_b_bound.set()

    bind_turn_b_thread = Thread(target=bind_turn_b)
    bind_turn_b_thread.start()
    assert turn_b_bound.wait(timeout=0.1) is False
    release_path_snapshot.set()
    old_hook_thread.join(timeout=2.0)
    bind_turn_b_thread.join(timeout=2.0)
    assert old_hook_thread.is_alive() is False
    assert bind_turn_b_thread.is_alive() is False
    assert turn_b_bound.is_set() is True

    options_b = options_b_holder["value"]
    old_permission = await options_a.can_use_tool(
        "Bash",
        {},
        type("Context", (), {"tool_use_id": "first-late-id"})(),
    )
    old_question = await options_a.can_use_tool(
        "AskUserQuestion",
        {"questions": []},
        type("Context", (), {"tool_use_id": "another-late-id"})(),
    )
    old_material_result = old_hook_result["value"]
    new_hook = options_b.hooks["PreToolUse"][0].hooks[0]
    new_material_result = await new_hook(
        {"tool_name": "Read", "tool_input": {"file_path": str(material_b)}},
        "new-id",
        None,
    )

    assert old_permission.interrupt is True
    assert old_question.interrupt is True
    assert old_material_result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert new_material_result == {}
