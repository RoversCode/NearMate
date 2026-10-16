from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from companion.core.api.dependencies import get_materials_dependencies
from companion.core.api.routers import materials


class RecordingMaterialLifecycle:
    """记录近期材料忘记请求并返回固定权威快照。"""

    def __init__(self) -> None:
        self.forgotten_paths: list[str] = []
        self.refresh_count = 0

    async def refresh_material_context(self) -> dict[str, object]:
        """记录一次显式状态刷新。

        Returns:
            Desktop 需要的更高 revision 完整材料快照。
        """

        self.refresh_count += 1
        return {
            "revision": 5,
            "turn_material_context": {
                "staged_materials": [],
                "active": None,
            },
            "recent_materials": [],
        }

    async def forget_recent_material(self, path: str) -> dict[str, object]:
        """记录已经通过 schema 校验的路径。

        Args:
            path: 去除首尾空白后的近期材料路径。

        Returns:
            Desktop 需要的完整 revision 材料快照。
        """

        self.forgotten_paths.append(path)
        return {
            "revision": 4,
            "turn_material_context": {
                "staged_materials": [],
                "active": None,
            },
            "recent_materials": [],
        }


def test_forget_recent_material_uses_json_body_and_returns_full_snapshot() -> None:
    """忘记接口应裁剪 JSON 路径并原样返回权威材料快照。"""

    client, lifecycle = _client()

    response = client.post(
        "/api/materials/recent/forget",
        json={"path": r"  C:\project\app.py  "},
    )

    assert response.status_code == 200
    assert lifecycle.forgotten_paths == [r"C:\project\app.py"]
    assert response.json() == {
        "accepted": True,
        "material_context": {
            "revision": 4,
            "turn_material_context": {
                "staged_materials": [],
                "active": None,
            },
            "recent_materials": [],
        },
    }


def test_get_current_material_context_runs_revisioned_status_refresh() -> None:
    """显式 GET 必须刷新路径状态并返回可越过 Desktop 门禁的新 revision。"""

    client, lifecycle = _client()

    response = client.get("/api/materials/contexts/current")

    assert response.status_code == 200
    assert lifecycle.refresh_count == 1
    assert response.json()["revision"] == 5


def test_forget_recent_material_rejects_extra_fields() -> None:
    """路径命令不能静默接纳 Desktop 未审查的多余字段。"""

    client, lifecycle = _client()

    response = client.post(
        "/api/materials/recent/forget",
        json={"path": r"C:\project\app.py", "material_id": "guessed"},
    )

    assert response.status_code == 422
    assert lifecycle.forgotten_paths == []


def _client() -> tuple[TestClient, RecordingMaterialLifecycle]:
    """构造只注册材料路由的测试客户端。

    Returns:
        FastAPI 测试客户端和可检查的材料生命周期替身。
    """

    lifecycle = RecordingMaterialLifecycle()
    app = FastAPI()
    app.include_router(materials.router)
    app.dependency_overrides[get_materials_dependencies] = lambda: SimpleNamespace(
        hub=SimpleNamespace(),
        material_lifecycle=lifecycle,
    )
    return TestClient(app), lifecycle
