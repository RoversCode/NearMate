"""验证受管 Skill 的 Console HTTP 契约。"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from companion.background_agent import skill_registry as skill_registry_module
from companion.background_agent.skill_asset_safety import SkillAssetBoundaryError
from companion.background_agent.skill_registry import (
    SkillNotFoundError,
    SkillRegistry,
    SkillRevisionConflictError,
)
from companion.core.api.dependencies import get_skill_library_dependencies
from companion.core.api.routers import skill_library


class StubSkillLibraryCoordinator:
    """记录技能库请求并按测试场景返回固定结果。"""

    def __init__(self) -> None:
        """初始化空的更新记录。"""

        self.updated: dict[str, object] | None = None

    async def current_skill_library_payload(self) -> dict[str, object]:
        """返回一项可用 Skill 的最小列表快照。"""

        return {"skills": [_skill_payload()], "revision": 7, "issues": [], "summary": {}}

    async def current_skill_detail_payload(self, skill_id: str) -> dict[str, object]:
        """返回详情，或模拟 Skill 不存在。

        Args:
            skill_id: API 传入的 Skill 编号。

        Returns:
            固定 Skill 详情。
        """

        if skill_id == "missing":
            raise SkillNotFoundError("找不到产品 Skill：missing")
        return {**_skill_payload(), "instruction": "# FFmpeg Convert", "files": ["SKILL.md"], "revision": 7}

    async def update_skill_enabled(self, **payload: object) -> dict[str, object]:
        """记录开关请求并模拟业务错误。

        Args:
            payload: 路由传入的 Skill 编号、开关和版本。

        Returns:
            更新后的 Skill 状态与版本。
        """

        self.updated = payload
        skill_id = str(payload["skill_id"])
        if skill_id == "missing":
            raise SkillNotFoundError("找不到产品 Skill：missing")
        if skill_id == "stale":
            raise SkillRevisionConflictError(6, 7)
        return {
            "skill": _skill_payload(enabled=bool(payload["enabled"])),
            "revision": 8,
        }


class RegistryBackedSkillDetailCoordinator:
    """让 API 回归测试直接经过真实 Skill Registry。"""

    def __init__(self, registry: SkillRegistry) -> None:
        """保存详情读取使用的真实 Registry。

        Args:
            registry: 已经完成初次产品 Skill 扫描的 Registry。

        Returns:
            无返回值。
        """

        self.registry = registry

    async def current_skill_detail_payload(self, skill_id: str) -> dict[str, object]:
        """读取真实 Skill 详情。

        Args:
            skill_id: API 传入的稳定 Skill 编号。

        Returns:
            Registry 生成的安全详情对象。
        """

        return self.registry.detail(skill_id)


def test_skill_library_lists_managed_skills() -> None:
    """列表接口返回 Skill 和乐观锁版本。"""

    response = _client(StubSkillLibraryCoordinator()).get("/api/console/skills")

    assert response.status_code == 200
    assert response.json()["revision"] == 7
    assert response.json()["skills"][0]["id"] == "ffmpeg-convert"


def test_skill_library_returns_plain_text_detail() -> None:
    """详情接口只返回说明原文和相对文件名。"""

    response = _client(StubSkillLibraryCoordinator()).get(
        "/api/console/skills/ffmpeg-convert"
    )

    assert response.status_code == 200
    assert response.json()["instruction"] == "# FFmpeg Convert"
    assert response.json()["files"] == ["SKILL.md"]


def test_skill_library_detail_returns_conflict_when_instruction_changes(
    tmp_path: Path,
) -> None:
    """初次扫描后说明文件损坏时，详情 API 应返回稳定资产冲突。"""

    asset_root = tmp_path / "assets"
    _write_skill_asset(asset_root, skill_id="document-read")
    registry = SkillRegistry(asset_root=asset_root)
    registry.get_manifest("document-read").instruction_path.write_bytes(b"\xff\xfe")

    response = _client(RegistryBackedSkillDetailCoordinator(registry)).get(
        "/api/console/skills/document-read"
    )

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "code": "skill_asset_changed",
        "message": "Skill 资产已变化，请刷新后重试",
    }


def test_skill_library_returns_conflict_when_asset_changes_during_detail_read(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """详情读取期间资产被替换时，API 应返回稳定冲突且不泄露路径。"""

    asset_root = tmp_path / "assets"
    _write_skill_asset(asset_root, skill_id="document-read")
    registry = SkillRegistry(asset_root=asset_root)
    original_validate = skill_registry_module.validate_skill_asset_tree
    validation_calls = 0

    def fail_second_validation(
        skill_root: Path,
        *,
        asset_root: Path,
        managed_skills_root: Path,
    ) -> object:
        """模拟详情读完说明后发现资产已经变化。

        Args:
            skill_root: Registry 正在读取的受管 Skill 根目录。
            asset_root: Registry 绑定的产品资产根目录。
            managed_skills_root: Registry 允许访问的受管 Skill 根目录。

        Returns:
            第一次检查得到的真实安全快照。

        Raises:
            SkillAssetBoundaryError: 第二次复验时模拟资产变化。
        """

        nonlocal validation_calls
        validation_calls += 1
        if validation_calls == 2:
            raise SkillAssetBoundaryError("测试外部路径不能进入响应")
        return original_validate(
            skill_root,
            asset_root=asset_root,
            managed_skills_root=managed_skills_root,
        )

    monkeypatch.setattr(
        skill_registry_module,
        "validate_skill_asset_tree",
        fail_second_validation,
    )

    response = _client(RegistryBackedSkillDetailCoordinator(registry)).get(
        "/api/console/skills/document-read"
    )

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "code": "skill_asset_changed",
        "message": "Skill 资产已变化，请刷新后重试",
    }
    assert str(tmp_path) not in response.text


def test_skill_library_updates_only_future_task_preference() -> None:
    """PATCH 严格传递开关值和 Console 已知版本。"""

    coordinator = StubSkillLibraryCoordinator()
    response = _client(coordinator).patch(
        "/api/console/skills/ffmpeg-convert",
        json={"enabled": False, "expected_revision": 7},
    )

    assert response.status_code == 202
    assert coordinator.updated == {
        "skill_id": "ffmpeg-convert",
        "enabled": False,
        "expected_revision": 7,
    }
    assert response.json()["skill"]["enabled"] is False
    assert response.json()["revision"] == 8


def test_skill_library_accepts_enabled_preference_for_unavailable_skill() -> None:
    """依赖缺失只影响可用性，PATCH 仍应保存用户的开启偏好。"""

    coordinator = StubSkillLibraryCoordinator()
    response = _client(coordinator).patch(
        "/api/console/skills/ffmpeg-convert",
        json={"enabled": True, "expected_revision": 7},
    )

    assert response.status_code == 202
    assert coordinator.updated == {
        "skill_id": "ffmpeg-convert",
        "enabled": True,
        "expected_revision": 7,
    }


def test_skill_library_returns_current_revision_for_stale_update() -> None:
    """旧页面提交不能覆盖新设置，并要携带当前版本供刷新。"""

    response = _client(StubSkillLibraryCoordinator()).patch(
        "/api/console/skills/stale",
        json={"enabled": False, "expected_revision": 6},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "code": "skill_revision_conflict",
        "message": "Skill 设置版本已变化，提交版本=6，当前版本=7",
        "actual_revision": 7,
    }


def test_skill_library_rejects_coerced_boolean_and_revision() -> None:
    """字符串和 JSON 布尔值不能被 Pydantic 偷偷转换成有效修改。"""

    client = _client(StubSkillLibraryCoordinator())

    assert client.patch(
        "/api/console/skills/ffmpeg-convert",
        json={"enabled": "false", "expected_revision": 7},
    ).status_code == 422
    assert client.patch(
        "/api/console/skills/ffmpeg-convert",
        json={"enabled": False, "expected_revision": True},
    ).status_code == 422


def test_skill_library_rejects_revision_above_safe_integer_bound() -> None:
    """Skill PATCH 不能接收 JavaScript 无法无损表示的版本。"""

    response = _client(StubSkillLibraryCoordinator()).patch(
        "/api/console/skills/ffmpeg-convert",
        json={"enabled": True, "expected_revision": 2**53},
    )

    assert response.status_code == 422


def _client(coordinator: Any) -> TestClient:
    """构造只挂载技能库路由的测试客户端。

    Args:
        coordinator: 本测试使用的应用服务替身。

    Returns:
        已覆盖技能库依赖的同步测试客户端。
    """

    app = FastAPI()
    app.include_router(skill_library.router)
    app.dependency_overrides[get_skill_library_dependencies] = lambda: SimpleNamespace(
        user_settings=coordinator,
    )
    return TestClient(app)


def _write_skill_asset(asset_root: Path, *, skill_id: str) -> None:
    """写入 API 回归测试使用的最小产品 Skill。

    Args:
        asset_root: 产品工作区资产根目录。
        skill_id: Skill 的稳定编号和目录名。

    Returns:
        无返回值。
    """

    skill_root = asset_root / ".claude" / "skills" / skill_id
    skill_root.mkdir(parents=True)
    (skill_root / "SKILL.md").write_text(
        (
            "---\n"
            f'name: "{skill_id}"\n'
            "description: API 回归测试 Skill。\n"
            "---\n\n"
            "# 使用说明"
        ),
        encoding="utf-8",
    )
    (skill_root / "manifest.json").write_text(
        json.dumps(
            {
                "id": skill_id,
                "name": "文档读取",
                "summary": "读取文档正文。",
                "category": "文件",
                "version": "1.0.0",
                "default_enabled": True,
                "dependencies": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _skill_payload(*, enabled: bool = True) -> dict[str, object]:
    """构造路由测试使用的最小 Skill 状态。

    Args:
        enabled: 返回状态中的用户开关。

    Returns:
        含列表页所需字段的 Skill 状态。
    """

    return {
        "id": "ffmpeg-convert",
        "name": "媒体格式转换",
        "summary": "使用 FFmpeg 转换音视频格式。",
        "category": "文件与媒体",
        "version": "1.0.0",
        "default_enabled": True,
        "source": "builtin",
        "trigger_scenarios": ["转换媒体格式"],
        "inputs": ["音视频文件"],
        "outputs": ["转换后的文件"],
        "dependencies": [],
        "enabled": enabled,
        "ready": True,
        "usable": enabled,
        "unavailable_reason": None,
        "dependency_status": [],
    }
