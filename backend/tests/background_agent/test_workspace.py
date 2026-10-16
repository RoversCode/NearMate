from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from companion.background_agent import workspace as workspace_module
from companion.background_agent.skill_asset_safety import SkillAssetBoundaryError
from companion.background_agent.workspace import WorkspaceManager
from companion.background_agent.skill_registry import SkillExecutionSnapshot


def _prepare_and_materialize(
    manager: WorkspaceManager,
    activity_id: str,
    *,
    agent_config_base: Path,
    skill_snapshot: SkillExecutionSnapshot | None = None,
):
    """按新生命周期创建任务目录，再原子物化启动配置。

    Args:
        manager: 被测工作区管理器。
        activity_id: 当前测试 Activity ID。
        agent_config_base: 私有 Agent 配置根目录。
        skill_snapshot: 启动时最终裁决的 Skill 快照。

    Returns:
        已经完成配置物化的任务路径。
    """

    task_paths = manager.prepare_task(
        activity_id,
        agent_config_base=agent_config_base,
    )
    manager.materialize_agent_config(
        task_paths,
        skill_snapshot=skill_snapshot or SkillExecutionSnapshot(),
    )
    return task_paths


def test_prepare_task_removes_partially_created_task_directory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """输出目录创建失败时，不得遗留只有一半结构的任务目录。

    Args:
        monkeypatch: Pytest 提供的临时替换工具。
        tmp_path: Pytest 为本用例创建的独立临时目录。

    Returns:
        无返回值。
    """

    workspace_root = tmp_path / "workspace"
    manager = WorkspaceManager(
        asset_root=tmp_path / "assets",
        workspace_root=workspace_root,
    )
    task_root = workspace_root / "tasks" / "partially-created"
    output_root = task_root / "outputs"
    original_mkdir = Path.mkdir

    def fail_output_directory_creation(
        path: Path,
        mode: int = 0o777,
        parents: bool = False,
        exist_ok: bool = False,
    ) -> None:
        """只让目标任务的输出目录创建失败。

        Args:
            path: 本次准备创建的目录。
            mode: 目录权限位。
            parents: 是否同时创建父目录。
            exist_ok: 目录存在时是否忽略。

        Returns:
            无返回值。

        Raises:
            OSError: 创建目标输出目录时模拟磁盘异常。
        """

        if path == output_root:
            raise OSError("simulated output directory failure")
        original_mkdir(path, mode=mode, parents=parents, exist_ok=exist_ok)

    monkeypatch.setattr(Path, "mkdir", fail_output_directory_creation)

    with pytest.raises(OSError, match="simulated output directory failure"):
        manager.prepare_task(
            "partially-created",
            agent_config_base=tmp_path / "runtime",
        )

    assert not task_root.exists()


def test_prepare_tasks_keep_independent_agent_config_snapshots(
    tmp_path: Path,
) -> None:
    """两项任务必须各自保留冻结配置，后准备者不能覆盖前一项。"""

    asset_root = tmp_path / "assets"
    managed_asset_root = asset_root / ".claude"
    managed_asset_root.mkdir(parents=True)
    baseline_settings = {"permissions": {"allow": [], "deny": []}}
    (managed_asset_root / "settings.json").write_text(
        json.dumps(baseline_settings),
        encoding="utf-8",
    )
    workspace_root = tmp_path / "workspace"
    manager = WorkspaceManager(
        asset_root=asset_root,
        workspace_root=workspace_root,
    )
    agent_config_base = tmp_path / "runtime"

    activity_a_paths = _prepare_and_materialize(
        manager,
        "activity-a",
        agent_config_base=agent_config_base,
    )
    frozen_scene_bytes = b"\x89PNG\r\n\x1a\nproduction-scene"
    current_scene_path = manager.write_current_scene(
        activity_a_paths,
        frozen_scene_bytes,
    )
    activity_a_settings_path = activity_a_paths.agent_config_root / "settings.json"
    activity_a_settings_path.write_text(
        json.dumps({"permissions": {"allow": ["Bash(*)"]}}),
        encoding="utf-8",
    )
    (activity_a_paths.agent_config_root / "injected.md").write_text(
        "不可信残留",
        encoding="utf-8",
    )

    activity_b_paths = _prepare_and_materialize(
        manager,
        "activity-b",
        agent_config_base=agent_config_base,
    )

    # 后一项任务只读取产品基线；前一项已经冻结的目录必须保持原样，直到它结束。
    assert activity_a_paths.agent_config_root != activity_b_paths.agent_config_root
    assert json.loads(activity_a_settings_path.read_text(encoding="utf-8")) == {
        "permissions": {"allow": ["Bash(*)"]}
    }
    assert (activity_a_paths.agent_config_root / "injected.md").is_file()
    assert json.loads(
        (activity_b_paths.agent_config_root / "settings.json").read_text(
            encoding="utf-8"
        )
    ) == baseline_settings
    assert not (activity_b_paths.agent_config_root / "injected.md").exists()
    assert not (workspace_root / ".claude").exists()
    assert (workspace_root / "tasks" / "activity-a" / "inputs").is_dir()
    assert (workspace_root / "tasks" / "activity-a" / "outputs").is_dir()
    assert current_scene_path.name == "current-scene.png"
    assert current_scene_path.read_bytes() == frozen_scene_bytes
    assert (workspace_root / "tasks" / "activity-b" / "inputs").is_dir()
    assert (workspace_root / "tasks" / "activity-b" / "outputs").is_dir()


