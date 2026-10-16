"""验证后台智能体固定工作区遵循系统文档目录。"""

from pathlib import Path

from companion.background_agent import workspace_location
from companion.background_agent.workspace import WorkspaceManager


def test_default_workspace_uses_resolved_user_documents_directory(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """默认工作区不能写死为用户主目录下的英文 Documents。"""

    redirected_documents = tmp_path / "OneDrive" / "文档"
    monkeypatch.setattr(
        workspace_location,
        "_resolve_user_documents_directory",
        lambda: redirected_documents,
    )

    assert workspace_location.resolve_default_workspace_root() == (
        redirected_documents / "NearMate Workspace"
    ).resolve()


def test_workspace_manager_uses_platform_default_when_not_explicitly_configured(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """未显式指定工作区时，管理器必须使用平台解析结果。"""

    expected_workspace = tmp_path / "系统文档" / "NearMate Workspace"
    monkeypatch.setattr(
        "companion.background_agent.workspace.resolve_default_workspace_root",
        lambda: expected_workspace,
    )

    manager = WorkspaceManager(asset_root=tmp_path / "assets")

    assert manager.workspace_root == expected_workspace.resolve()
