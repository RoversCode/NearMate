import json
import time
from pathlib import Path

from companion.filesystem.safety import (
    FileSystemNodeSnapshot,
    inspect_filesystem_node,
)
from companion.materials.models import MaterialAttachment, MaterialWorkloadProfile
from companion.materials.preflight import (
    MaterialPreflightFailure,
    MaterialPreflightResult,
    _ItemFacts,
    _inspect_folder,
    inspect_material_batch,
)
from companion.materials.turn_context import TurnMaterialContextService


async def test_admission_of_many_files_never_deep_reads_and_marks_known_over_limit(tmp_path: Path, monkeypatch) -> None:
    """验证几十项本地文件只执行元数据登记，不调用 Reader 或内容解析。"""

    paths = []
    for index in range(40):
        path = tmp_path / f"item-{index}.txt"
        path.write_text("x", encoding="utf-8")
        paths.append(path)
    monkeypatch.setattr(
        "companion.materials.readers.registry.MaterialReaderRegistry.read",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("admission must not read content")),
    )
    service = TurnMaterialContextService()
    started = time.perf_counter()

    result = await service.append_staged(
        attachments=[_item(path) for path in paths],
        created_at="2026-07-12T00:00:00+08:00",
    )

    assert result.to_payload()["added_count"] == 40
    assert service.staged_preflight_snapshot()[2] == "over_limit"
    assert time.perf_counter() - started < 0.2


def test_folder_preflight_stops_immediately_after_sixth_supported_file(tmp_path: Path, monkeypatch) -> None:
    """验证目录确认第六个支持文件后不再访问后续文件。"""

    folder = tmp_path / "bundle"
    folder.mkdir()
    for index in range(20):
        (folder / f"{index:02}.txt").write_text("x", encoding="utf-8")
    visited: list[str] = []

    def inspect(path: Path) -> _ItemFacts:
        visited.append(path.name)
        return _ItemFacts(local_files=1, text_bytes=1, total_bytes=1)

    monkeypatch.setattr("companion.materials.preflight._inspect_file", inspect)
    facts = _inspect_folder(folder, deadline=time.monotonic() + 1)

    assert facts.local_files == 6
    assert visited == [f"{index:02}.txt" for index in range(6)]


