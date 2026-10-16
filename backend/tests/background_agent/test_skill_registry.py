from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from companion.background_agent.skill_asset_safety import SkillAssetBoundaryError
from companion.background_agent.skill_registry import (
    SkillRegistry,
    SkillRevisionConflictError,
)


def test_registry_lists_valid_product_skill_and_builds_execution_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """只有产品目录中清单合法、依赖可用的 Skill 才能进入任务快照。"""

    _write_skill(tmp_path, skill_id="media-convert", dependency="ffmpeg")
    monkeypatch.setattr(
        "companion.background_agent.skill_dependencies.shutil.which",
        lambda name: "C:/tools/ffmpeg.exe" if name == "ffmpeg" else None,
    )
    registry = SkillRegistry(asset_root=tmp_path)

    status_snapshot = registry.status_snapshot()
    execution_snapshot = registry.execution_snapshot()

    assert [item.manifest.skill_id for item in status_snapshot.statuses] == [
        "media-convert"
    ]
    assert status_snapshot.statuses[0].enabled is True
    assert status_snapshot.statuses[0].ready is True
    assert execution_snapshot.enabled_skill_ids == ("media-convert",)
    assert execution_snapshot.revision == status_snapshot.revision
    assert 0 <= status_snapshot.revision < 2**53