def test_prepare_task_only_materializes_skills_in_frozen_snapshot(
    tmp_path: Path,
) -> None:
    """工作区只能出现任务快照明确允许的产品 Skill。"""

    asset_root = tmp_path / "assets"
    for skill_id in ("enabled-skill", "disabled-skill"):
        skill_root = asset_root / ".claude" / "skills" / skill_id
        skill_root.mkdir(parents=True)
        (skill_root / "SKILL.md").write_text(
            (
                "---\n"
                f'name: "{skill_id}"\n'
                "description: 工作区测试 Skill。\n"
                "---\n\n"
                "说明"
            ),
            encoding="utf-8",
        )
        (skill_root / "manifest.json").write_text(
            json.dumps(
                {
                    "id": skill_id,
                    "name": skill_id,
                    "summary": "测试 Skill",
                    "category": "测试",
                    "version": "1.0.0",
                    "default_enabled": True,
                    "dependencies": [],
                }
            ),
            encoding="utf-8",
        )
    workspace_root = tmp_path / "workspace"
    manager = WorkspaceManager(asset_root=asset_root, workspace_root=workspace_root)

    task_paths = manager.prepare_task(
        "activity-a",
        agent_config_base=tmp_path / "runtime",
    )
    assert not task_paths.agent_config_root.exists()
    manager.materialize_agent_config(
        task_paths,
        skill_snapshot=SkillExecutionSnapshot(
            enabled_skill_ids=("enabled-skill",),
            revision=1,
        ),
    )

    managed_skills_root = task_paths.agent_config_root / "skills"
    assert (managed_skills_root / "enabled-skill" / "SKILL.md").is_file()
    assert not (managed_skills_root / "disabled-skill").exists()


def test_prepare_task_omits_generated_python_cache_from_skill_assets(
    tmp_path: Path,
) -> None:
    """任务工作区不能把开发机生成的 Python 字节码当成产品 Skill 资产。"""

    asset_root = tmp_path / "assets"
    skill_root = asset_root / ".claude" / "skills" / "enabled-skill"
    cache_root = skill_root / "scripts" / "__pycache__"
    cache_root.mkdir(parents=True)
    (skill_root / "SKILL.md").write_text(
        "---\nname: enabled-skill\ndescription: 工作区测试 Skill。\n---\n\n说明",
        encoding="utf-8",
    )
    (skill_root / "manifest.json").write_text(
        json.dumps(
            {
                "id": "enabled-skill",
                "name": "测试 Skill",
                "summary": "验证缓存排除。",
                "category": "测试",
                "version": "1.0.0",
                "default_enabled": True,
                "dependencies": [],
            }
        ),
        encoding="utf-8",
    )
    (cache_root / "helper.pyc").write_bytes(b"cache")
    workspace_root = tmp_path / "workspace"
    manager = WorkspaceManager(asset_root=asset_root, workspace_root=workspace_root)

    task_paths = manager.prepare_task(
        "activity-a",
        agent_config_base=tmp_path / "runtime",
    )
    manager.materialize_agent_config(
        task_paths,
        skill_snapshot=SkillExecutionSnapshot(
            enabled_skill_ids=("enabled-skill",),
            revision=1,
        ),
    )

    copied_skill_root = task_paths.agent_config_root / "skills" / "enabled-skill"
    assert (copied_skill_root / "SKILL.md").is_file()
    assert not (copied_skill_root / "scripts" / "__pycache__").exists()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Junction 专属回归")
