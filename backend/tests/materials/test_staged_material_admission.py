import asyncio
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from companion.core.api.routers.materials import append_staged_turn_materials, delete_staged_turn_material
from companion.core.api.schemas import MaterialItemsRequest
from companion.materials.turn_context import TurnMaterialContextService


@pytest.mark.parametrize(
    ("file_name", "expected_media_class"),
    [
        ("voice.mp3", "audio"),
        ("voice.WAV", "audio"),
        ("voice.m4a", "audio"),
        ("voice.aac", "audio"),
        ("voice.flac", "audio"),
        ("voice.ogg", "audio"),
        ("voice.opus", "audio"),
        ("movie.mp4", "video"),
        ("movie.MOV", "video"),
        ("movie.mkv", "video"),
        ("movie.webm", "video"),
        ("movie.avi", "video"),
        ("movie.m4v", "video"),
    ],
)
async def test_common_audio_and_video_files_are_admitted_as_path_materials(
    tmp_path: Path,
    file_name: str,
    expected_media_class: str,
) -> None:
    """验证常用音视频扩展名只按路径接纳，并得到明确媒体类型。

    Args:
        tmp_path: pytest 提供的临时目录。
        file_name: 当前场景使用的媒体文件名。
        expected_media_class: 准入后应冻结的媒体类别。
    """

    media_path = tmp_path / file_name
    media_path.write_bytes(b"path-only-media")
    service = TurnMaterialContextService()

    result = await service.append_staged(
        attachments=[_file_item(media_path)],
        created_at="2026-07-30T10:00:00+08:00",
    )

    assert result.to_payload()["added_count"] == 1
    assert result.added[0].media_class == expected_media_class
    assert result.added[0].node_kind == "file"


def test_material_request_requires_path_and_rejects_text_preview(tmp_path: Path) -> None:
    """验证路径是唯一材料实体入口，旧正文预览字段不能再进入协议。"""

    path = tmp_path / "notes.txt"
    path.write_text("hello", encoding="utf-8")
    base = {
        "kind": "dragged_file",
        "source": "test",
        "display_name": "notes.txt",
    }

    with pytest.raises(ValidationError):
        MaterialItemsRequest(items=[base])
    with pytest.raises(ValidationError):
        MaterialItemsRequest(items=[{**base, "path": str(path), "text_preview": "hello"}])


async def test_staged_material_admission_reports_mixed_results(tmp_path: Path, caplog) -> None:
    """验证批量材料会分别统计成功、不支持、不可读和重复项。"""

    supported_path = tmp_path / "notes.txt"
    supported_path.write_text("hello", encoding="utf-8")
    unsupported_path = tmp_path / "archive.exe"
    unsupported_path.write_bytes(b"binary")
    empty_path = tmp_path / "empty.md"
    empty_path.write_text("", encoding="utf-8")
    service = TurnMaterialContextService()

    first = await service.append_staged(
        attachments=[_file_item(supported_path)],
        created_at="2026-07-11T10:00:00+08:00",
    )
    with caplog.at_level(logging.INFO, logger="companion.materials.turn_context"):
        result = await service.append_staged(
            attachments=[
                _file_item(supported_path),
                _file_item(unsupported_path),
                _file_item(empty_path),
                _file_item(tmp_path / "missing.txt"),
            ],
            created_at="2026-07-11T10:01:00+08:00",
        )

    assert first.to_payload() == {
        "submitted_count": 1,
        "added_count": 1,
        "unsupported_count": 0,
        "unreadable_count": 0,
        "duplicate_count": 0,
        "issues": [],
    }
    assert result.to_payload() == {
        "submitted_count": 4,
        "added_count": 0,
        "unsupported_count": 1,
        "unreadable_count": 2,
        "duplicate_count": 1,
        "issues": [
            {"submitted_index": 0, "display_name": "notes.txt", "category": "duplicate", "reason": "duplicate"},
            {"submitted_index": 1, "display_name": "archive.exe", "category": "unsupported", "reason": "unsupported_extension"},
            {"submitted_index": 2, "display_name": "empty.md", "category": "unreadable", "reason": "empty_file"},
            {"submitted_index": 3, "display_name": "missing.txt", "category": "unreadable", "reason": "missing_path"},
        ],
    }
    assert [item.display_name for item in service.staged_materials()] == ["notes.txt"]
    assert "display_name=archive.exe reason=unsupported_extension detail=文件格式不支持" in caplog.text
    assert "display_name=empty.md reason=empty_file detail=文件内容为空" in caplog.text
    assert "display_name=missing.txt reason=missing_path detail=文件路径不存在" in caplog.text
    assert str(tmp_path) not in caplog.text