def test_registry_keeps_enabled_preference_but_excludes_missing_dependency(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """依赖后来失效时保留用户开关，但下一项任务不能继续加载该 Skill。"""

    _write_skill(tmp_path, skill_id="media-convert", dependency="ffmpeg")
    monkeypatch.setattr(
        "companion.background_agent.skill_dependencies.shutil.which",
        lambda _name: None,
    )
    registry = SkillRegistry(asset_root=tmp_path)

    status = registry.status_snapshot({"media-convert": True}).statuses[0]
    execution_snapshot = registry.execution_snapshot({"media-convert": True})

    assert status.enabled is True
    assert status.ready is False
    assert status.usable is False
    assert "ffmpeg" in str(status.unavailable_reason)
    assert execution_snapshot.enabled_skill_ids == ()


def test_registry_isolates_invalid_manifest_without_hiding_valid_skills(
    tmp_path: Path,
) -> None:
    """一个坏 Skill 不能阻断整个后台智能体，也不能偷偷进入可用名单。"""

    _write_skill(tmp_path, skill_id="valid-skill")
    invalid_root = tmp_path / ".claude" / "skills" / "invalid-skill"
    invalid_root.mkdir(parents=True)
    (invalid_root / "SKILL.md").write_text("说明", encoding="utf-8")
    (invalid_root / "manifest.json").write_text(
        json.dumps(
            {
                "id": "different-id",
                "name": "错误 Skill",
                "summary": "错误",
                "category": "测试",
                "version": "1.0.0",
            }
        ),
        encoding="utf-8",
    )

    registry = SkillRegistry(asset_root=tmp_path)

    assert [item.skill_id for item in registry.manifests] == ["valid-skill"]
    assert len(registry.issues) == 1
    assert "目录名一致" in registry.issues[0].message


def test_registry_isolates_skill_with_non_utf8_instruction(tmp_path: Path) -> None:
    """说明文件不是 UTF-8 时整项隔离，不能把坏文本复制进任务工作区。"""

    _write_skill(tmp_path, skill_id="invalid-instruction")
    instruction_path = (
        tmp_path
        / ".claude"
        / "skills"
        / "invalid-instruction"
        / "SKILL.md"
    )
    instruction_path.write_bytes(b"\xff\xfe\x00")

    registry = SkillRegistry(asset_root=tmp_path)

    assert registry.manifests == ()
    assert len(registry.issues) == 1
    assert registry.issues[0].message == "SKILL.md 必须是 UTF-8 文本"
    assert str(tmp_path) not in registry.issues[0].message


@pytest.mark.parametrize(
    ("instruction_text", "expected_message"),
    [
        pytest.param(
            "# 使用说明\n",
            "SKILL.md 缺少 YAML frontmatter",
            id="missing-frontmatter",
        ),
        pytest.param(
            "---\nname: another-skill\ndescription: 测试说明\n---\n",
            "SKILL.md frontmatter.name 必须与 Skill ID 一致",
            id="wrong-name",
        ),
        pytest.param(
            '---\nname: document-read\ndescription: "   "\n---\n',
            "SKILL.md frontmatter.description 必须是非空字符串",
            id="empty-description",
        ),
    ],
)
def test_registry_isolates_invalid_skill_frontmatter(
    tmp_path: Path,
    instruction_text: str,
    expected_message: str,
) -> None:
    """身份 frontmatter 不完整的 Skill 不能进入产品能力名单。"""

    _write_skill(tmp_path, skill_id="document-read")
    instruction_path = (
        tmp_path / ".claude" / "skills" / "document-read" / "SKILL.md"
    )
    instruction_path.write_text(instruction_text, encoding="utf-8")

    registry = SkillRegistry(asset_root=tmp_path)

    assert registry.manifests == ()
    assert len(registry.issues) == 1
    assert registry.issues[0].message == expected_message


def test_registry_checks_minimum_executable_version(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """声明最低版本后，命令存在但版本过低也必须判为不可用。"""

    _write_skill(
        tmp_path,
        skill_id="media-convert",
        dependency="ffmpeg",
        minimum_version="7.0.0",
    )
    monkeypatch.setattr(
        "companion.background_agent.skill_dependencies.shutil.which",
        lambda _name: "C:/tools/ffmpeg.exe",
    )
    monkeypatch.setattr(
        "companion.background_agent.skill_dependencies.subprocess.run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="ffmpeg version 6.1.2",
            stderr="",
        ),
    )

    status = SkillRegistry(asset_root=tmp_path).status_snapshot().statuses[0]

    assert status.ready is False
    assert status.dependencies[0].detected_version == "6.1.2"
    assert "低于要求 7.0.0" in str(status.unavailable_reason)


def test_registry_treats_missing_version_segments_as_zero(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """外部命令报告 7.0 时，应满足清单要求的 7.0.0。"""

    _write_skill(
        tmp_path,
        skill_id="media-convert",
        dependency="ffmpeg",
        minimum_version="7.0.0",
    )
    monkeypatch.setattr(
        "companion.background_agent.skill_dependencies.shutil.which",
        lambda _name: "C:/tools/ffmpeg.exe",
    )
    monkeypatch.setattr(
        "companion.background_agent.skill_dependencies.subprocess.run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="ffmpeg version 7.0",
            stderr="",
        ),
    )

    status = SkillRegistry(asset_root=tmp_path).status_snapshot().statuses[0]

    assert status.ready is True
    assert status.dependencies[0].detected_version == "7.0"


def test_registry_tolerates_non_utf8_dependency_version_output(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """外部命令混入非 UTF-8 字节时，依赖检查仍返回状态而不是抛异常。"""

    _write_skill(
        tmp_path,
        skill_id="media-convert",
        dependency="ffmpeg",
        minimum_version="7.0.0",
    )
    monkeypatch.setattr(
        "companion.background_agent.skill_dependencies.shutil.which",
        lambda _name: "C:/tools/ffmpeg.exe",
    )

    def run_with_invalid_utf8(
        *_args: object,
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        """模拟 subprocess 按调用方指定策略解码坏字节。

        Args:
            _args: subprocess.run 的位置参数。
            kwargs: subprocess.run 的关键字参数。

        Returns:
            含替换字符但仍可识别版本号的命令结果。
        """

        assert kwargs["encoding"] == "utf-8"
        assert kwargs["errors"] == "replace"
        return subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=b"ffmpeg version 7.0\xff".decode("utf-8", errors="replace"),
            stderr="",
        )

    monkeypatch.setattr(
        "companion.background_agent.skill_dependencies.subprocess.run",
        run_with_invalid_utf8,
    )

    status = SkillRegistry(asset_root=tmp_path).status_snapshot().statuses[0]

    assert status.ready is True
    assert status.dependencies[0].detected_version == "7.0"


def test_update_enabled_rejects_stale_revision_and_preserves_unavailable_preference(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """版本过期仍拒绝，但依赖缺失不应抹掉用户的长期开启意图。"""

    _write_skill(tmp_path, skill_id="media-convert", dependency="ffmpeg")
    monkeypatch.setattr(
        "companion.background_agent.skill_dependencies.shutil.which",
        lambda _name: None,
    )
    registry = SkillRegistry(asset_root=tmp_path)
    revision = registry.revision_for_preferences({})

    with pytest.raises(SkillRevisionConflictError):
        registry.update_enabled(
            preferences={},
            skill_id="media-convert",
            enabled=False,
            expected_revision=revision + 1,
        )
    updated_preferences, updated_status, _revision = registry.update_enabled(
        preferences={"media-convert": False},
        skill_id="media-convert",
        enabled=True,
        expected_revision=registry.revision_for_preferences(
            {"media-convert": False}
        ),
    )

    assert updated_preferences == {"media-convert": True}
    assert updated_status.enabled is True
    assert updated_status.ready is False
    assert updated_status.usable is False
    assert "ffmpeg" in str(updated_status.unavailable_reason)
    assert registry.execution_snapshot(updated_preferences).enabled_skill_ids == ()


def test_saved_enabled_preference_becomes_usable_after_dependency_recovers(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """依赖后来安装成功时，新任务应自动采用此前保存的开启偏好。"""

    _write_skill(tmp_path, skill_id="media-convert", dependency="ffmpeg")
    dependency_available = False
    monkeypatch.setattr(
        "companion.background_agent.skill_dependencies.shutil.which",
        lambda _name: "C:/tools/ffmpeg.exe" if dependency_available else None,
    )
    registry = SkillRegistry(asset_root=tmp_path)
    preferences, unavailable_status, _revision = registry.update_enabled(
        preferences={"media-convert": False},
        skill_id="media-convert",
        enabled=True,
        expected_revision=registry.revision_for_preferences(
            {"media-convert": False}
        ),
    )
    assert unavailable_status.usable is False

    dependency_available = True
    registry.refresh()

    assert registry.execution_snapshot(preferences).enabled_skill_ids == (
        "media-convert",
    )


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Junction 专属回归")
def test_registry_skips_managed_skills_root_junction(tmp_path: Path) -> None:
    """受管 Skill 根目录是 Junction 时，扫描不能跟随到产品目录外。"""

    import _winapi

    asset_root = tmp_path / "assets"
    external_asset_root = tmp_path / "external"
    _write_skill(external_asset_root, skill_id="escaped-skill")
    managed_claude_root = asset_root / ".claude"
    managed_claude_root.mkdir(parents=True)
    _winapi.CreateJunction(
        str(external_asset_root / ".claude" / "skills"),
        str(managed_claude_root / "skills"),
    )

    registry = SkillRegistry(asset_root=asset_root)

    assert registry.manifests == ()
    assert registry.issues == ()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Junction 专属回归")
def test_registry_skips_skills_below_junction_ancestor(tmp_path: Path) -> None:
    """`.claude` 祖先是 Junction 时，正常的 skills 子目录也不能越过边界。"""

    import _winapi

    asset_root = tmp_path / "assets"
    asset_root.mkdir()
    external_asset_root = tmp_path / "external"
    _write_skill(external_asset_root, skill_id="escaped-skill")
    _winapi.CreateJunction(
        str(external_asset_root / ".claude"),
        str(asset_root / ".claude"),
    )

    registry = SkillRegistry(asset_root=asset_root)

    assert registry.manifests == ()
    assert registry.issues == ()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Junction 专属回归")
def test_registry_isolates_skill_containing_junction(tmp_path: Path) -> None:
    """单个 Skill 内含 Junction 时，只隔离该 Skill 且不暴露外部路径。"""

    import _winapi

    asset_root = tmp_path / "assets"
    _write_skill(asset_root, skill_id="unsafe-skill")
    external_root = tmp_path / "external"
    external_root.mkdir()
    (external_root / "secret.txt").write_text("outside", encoding="utf-8")
    _winapi.CreateJunction(
        str(external_root),
        str(asset_root / ".claude" / "skills" / "unsafe-skill" / "external"),
    )

    registry = SkillRegistry(asset_root=asset_root)

    assert registry.manifests == ()
    assert len(registry.issues) == 1
    assert "链接或重解析点" in registry.issues[0].message
    assert str(external_root) not in registry.issues[0].message


def test_registry_isolates_skill_containing_symbolic_link(tmp_path: Path) -> None:
    """单个 Skill 内含软链接时，扫描不能把链接目标当成产品资产。"""

    asset_root = tmp_path / "assets"
    _write_skill(asset_root, skill_id="unsafe-skill")
    external_file = tmp_path / "external.txt"
    external_file.write_text("outside", encoding="utf-8")
    linked_file = (
        asset_root / ".claude" / "skills" / "unsafe-skill" / "linked.txt"
    )
    try:
        os.symlink(external_file, linked_file)
    except OSError as exception:
        pytest.skip(f"当前系统不允许创建测试软链接：{exception}")

    registry = SkillRegistry(asset_root=asset_root)

    assert registry.manifests == ()
    assert len(registry.issues) == 1
    assert "链接或重解析点" in registry.issues[0].message


def test_skill_detail_lists_relative_files_and_keeps_instruction_out_of_list(
    tmp_path: Path,
) -> None:
    """列表保持轻量，详情才返回说明原文和相对资产清单。"""

    _write_skill(tmp_path, skill_id="document-read")
    cache_root = (
        tmp_path / ".claude" / "skills" / "document-read" / "scripts" / "__pycache__"
    )
    cache_root.mkdir(parents=True)
    (cache_root / "helper.pyc").write_bytes(b"cache")
    registry = SkillRegistry(asset_root=tmp_path)

    list_payload = registry.status_snapshot().to_payload()
    detail_payload = registry.detail("document-read")

    assert "instruction" not in list_payload["skills"][0]
    assert "# 使用说明" in detail_payload["instruction"]
    assert "SKILL.md" in detail_payload["files"]
    assert all("__pycache__" not in item for item in detail_payload["files"])
    assert all(not Path(item).is_absolute() for item in detail_payload["files"])


def test_skill_detail_rejects_instruction_replaced_after_manifest_scan(
    tmp_path: Path,
) -> None:
    """清单扫描后说明文件被替换时，详情必须拒绝混合资产快照。"""

    _write_skill(tmp_path, skill_id="document-read")
    registry = SkillRegistry(asset_root=tmp_path)
    instruction_path = registry.get_manifest("document-read").instruction_path
    instruction_path.write_bytes(b"\xff\xfe\x00")

    with pytest.raises(SkillAssetBoundaryError):
        registry.detail("document-read")


def test_manifest_exposes_optional_scenario_input_and_output_metadata(
    tmp_path: Path,
) -> None:
    """详情页元数据必须来自受管清单，不能由 Console 猜测说明原文。"""

    _write_skill(tmp_path, skill_id="document-read")
    manifest_path = (
        tmp_path / ".claude" / "skills" / "document-read" / "manifest.json"
    )
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload.update(
        {
            "trigger_scenarios": ["读取文档正文"],
            "inputs": ["文档文件"],
            "outputs": ["结构化文本"],
        }
    )
    manifest_path.write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )

    status_payload = SkillRegistry(asset_root=tmp_path).status_snapshot().to_payload()

    assert status_payload["skills"][0]["trigger_scenarios"] == ["读取文档正文"]
    assert status_payload["skills"][0]["inputs"] == ["文档文件"]
    assert status_payload["skills"][0]["outputs"] == ["结构化文本"]


def test_registry_revision_ignores_removed_skill_preferences(tmp_path: Path) -> None:
    """已经不属于当前产品清单的旧开关不能制造无意义版本变化。"""

    _write_skill(tmp_path, skill_id="document-read")
    registry = SkillRegistry(asset_root=tmp_path)

    assert registry.revision_for_preferences({"removed-skill": False}) == (
        registry.revision_for_preferences({})
    )


def _write_skill(
    asset_root: Path,
    *,
    skill_id: str,
    dependency: str | None = None,
    minimum_version: str | None = None,
) -> None:
    """写入测试用的最小产品 Skill 资产。"""

    skill_root = asset_root / ".claude" / "skills" / skill_id
    skill_root.mkdir(parents=True)
    (skill_root / "SKILL.md").write_text(
        (
            "---\n"
            f'name: "{skill_id}"\n'
            "description: 测试使用的 Skill。\n"
            "---\n\n"
            "# 使用说明\n"
        ),
        encoding="utf-8",
    )
    dependencies: list[dict[str, str]] = []
    if dependency is not None:
        dependency_payload = {"kind": "executable", "name": dependency}
        if minimum_version is not None:
            dependency_payload["minimum_version"] = minimum_version
        dependencies.append(dependency_payload)
    (skill_root / "manifest.json").write_text(
        json.dumps(
            {
                "id": skill_id,
                "name": f"{skill_id} 名称",
                "summary": "测试 Skill",
                "category": "测试",
                "version": "1.0.0",
                "default_enabled": True,
                "dependencies": dependencies,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