def test_prepare_task_keeps_existing_config_when_skill_contains_junction(
    tmp_path: Path,
) -> None:
    """复制前发现 Skill 内部 Junction 时，不得影响另一项已冻结配置。"""

    import _winapi

    asset_root = tmp_path / "assets"
    _write_skill(asset_root, "enabled-skill")
    workspace_root = tmp_path / "workspace"
    manager = WorkspaceManager(asset_root=asset_root, workspace_root=workspace_root)
    snapshot = SkillExecutionSnapshot(
        enabled_skill_ids=("enabled-skill",),
        revision=1,
    )
    agent_config_base = tmp_path / "runtime"
    activity_a_paths = _prepare_and_materialize(
        manager,
        "activity-a",
        agent_config_base=agent_config_base,
        skill_snapshot=snapshot,
    )
    existing_instruction = (
        activity_a_paths.agent_config_root
        / "skills"
        / "enabled-skill"
        / "SKILL.md"
    ).read_text(encoding="utf-8")

    external_root = tmp_path / "external"
    external_root.mkdir()
    (external_root / "secret.txt").write_text("outside", encoding="utf-8")
    _winapi.CreateJunction(
        str(external_root),
        str(asset_root / ".claude" / "skills" / "enabled-skill" / "external"),
    )

    with pytest.raises(SkillAssetBoundaryError):
        activity_b_paths = manager.prepare_task(
            "activity-b",
            agent_config_base=agent_config_base,
        )
        manager.materialize_agent_config(
            activity_b_paths,
            skill_snapshot=snapshot,
        )
    manager.discard_unstarted_task(
        "activity-b",
        agent_config_root=agent_config_base / "activities" / "activity-b",
    )

    copied_skill_root = activity_a_paths.agent_config_root / "skills" / "enabled-skill"
    assert (copied_skill_root / "SKILL.md").read_text(encoding="utf-8") == existing_instruction
    assert not (copied_skill_root / "external").exists()
    assert not list((agent_config_base / "activities").glob("*.tmp"))
    assert not (workspace_root / "tasks" / "activity-b").exists()


def test_prepare_task_keeps_existing_config_when_skill_changes_during_copy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """复制期间源 Skill 被改写时，只回滚新任务并保留已冻结配置。"""

    asset_root = tmp_path / "assets"
    _write_skill(asset_root, "enabled-skill")
    workspace_root = tmp_path / "workspace"
    manager = WorkspaceManager(asset_root=asset_root, workspace_root=workspace_root)
    snapshot = SkillExecutionSnapshot(
        enabled_skill_ids=("enabled-skill",),
        revision=1,
    )
    agent_config_base = tmp_path / "runtime"
    activity_a_paths = _prepare_and_materialize(
        manager,
        "activity-a",
        agent_config_base=agent_config_base,
        skill_snapshot=snapshot,
    )
    copied_instruction_path = (
        activity_a_paths.agent_config_root
        / "skills"
        / "enabled-skill"
        / "SKILL.md"
    )
    existing_instruction = copied_instruction_path.read_text(encoding="utf-8")
    source_skill_root = asset_root / ".claude" / "skills" / "enabled-skill"
    original_copytree = workspace_module.shutil.copytree

    def copy_then_change_source(
        source: str | Path,
        destination: str | Path,
        **kwargs: object,
    ) -> Path:
        """完成真实复制后改写源说明，制造确定性的复制竞态。

        Args:
            source: copytree 正在读取的源目录。
            destination: copytree 准备写入的临时目录。
            **kwargs: 生产代码传入的忽略规则等参数。

        Returns:
            真实 copytree 创建的目标路径。
        """

        copied_path = original_copytree(source, destination, **kwargs)
        if Path(source) == source_skill_root:
            (source_skill_root / "SKILL.md").write_text(
                "---\nname: enabled-skill\ndescription: 已被替换。\n---\n\n新说明",
                encoding="utf-8",
            )
        return copied_path

    monkeypatch.setattr(workspace_module.shutil, "copytree", copy_then_change_source)

    with pytest.raises(SkillAssetBoundaryError):
        activity_b_paths = manager.prepare_task(
            "activity-b",
            agent_config_base=agent_config_base,
        )
        manager.materialize_agent_config(
            activity_b_paths,
            skill_snapshot=snapshot,
        )
    manager.discard_unstarted_task(
        "activity-b",
        agent_config_root=agent_config_base / "activities" / "activity-b",
    )

    assert copied_instruction_path.read_text(encoding="utf-8") == existing_instruction
    assert not list((agent_config_base / "activities").glob("*.tmp"))
    assert not (workspace_root / "tasks" / "activity-b").exists()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Junction 专属回归")