async def test_folder_is_admitted_without_recursive_read(tmp_path: Path) -> None:
    """验证文件夹热路径只登记引用，不递归统计内部文件。"""

    folder = tmp_path / "bundle"
    folder.mkdir()
    (folder / "readme.md").write_text("content", encoding="utf-8")
    (folder / "program.exe").write_bytes(b"binary")
    service = TurnMaterialContextService()

    result = await service.append_staged(
        attachments=[{
            **_file_item(folder),
            "kind": "dragged_folder",
        }],
        created_at="2026-07-11T10:00:00+08:00",
    )

    assert result.to_payload()["added_count"] == 1
    assert result.added[0].media_class == "mixed_or_document"
    assert result.added[0].node_kind == "directory"
    assert service.staged_preflight_snapshot()[2] == "pending"


async def test_file_entry_rejects_a_real_directory(tmp_path: Path) -> None:
    """普通文件入口不能把真实目录误接纳成单文件材料。"""

    folder = tmp_path / "report.pdf"
    folder.mkdir()
    service = TurnMaterialContextService()

    result = await service.append_staged(
        attachments=[_file_item(folder)],
        created_at="2026-08-23T10:00:00+08:00",
    )

    assert result.to_payload()["added_count"] == 0
    assert result.to_payload()["issues"] == [{
        "submitted_index": 0,
        "display_name": "report.pdf",
        "category": "unreadable",
        "reason": "not_regular_file",
    }]


async def test_folder_entry_rejects_a_real_file(tmp_path: Path) -> None:
    """目录入口不能把普通文件误接纳成目录根能力。"""

    file_path = tmp_path / "project"
    file_path.write_text("content", encoding="utf-8")
    service = TurnMaterialContextService()

    result = await service.append_staged(
        attachments=[{**_file_item(file_path), "kind": "dragged_folder"}],
        created_at="2026-08-23T10:00:00+08:00",
    )

    assert result.to_payload()["added_count"] == 0
    assert result.to_payload()["issues"] == [{
        "submitted_index": 0,
        "display_name": "project",
        "category": "unreadable",
        "reason": "not_regular_file",
    }]


async def test_windows_shortcut_is_filtered_without_affecting_supported_files(tmp_path: Path) -> None:
    """验证桌面混合选择中的快捷方式只计入不支持项。"""

    supported_path = tmp_path / "notes.txt"
    supported_path.write_text("content", encoding="utf-8")
    shortcut_path = tmp_path / "application.lnk"
    shortcut_path.write_bytes(b"shortcut")
    service = TurnMaterialContextService()

    result = await service.append_staged(
        attachments=[_file_item(shortcut_path), _file_item(supported_path)],
        created_at="2026-07-12T00:00:00+08:00",
    )

    assert result.to_payload() == {
        "submitted_count": 2,
        "added_count": 1,
        "unsupported_count": 1,
        "unreadable_count": 0,
        "duplicate_count": 0,
        "issues": [
            {"submitted_index": 0, "display_name": "application.lnk", "category": "unsupported", "reason": "unsupported_extension"},
        ],
    }
    assert [item.display_name for item in service.staged_materials()] == ["notes.txt"]


@pytest.mark.asyncio
async def test_staged_material_route_returns_real_admission(tmp_path: Path) -> None:
    """验证 staged 路由返回服务层真实批次统计而非提交数量。"""

    supported_path = tmp_path / "notes.md"
    supported_path.write_text("content", encoding="utf-8")
    unsupported_path = tmp_path / "archive.exe"
    unsupported_path.write_bytes(b"binary")
    turn_materials = TurnMaterialContextService()
    lifecycle = SimpleNamespace(broadcast_changed=_async_snapshot, schedule_staged_preflight=lambda: None)
    container = SimpleNamespace(
        hub=SimpleNamespace(turn_materials=turn_materials),
        material_lifecycle=lifecycle,
    )

    response = await append_staged_turn_materials(
        MaterialItemsRequest(items=[_file_item(supported_path), _file_item(unsupported_path)]),
        container,
    )

    assert response["admission"] == {
        "submitted_count": 2,
        "added_count": 1,
        "unsupported_count": 1,
        "unreadable_count": 0,
        "duplicate_count": 0,
        "issues": [
            {"submitted_index": 1, "display_name": "archive.exe", "category": "unsupported", "reason": "unsupported_extension"},
        ],
    }
    assert len(response["materials"]) == 1
    assert isinstance(response["materials"][0]["created_at"], str)
    json.dumps(response["material_context"])