def test_folder_preflight_does_not_enter_reparse_directory(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """验证目录预检使用统一重解析点判断，不进入 Junction 形状的子目录。

    Args:
        tmp_path: pytest 提供的临时目录。
        monkeypatch: pytest 提供的函数替换工具。
    """

    folder = tmp_path / "bundle"
    unsafe_directory = folder / "linked-project"
    unsafe_directory.mkdir(parents=True)
    (unsafe_directory / "secret.txt").write_text("secret", encoding="utf-8")
    (folder / "visible.txt").write_text("visible", encoding="utf-8")

    def inspect_with_reparse_directory(path: Path) -> FileSystemNodeSnapshot:
        snapshot = inspect_filesystem_node(path)
        if path == unsafe_directory:
            return FileSystemNodeSnapshot(
                node_kind="directory",
                is_linked_or_reparse=True,
                size=0,
            )
        return snapshot

    monkeypatch.setattr(
        "companion.materials.preflight.inspect_filesystem_node",
        inspect_with_reparse_directory,
    )

    facts = _inspect_folder(folder, deadline=time.monotonic() + 1)

    assert facts.local_files == 1
    assert facts.text_bytes == len("visible")


def test_corrupt_top_level_item_is_filtered_while_remaining_material_stays_ready(tmp_path: Path) -> None:
    """验证单项损坏只生成稳定失败，不阻断其余有效材料。"""

    bad_image = tmp_path / "bad.png"
    bad_image.write_bytes(b"not-an-image")
    notes = tmp_path / "notes.txt"
    notes.write_text("hello", encoding="utf-8")

    result = inspect_material_batch([
        _attachment("bad", bad_image, "mixed_or_document"),
        _attachment("notes", notes, "text"),
    ])

    assert result.status == "ready"
    assert [failure.material_id for failure in result.failures] == ["bad"]
    assert [failure.reason for failure in result.failures] == ["invalid_image"]
    assert result.profile.local_file_count == 1
    assert result.profile.task_result_material_eligible is True


def test_web_selection_v2_counts_plain_text_once_and_only_images_as_visual_units(
    tmp_path: Path,
) -> None:
    """网页选区预算不能重复计算 DOM 文字，普通链接也不属于视觉单元。"""

    selection_path = tmp_path / "selection.json"
    selection_path.write_text(
        json.dumps(
            {
                "version": 2,
                "plain_text": "权威正文",
                "parts": [
                    {"type": "text", "text": "权威"},
                    {
                        "type": "link",
                        "text": "正文",
                        "url": "http:example.com",
                    },
                    {"type": "image_url", "url": "/relative/cover.png"},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = inspect_material_batch([
        MaterialAttachment(
            material_id="selection",
            turn_id="staged",
            kind="selected_web",
            source="selection_capture",
            display_name="网页选区",
            path=str(selection_path),
            created_at="2026-09-06T00:00:00+08:00",
            node_kind="file",
            media_class="mixed_or_document",
        )
    ])

    assert result.status == "ready"
    assert result.profile.text_byte_count == len("权威正文".encode("utf-8"))
    assert result.profile.visual_unit_count == 1


def test_large_video_is_path_only_and_does_not_consume_mixed_content_budget(
    tmp_path: Path,
) -> None:
    """验证大视频只记录真实大小，不触发旧图片和文档内容预算。

    Args:
        tmp_path: pytest 提供的临时目录。
    """

    video = tmp_path / "recording.mp4"
    with video.open("wb") as stream:
        # 使用稀疏文件模拟超过原 10 MiB 上限的视频，避免测试真实写入大块数据。
        stream.seek(32 * 1024 * 1024)
        stream.write(b"x")

    result = inspect_material_batch([
        _attachment("video", video, "video"),
    ])

    assert result.status == "ready"
    assert result.profile.local_file_count == 1
    assert result.profile.local_total_bytes > 10 * 1024 * 1024
    assert result.profile.blocking_reasons == ()


def test_audio_and_video_in_folder_are_not_decoded_as_text(tmp_path: Path) -> None:
    """验证文件夹预检只统计媒体路径，不尝试 UTF-8 解码。

    Args:
        tmp_path: pytest 提供的临时目录。
    """

    folder = tmp_path / "media"
    folder.mkdir()
    (folder / "voice.mp3").write_bytes(b"\xff\xfbnot-real-but-path-valid")
    (folder / "movie.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42")

    result = inspect_material_batch([
        _attachment("folder", folder, "mixed_or_document"),
    ])

    assert result.status == "ready"
    assert result.profile.local_file_count == 2
    assert result.profile.local_total_bytes > 0
    assert result.profile.text_byte_count == 0


async def test_partial_preflight_failure_blocks_task_result_after_turn_freeze(
    tmp_path: Path,
) -> None:
    """同批材料存在读取失败时，剩余附件和短粘贴不得重新放行答案产物。"""

    bad_image = tmp_path / "bad.png"
    bad_image.write_bytes(b"not-an-image")
    notes = tmp_path / "notes.txt"
    notes.write_text("hello", encoding="utf-8")
    service = TurnMaterialContextService()
    await service.append_staged(
        attachments=[_item(bad_image), _item(notes)],
        created_at="2026-07-21T00:00:00+08:00",
    )
    generation, attachments, _ = service.staged_preflight_snapshot()
    result = inspect_material_batch(attachments)
    service.apply_preflight_result(generation, result)
    context = service.activate_turn(
        turn_id="turn-partial-failure",
        created_at="2026-07-21T00:00:01+08:00",
    )

    frozen = service.freeze_active_context(context.context_id)
    assert frozen is not None
    frozen_context, profile = frozen
    assert len(frozen_context.attachments) == 1
    assert len(frozen_context.preflight_failures) == 1
    assert profile.task_result_material_eligible is False
    assert "preflight_failed" in profile.blocking_reasons


async def test_deleted_generation_rejects_late_preflight_result(tmp_path: Path) -> None:
    """验证删除后旧 generation 的迟到结果不会修改 staged 状态。"""

    path = tmp_path / "notes.txt"
    path.write_text("hello", encoding="utf-8")
    service = TurnMaterialContextService()
    admission = await service.append_staged(
        attachments=[{**_item(path), "managed_asset_id": "asset-1"}],
        created_at="2026-07-12T00:00:00+08:00",
    )
    generation, _, _ = service.staged_preflight_snapshot()
    service.remove_staged(admission.added[0].material_id)
    late = MaterialPreflightResult("ready", _ready_profile(), ())

    assert service.apply_preflight_result(generation, late) is None
    assert service.staged_materials() == []


async def test_foreground_freeze_closes_pending_generation_and_ignores_late_failure(tmp_path: Path) -> None:
    """验证 prompt 与工具冻结后迟到失败既不删材料也不再产生反馈。"""

    path = tmp_path / "notes.txt"
    path.write_text("hello", encoding="utf-8")
    service = TurnMaterialContextService()
    admission = await service.append_staged(
        attachments=[_item(path)],
        created_at="2026-07-12T00:00:00+08:00",
    )
    generation, _, _ = service.staged_preflight_snapshot()
    context = service.activate_turn(turn_id="turn-1", created_at="2026-07-12T00:00:01+08:00")

    frozen = service.freeze_active_context(context.context_id)
    assert frozen is not None
    _, profile = frozen
    late = MaterialPreflightResult(
        "ready",
        _ready_profile(),
        (MaterialPreflightFailure(admission.added[0].material_id, "notes.txt", "unreadable_file"),),
    )

    assert profile.task_result_material_eligible is False
    assert service.apply_preflight_result(generation, late) is None
    assert len(service.active_context(context.context_id).attachments) == 1  # type: ignore[union-attr]


def test_empty_turn_freezes_to_neutral_material_profile() -> None:
    """无附件且无预检失败的 turn 不得携带 closed 阻断派生来源。"""

    service = TurnMaterialContextService()
    context = service.activate_turn(
        turn_id="turn-empty",
        created_at="2026-07-21T00:00:00+08:00",
    )

    frozen = service.freeze_active_context(context.context_id)
    assert frozen is not None
    _, profile = frozen

    assert profile.task_result_material_eligible is False
    assert profile.text_byte_count == 0
    assert profile.blocking_reasons == ()


def _item(path: Path) -> dict[str, object]:
    """生成本地文件接纳载荷。"""

    return {
        "kind": "dragged_file",
        "source": "test",
        "display_name": path.name,
        "path": str(path),
    }


def _attachment(material_id: str, path: Path, media_class: str) -> MaterialAttachment:
    """生成预检使用的稳定材料引用。"""

    return MaterialAttachment(
        material_id=material_id,
        turn_id="staged",
        kind="dragged_file",
        source="test",
        display_name=path.name,
        path=str(path),
        created_at="2026-07-12T00:00:00+08:00",
        node_kind="file",
        media_class=media_class,  # type: ignore[arg-type]
    )


def _ready_profile() -> MaterialWorkloadProfile:
    """生成限内且可提供产物工具的画像。"""

    return MaterialWorkloadProfile(True, 1, 0, 5, 5, False, 0, ())