def test_prepare_task_keeps_existing_config_when_fixed_asset_contains_junction(
    tmp_path: Path,
) -> None:
    """固定资产含 Junction 时，不得复制外部配置或改写已冻结任务。

    Args:
        tmp_path: Pytest 为本用例创建的独立临时目录。

    Returns:
        无返回值。
    """

    import _winapi

    asset_root = tmp_path / "assets"
    product_claude_root = asset_root / ".claude"
    product_claude_root.mkdir(parents=True)
    (product_claude_root / "settings.json").write_text(
        '{"source":"managed"}',
        encoding="utf-8",
    )
    workspace_root = tmp_path / "workspace"
    manager = WorkspaceManager(asset_root=asset_root, workspace_root=workspace_root)
    agent_config_base = tmp_path / "runtime"
    activity_a_paths = _prepare_and_materialize(
        manager,
        "activity-a",
        agent_config_base=agent_config_base,
    )

    external_root = tmp_path / "external-fixed-assets"
    external_root.mkdir()
    (external_root / "injected.md").write_text("outside", encoding="utf-8")
    _winapi.CreateJunction(
        str(external_root),
        str(product_claude_root / "rules"),
    )

    with pytest.raises(SkillAssetBoundaryError):
        activity_b_paths = manager.prepare_task(
            "activity-b",
            agent_config_base=agent_config_base,
        )
        manager.materialize_agent_config(
            activity_b_paths,
            skill_snapshot=SkillExecutionSnapshot(),
        )
    manager.discard_unstarted_task(
        "activity-b",
        agent_config_root=agent_config_base / "activities" / "activity-b",
    )

    assert (activity_a_paths.agent_config_root / "settings.json").read_text(encoding="utf-8") == (
        '{"source":"managed"}'
    )
    assert not (activity_a_paths.agent_config_root / "rules").exists()
    assert not list((agent_config_base / "activities").glob("*.tmp"))
    assert not (workspace_root / "tasks" / "activity-b").exists()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Junction 专属回归")
def test_prepare_task_keeps_existing_config_when_product_asset_root_is_junction(
    tmp_path: Path,
) -> None:
    """产品资产根变成 Junction 时，不得把外部目录复制进新任务。

    Args:
        tmp_path: Pytest 为本用例创建的独立临时目录。

    Returns:
        无返回值。
    """

    import _winapi

    asset_root = tmp_path / "assets"
    product_claude_root = asset_root / ".claude"
    product_claude_root.mkdir(parents=True)
    (product_claude_root / "settings.json").write_text(
        '{"source":"managed"}',
        encoding="utf-8",
    )
    workspace_root = tmp_path / "workspace"
    manager = WorkspaceManager(asset_root=asset_root, workspace_root=workspace_root)
    agent_config_base = tmp_path / "runtime"
    activity_a_paths = _prepare_and_materialize(
        manager,
        "activity-a",
        agent_config_base=agent_config_base,
    )

    workspace_module.shutil.rmtree(product_claude_root)
    external_root = tmp_path / "external-claude-root"
    external_root.mkdir()
    (external_root / "settings.json").write_text(
        '{"source":"outside"}',
        encoding="utf-8",
    )
    _winapi.CreateJunction(str(external_root), str(product_claude_root))

    with pytest.raises(SkillAssetBoundaryError):
        activity_b_paths = manager.prepare_task(
            "activity-b",
            agent_config_base=agent_config_base,
        )
        manager.materialize_agent_config(
            activity_b_paths,
            skill_snapshot=SkillExecutionSnapshot(),
        )
    manager.discard_unstarted_task(
        "activity-b",
        agent_config_root=agent_config_base / "activities" / "activity-b",
    )

    copied_settings = activity_a_paths.agent_config_root / "settings.json"
    assert copied_settings.read_text(encoding="utf-8") == '{"source":"managed"}'
    assert not list((agent_config_base / "activities").glob("*.tmp"))
    assert not (workspace_root / "tasks" / "activity-b").exists()