async def test_concurrent_batches_commit_same_path_only_once(tmp_path: Path) -> None:
    """验证并发批次会在提交阶段复核重复项。"""

    supported_path = tmp_path / "shared.md"
    supported_path.write_text("content", encoding="utf-8")
    service = TurnMaterialContextService()

    first, second = await asyncio.gather(
        service.append_staged(attachments=[_file_item(supported_path)], created_at="2026-07-12T00:00:00+08:00"),
        service.append_staged(attachments=[_file_item(supported_path)], created_at="2026-07-12T00:00:00+08:00"),
    )

    assert sorted([first.to_payload()["added_count"], second.to_payload()["added_count"]]) == [0, 1]
    assert sorted([first.to_payload()["duplicate_count"], second.to_payload()["duplicate_count"]]) == [0, 1]
    assert len(service.staged_materials()) == 1


async def test_delete_route_returns_authoritative_snapshot_and_released_asset(tmp_path: Path, caplog) -> None:
    """验证删除接口返回删除后的权威快照和受管资产编号。"""

    supported_path = tmp_path / "managed.md"
    supported_path.write_text("content", encoding="utf-8")
    service = TurnMaterialContextService()
    admission = await service.append_staged(
        attachments=[{**_file_item(supported_path), "managed_asset_id": "asset-managed"}],
        created_at="2026-07-12T00:00:00+08:00",
    )
    material_id = admission.added[0].material_id
    snapshot = {
        "turn_material_context": {"staged_materials": [], "active": None},
    }
    lifecycle = SimpleNamespace(
        broadcast_changed=lambda released: _async_value(snapshot, released),
        schedule_staged_preflight=lambda: None,
    )
    container = SimpleNamespace(
        hub=SimpleNamespace(turn_materials=service),
        material_lifecycle=lifecycle,
    )

    with caplog.at_level(logging.INFO, logger="companion.materials.turn_context"):
        response = await delete_staged_turn_material(material_id, container)

    assert response == {
        "accepted": True,
        "deleted_material_id": material_id,
        "deleted_material": {
            "material_id": material_id,
            "display_name": "managed.md",
        },
        "released_managed_asset_ids": ["asset-managed"],
        "material_context": snapshot,
    }
    assert service.staged_materials() == []
    assert (
        f"staged 材料删除成功 material_id={material_id} display_name=managed.md "
        "remaining_count=0 released_managed_asset_count=1"
    ) in caplog.text


def test_remove_missing_staged_material_logs_failure_and_remaining_count(tmp_path: Path, caplog) -> None:
    """验证删除未命中时日志会显示失败和当前剩余数量。"""

    service = TurnMaterialContextService()

    with caplog.at_level(logging.WARNING, logger="companion.materials.turn_context"):
        released = service.remove_staged("missing-material")

    assert released is None
    assert (
        "staged 材料删除失败 material_id=missing-material reason=not_found remaining_count=0"
        in caplog.text
    )


def _file_item(path: Path) -> dict[str, object]:
    """生成文件材料测试载荷。"""

    return {
        "kind": "dragged_file",
        "source": "selection_capture",
        "display_name": path.name,
        "path": str(path),
    }


async def _async_none() -> None:
    """模拟材料生命周期广播完成。"""


async def _async_snapshot() -> dict[str, object]:
    """模拟添加后返回带 revision 的权威快照。"""

    return {"revision": 1, "turn_material_context": {"staged_materials": [], "active": None}}


async def _async_value(value: dict[str, object], released: list[str]) -> dict[str, object]:
    """模拟生命周期返回权威快照并验证释放编号。"""

    assert released == ["asset-managed"]
    return value