def test_prepare_task_keeps_existing_config_when_new_config_commit_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """新配置提交失败时，已冻结配置必须保留且新任务完全回滚。

    Args:
        monkeypatch: Pytest 提供的临时替换工具。
        tmp_path: Pytest 为本用例创建的独立临时目录。

    Returns:
        无返回值。
    """

    asset_root = tmp_path / "assets"
    product_claude_root = asset_root / ".claude"
    product_claude_root.mkdir(parents=True)
    source_settings = product_claude_root / "settings.json"
    source_settings.write_text('{"revision":1}', encoding="utf-8")
    workspace_root = tmp_path / "workspace"
    manager = WorkspaceManager(asset_root=asset_root, workspace_root=workspace_root)
    agent_config_base = tmp_path / "runtime"
    activity_a_paths = _prepare_and_materialize(
        manager,
        "activity-a",
        agent_config_base=agent_config_base,
    )
    source_settings.write_text('{"revision":2}', encoding="utf-8")
    original_replace = Path.replace

    def fail_temporary_commit(source: Path, target: str | Path) -> Path:
        """只让 activity-b 的临时配置提交失败。

        Args:
            source: 准备移动的源路径。
            target: 准备替换的目标路径。

        Returns:
            非目标调用的真实替换结果。

        Raises:
            OSError: 临时目录准备提交为 activity-b 配置时抛出。
        """

        if (
            source.name.endswith(".tmp")
            and Path(target) == agent_config_base / "activities" / "activity-b"
        ):
            raise OSError("simulated final replace failure")
        return original_replace(source, target)

    monkeypatch.setattr(Path, "replace", fail_temporary_commit)

    with pytest.raises(OSError, match="simulated final replace failure"):
        activity_b_paths = manager.prepare_task(
            "activity-b",
            agent_config_base=agent_config_base,
        )
        manager.materialize_agent_config(
            activity_b_paths,
            skill_snapshot=SkillExecutionSnapshot(),
        )
    manager.discard_unstarted_task(
        "activity-b",
        agent_config_root=agent_config_base / "activities" / "activity-b",
    )

    copied_settings = activity_a_paths.agent_config_root / "settings.json"
    assert copied_settings.read_text(encoding="utf-8") == '{"revision":1}'
    assert not list((agent_config_base / "activities").glob("*.tmp"))
    assert not (agent_config_base / "activities" / "activity-b").exists()
    assert not (workspace_root / "tasks" / "activity-b").exists()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Junction 专属回归")
def test_prepare_task_rejects_existing_config_junction_without_deleting_target(
    tmp_path: Path,
) -> None:
    """目标配置是 Junction 时必须拒绝，且不能递归删除外部目标。

    Args:
        tmp_path: Pytest 为本用例创建的独立临时目录。

    Returns:
        无返回值。
    """

    import _winapi

    asset_root = tmp_path / "assets"
    product_claude_root = asset_root / ".claude"
    product_claude_root.mkdir(parents=True)
    (product_claude_root / "settings.json").write_text(
        '{"source":"managed"}',
        encoding="utf-8",
    )
    workspace_root = tmp_path / "workspace"
    agent_config_base = tmp_path / "runtime"
    agent_config_activities_root = agent_config_base / "activities"
    agent_config_activities_root.mkdir(parents=True)
    external_root = tmp_path / "external-agent-config"
    external_root.mkdir()
    external_file = external_root / "must-survive.txt"
    external_file.write_text("outside", encoding="utf-8")
    _winapi.CreateJunction(
        str(external_root),
        str(agent_config_activities_root / "activity-a"),
    )

    manager = WorkspaceManager(asset_root=asset_root, workspace_root=workspace_root)
    with pytest.raises(FileExistsError):
        manager.prepare_task(
            "activity-a",
            agent_config_base=agent_config_base,
        )

    assert external_file.read_text(encoding="utf-8") == "outside"
    assert (agent_config_activities_root / "activity-a").is_junction()
    assert not (workspace_root / "tasks" / "activity-a").exists()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Junction 专属回归")
def test_validate_prepared_task_rejects_config_replaced_by_junction(
    tmp_path: Path,
) -> None:
    """普通任务 prepare 后配置根被替换时，启动复核必须拒绝越界。

    Args:
        tmp_path: Pytest 为本用例创建的独立临时目录。

    Returns:
        无返回值。
    """

    import _winapi

    manager = WorkspaceManager(
        asset_root=tmp_path / "assets",
        workspace_root=tmp_path / "workspace",
    )
    task_paths = _prepare_and_materialize(
        manager,
        "activity-a",
        agent_config_base=tmp_path / "runtime",
    )
    external_root = tmp_path / "external-config"
    external_root.mkdir()
    external_file = external_root / "must-not-load.txt"
    external_file.write_text("outside", encoding="utf-8")
    workspace_module.shutil.rmtree(task_paths.agent_config_root)
    _winapi.CreateJunction(
        str(external_root),
        str(task_paths.agent_config_root),
    )

    try:
        with pytest.raises(RuntimeError, match="私有 Agent 配置"):
            manager.validate_prepared_task(
                "activity-a",
                agent_config_root=task_paths.agent_config_root,
            )
    finally:
        if task_paths.agent_config_root.is_junction():
            task_paths.agent_config_root.rmdir()

    assert external_file.read_text(encoding="utf-8") == "outside"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Junction 专属回归")
def test_validate_correction_rejects_inherited_config_replaced_by_junction(
    tmp_path: Path,
) -> None:
    """纠正任务继承的父配置被替换时，也必须在 Runner 启动前拒绝。

    Args:
        tmp_path: Pytest 为本用例创建的独立临时目录。

    Returns:
        无返回值。
    """

    import _winapi

    manager = WorkspaceManager(
        asset_root=tmp_path / "assets",
        workspace_root=tmp_path / "workspace",
    )
    parent_paths = manager.prepare_task(
        "activity-parent",
        agent_config_base=tmp_path / "runtime",
    )
    manager.materialize_agent_config(
        parent_paths,
        skill_snapshot=SkillExecutionSnapshot(),
    )
    correction_paths = manager.prepare_correction_task(
        "activity-correction",
        agent_config_root=parent_paths.agent_config_root,
    )
    external_root = tmp_path / "external-correction-config"
    external_root.mkdir()
    external_file = external_root / "must-not-load.txt"
    external_file.write_text("outside", encoding="utf-8")
    workspace_module.shutil.rmtree(parent_paths.agent_config_root)
    _winapi.CreateJunction(
        str(external_root),
        str(parent_paths.agent_config_root),
    )

    try:
        with pytest.raises(RuntimeError, match="私有 Agent 配置"):
            manager.validate_prepared_task(
                "activity-correction",
                agent_config_root=correction_paths.agent_config_root,
            )
    finally:
        if parent_paths.agent_config_root.is_junction():
            parent_paths.agent_config_root.rmdir()

    assert external_file.read_text(encoding="utf-8") == "outside"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Junction 专属回归")
def test_discard_unstarted_task_does_not_follow_replaced_activities_junction(
    tmp_path: Path,
) -> None:
    """未启动清理不能沿被替换的 activities 父目录删除外部同名目录。

    Args:
        tmp_path: Pytest 为本用例创建的独立临时目录。

    Returns:
        无返回值。
    """

    import _winapi

    manager = WorkspaceManager(
        asset_root=tmp_path / "assets",
        workspace_root=tmp_path / "workspace",
    )
    task_paths = _prepare_and_materialize(
        manager,
        "activity-a",
        agent_config_base=tmp_path / "runtime",
    )
    agent_config_activities_root = task_paths.agent_config_root.parent
    external_activities_root = tmp_path / "external-activities"
    external_activity_root = external_activities_root / "activity-a"
    external_activity_root.mkdir(parents=True)
    external_file = external_activity_root / "must-survive.txt"
    external_file.write_text("outside", encoding="utf-8")
    workspace_module.shutil.rmtree(agent_config_activities_root)
    _winapi.CreateJunction(
        str(external_activities_root),
        str(agent_config_activities_root),
    )

    try:
        removed = manager.discard_unstarted_task(
            "activity-a",
            agent_config_root=task_paths.agent_config_root,
        )
    finally:
        if agent_config_activities_root.is_junction():
            agent_config_activities_root.rmdir()

    assert removed is True
    assert not task_paths.task_root.exists()
    assert external_file.read_text(encoding="utf-8") == "outside"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Junction 专属回归")
def test_prepare_task_rejects_tasks_parent_junction_without_writing_target(
    tmp_path: Path,
) -> None:
    """任务父目录已被重定向时，prepare 不得在外部目录创建任务。

    Args:
        tmp_path: Pytest 为本用例创建的独立临时目录。

    Returns:
        无返回值。
    """

    import _winapi

    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    tasks_root = workspace_root / "tasks"
    external_tasks_root = tmp_path / "external-tasks-for-prepare"
    external_tasks_root.mkdir()
    external_file = external_tasks_root / "must-survive.txt"
    external_file.write_text("outside", encoding="utf-8")
    _winapi.CreateJunction(str(external_tasks_root), str(tasks_root))
    manager = WorkspaceManager(
        asset_root=tmp_path / "assets",
        workspace_root=workspace_root,
    )

    try:
        with pytest.raises(RuntimeError, match="任务目录"):
            manager.prepare_task(
                "activity-a",
                agent_config_base=tmp_path / "runtime",
            )
    finally:
        if tasks_root.is_junction():
            tasks_root.rmdir()

    assert external_file.read_text(encoding="utf-8") == "outside"
    assert not (external_tasks_root / "activity-a").exists()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Junction 专属回归")
def test_prepare_task_rejects_workspace_root_junction_without_writing_target(
    tmp_path: Path,
) -> None:
    """业务工作区根已被重定向时，prepare 不得在外部目录创建任务。

    Args:
        tmp_path: Pytest 为本用例创建的独立临时目录。

    Returns:
        无返回值。
    """

    import _winapi

    workspace_root = tmp_path / "workspace"
    external_workspace_root = tmp_path / "external-workspace-for-prepare"
    external_workspace_root.mkdir()
    external_file = external_workspace_root / "must-survive.txt"
    external_file.write_text("outside", encoding="utf-8")
    _winapi.CreateJunction(str(external_workspace_root), str(workspace_root))
    manager = WorkspaceManager(
        asset_root=tmp_path / "assets",
        workspace_root=workspace_root,
    )

    try:
        with pytest.raises(RuntimeError, match="业务工作区"):
            manager.prepare_task(
                "activity-a",
                agent_config_base=tmp_path / "runtime",
            )
    finally:
        if workspace_root.is_junction():
            workspace_root.rmdir()

    assert external_file.read_text(encoding="utf-8") == "outside"
    assert not (external_workspace_root / "tasks").exists()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Junction 专属回归")
def test_validate_prepared_task_rejects_replaced_tasks_parent_junction(
    tmp_path: Path,
) -> None:
    """prepare 后任务父目录被重定向时，启动复核必须拒绝越界。

    Args:
        tmp_path: Pytest 为本用例创建的独立临时目录。

    Returns:
        无返回值。
    """

    import _winapi

    workspace_root = tmp_path / "workspace"
    manager = WorkspaceManager(
        asset_root=tmp_path / "assets",
        workspace_root=workspace_root,
    )
    task_paths = manager.prepare_task(
        "activity-a",
        agent_config_base=tmp_path / "runtime",
    )
    tasks_root = task_paths.task_root.parent
    external_task_root = tmp_path / "external-tasks-for-validation" / "activity-a"
    (external_task_root / "inputs").mkdir(parents=True)
    (external_task_root / "outputs").mkdir()
    external_file = external_task_root / "must-survive.txt"
    external_file.write_text("outside", encoding="utf-8")
    workspace_module.shutil.rmtree(tasks_root)
    _winapi.CreateJunction(str(external_task_root.parent), str(tasks_root))

    try:
        with pytest.raises(RuntimeError, match="任务目录"):
            manager.validate_prepared_task(
                "activity-a",
                agent_config_root=task_paths.agent_config_root,
            )
    finally:
        if tasks_root.is_junction():
            tasks_root.rmdir()

    assert external_file.read_text(encoding="utf-8") == "outside"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Junction 专属回归")
def test_discard_unstarted_task_does_not_follow_replaced_workspace_junction(
    tmp_path: Path,
) -> None:
    """工作区根被替换后，未启动清理不能删除外部同名任务目录。

    Args:
        tmp_path: Pytest 为本用例创建的独立临时目录。

    Returns:
        无返回值。
    """

    import _winapi

    workspace_root = tmp_path / "workspace"
    manager = WorkspaceManager(
        asset_root=tmp_path / "assets",
        workspace_root=workspace_root,
    )
    task_paths = _prepare_and_materialize(
        manager,
        "activity-a",
        agent_config_base=tmp_path / "runtime",
    )
    external_task_root = (
        tmp_path / "external-workspace-for-cleanup" / "tasks" / "activity-a"
    )
    external_task_root.mkdir(parents=True)
    external_file = external_task_root / "must-survive.txt"
    external_file.write_text("outside", encoding="utf-8")
    workspace_module.shutil.rmtree(workspace_root)
    _winapi.CreateJunction(str(external_task_root.parent.parent), str(workspace_root))

    try:
        removed = manager.discard_unstarted_task(
            "activity-a",
            agent_config_root=task_paths.agent_config_root,
        )
    finally:
        if workspace_root.is_junction():
            workspace_root.rmdir()

    assert removed is True
    assert not task_paths.agent_config_root.exists()
    assert external_file.read_text(encoding="utf-8") == "outside"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Junction 专属回归")
def test_discard_unstarted_task_does_not_follow_replaced_tasks_junction(
    tmp_path: Path,
) -> None:
    """未启动清理不能沿被替换的 tasks 父目录删除外部同名目录。

    Args:
        tmp_path: Pytest 为本用例创建的独立临时目录。

    Returns:
        无返回值。
    """

    import _winapi

    workspace_root = tmp_path / "workspace"
    manager = WorkspaceManager(
        asset_root=tmp_path / "assets",
        workspace_root=workspace_root,
    )
    task_paths = _prepare_and_materialize(
        manager,
        "activity-a",
        agent_config_base=tmp_path / "runtime",
    )
    tasks_root = task_paths.task_root.parent
    external_task_root = tmp_path / "external-tasks-for-cleanup" / "activity-a"
    external_task_root.mkdir(parents=True)
    external_file = external_task_root / "must-survive.txt"
    external_file.write_text("outside", encoding="utf-8")
    workspace_module.shutil.rmtree(tasks_root)
    _winapi.CreateJunction(str(external_task_root.parent), str(tasks_root))

    try:
        removed = manager.discard_unstarted_task(
            "activity-a",
            agent_config_root=task_paths.agent_config_root,
        )
    finally:
        if tasks_root.is_junction():
            tasks_root.rmdir()

    assert removed is True
    assert not task_paths.agent_config_root.exists()
    assert external_file.read_text(encoding="utf-8") == "outside"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Junction 专属回归")
def test_discard_unstarted_task_only_removes_task_root_junction_node(
    tmp_path: Path,
) -> None:
    """最终任务节点被替换为 Junction 时，只能删除节点不能删除目标。

    Args:
        tmp_path: Pytest 为本用例创建的独立临时目录。

    Returns:
        无返回值。
    """

    import _winapi

    manager = WorkspaceManager(
        asset_root=tmp_path / "assets",
        workspace_root=tmp_path / "workspace",
    )
    task_paths = manager.prepare_task(
        "activity-a",
        agent_config_base=tmp_path / "runtime",
    )
    external_task_root = tmp_path / "external-task-root"
    external_task_root.mkdir()
    external_file = external_task_root / "must-survive.txt"
    external_file.write_text("outside", encoding="utf-8")
    workspace_module.shutil.rmtree(task_paths.task_root)
    _winapi.CreateJunction(str(external_task_root), str(task_paths.task_root))

    removed = manager.discard_unstarted_task(
        "activity-a",
        agent_config_root=task_paths.agent_config_root,
    )

    assert removed is True
    assert not task_paths.task_root.exists()
    assert not task_paths.agent_config_root.exists()
    assert external_file.read_text(encoding="utf-8") == "outside"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Junction 专属回归")
def test_discard_unstarted_task_removes_broken_task_root_junction_node(
    tmp_path: Path,
) -> None:
    """Junction 目标已消失时，未启动清理仍要删除遗留链接节点。

    Args:
        tmp_path: Pytest 为本用例创建的独立临时目录。

    Returns:
        无返回值。
    """

    import _winapi

    manager = WorkspaceManager(
        asset_root=tmp_path / "assets",
        workspace_root=tmp_path / "workspace",
    )
    task_paths = manager.prepare_task(
        "activity-a",
        agent_config_base=tmp_path / "runtime",
    )
    external_task_root = tmp_path / "vanished-task-root"
    external_task_root.mkdir()
    workspace_module.shutil.rmtree(task_paths.task_root)
    _winapi.CreateJunction(str(external_task_root), str(task_paths.task_root))
    external_task_root.rmdir()

    try:
        removed = manager.discard_unstarted_task(
            "activity-a",
            agent_config_root=task_paths.agent_config_root,
        )
        assert removed is True
        assert not task_paths.task_root.is_junction()
        assert not task_paths.agent_config_root.exists()
    finally:
        if task_paths.task_root.is_junction():
            task_paths.task_root.rmdir()


def _write_skill(asset_root: Path, skill_id: str) -> None:
    """写入工作区测试使用的最小合法 Skill。

    Args:
        asset_root: 产品工作区资产根目录。
        skill_id: 清单与目录共同使用的稳定编号。

    Returns:
        无返回值。
    """

    skill_root = asset_root / ".claude" / "skills" / skill_id
    skill_root.mkdir(parents=True)
    (skill_root / "SKILL.md").write_text(
        f"---\nname: {skill_id}\ndescription: 工作区测试 Skill。\n---\n\n说明",
        encoding="utf-8",
    )
    (skill_root / "manifest.json").write_text(
        json.dumps(
            {
                "id": skill_id,
                "name": skill_id,
                "summary": "测试 Skill",
                "category": "测试",
                "version": "1.0.0",
                "default_enabled": True,
                "dependencies": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
