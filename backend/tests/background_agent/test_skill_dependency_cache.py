"""验证 Skill 依赖检查的去重、短缓存和并发边界。"""

from __future__ import annotations

import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, Lock

import pytest

from companion.background_agent.skill_registry import SkillRegistry


def test_execution_snapshot_checks_each_unique_dependency_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """执行快照检查全库，但多个 Skill 的相同声明只探测一次。"""

    _write_skill(tmp_path, skill_id="audio-convert", dependency="shared-tool")
    _write_skill(tmp_path, skill_id="video-convert", dependency="shared-tool")
    _write_skill(tmp_path, skill_id="document-read", dependency="document-tool")
    checked_names: list[str] = []

    def find_executable(name: str) -> None:
        """记录本次真正执行的 PATH 查询。

        Args:
            name: 依赖清单声明的命令名。

        Returns:
            始终返回空，模拟命令未安装。
        """

        checked_names.append(name)
        return None

    monkeypatch.setattr(
        "companion.background_agent.skill_dependencies.shutil.which",
        find_executable,
    )
    registry = SkillRegistry(asset_root=tmp_path)

    snapshot = registry.execution_snapshot()

    assert snapshot.enabled_skill_ids == ()
    assert checked_names.count("shared-tool") == 1
    assert checked_names.count("document-tool") == 1


def test_dependency_cache_hits_then_expires_with_injected_clock(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """TTL 内复用依赖状态，到期后使用可控时钟触发重新检查。"""

    _write_skill(tmp_path, skill_id="document-read", dependency="document-tool")
    current_time = [100.0]
    check_count = 0

    def find_executable(_name: str) -> None:
        """统计真实 PATH 查询次数。

        Args:
            _name: 依赖清单声明的命令名。

        Returns:
            始终返回空，模拟命令未安装。
        """

        nonlocal check_count
        check_count += 1
        return None

    monkeypatch.setattr(
        "companion.background_agent.skill_dependencies.shutil.which",
        find_executable,
    )
    registry = SkillRegistry(
        asset_root=tmp_path,
        dependency_cache_ttl_seconds=5.0,
        dependency_clock=lambda: current_time[0],
    )

    registry.status_snapshot()
    current_time[0] += 4.9
    registry.detail("document-read")

    assert check_count == 1

    current_time[0] += 0.1
    registry.status_snapshot()

    assert check_count == 2


def test_dependency_cache_key_includes_minimum_version(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """同名命令的不同最低版本声明必须分别计算依赖状态。"""

    _write_skill(
        tmp_path,
        skill_id="legacy-convert",
        dependency="shared-tool",
        minimum_version="1.0.0",
    )
    _write_skill(
        tmp_path,
        skill_id="modern-convert",
        dependency="shared-tool",
        minimum_version="2.0.0",
    )
    subprocess_count = 0
    monkeypatch.setattr(
        "companion.background_agent.skill_dependencies.shutil.which",
        lambda _name: "C:/tools/shared-tool.exe",
    )

    def read_version(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        """统计不同完整依赖声明触发的版本检查。

        Args:
            _args: subprocess.run 的位置参数。
            _kwargs: subprocess.run 的关键字参数。

        Returns:
            固定报告 2.0.0 的命令结果。
        """

        nonlocal subprocess_count
        subprocess_count += 1
        return subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="shared-tool version 2.0.0",
            stderr="",
        )

    monkeypatch.setattr(
        "companion.background_agent.skill_dependencies.subprocess.run",
        read_version,
    )

    statuses = SkillRegistry(asset_root=tmp_path).status_snapshot().statuses

    assert all(status.ready for status in statuses)
    assert subprocess_count == 2


def test_registry_refresh_clears_only_its_own_dependency_cache(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """refresh 立即重查当前 Registry，另一个实例也不共享旧状态。"""

    _write_skill(tmp_path, skill_id="document-read", dependency="document-tool")
    check_count = 0

    def find_executable(_name: str) -> None:
        """统计不同 Registry 发起的真实 PATH 查询。

        Args:
            _name: 依赖清单声明的命令名。

        Returns:
            始终返回空，模拟命令未安装。
        """

        nonlocal check_count
        check_count += 1
        return None

    monkeypatch.setattr(
        "companion.background_agent.skill_dependencies.shutil.which",
        find_executable,
    )
    first_registry = SkillRegistry(asset_root=tmp_path)
    second_registry = SkillRegistry(asset_root=tmp_path)

    first_registry.status_snapshot()
    second_registry.status_snapshot()
    assert check_count == 2

    first_registry.status_snapshot()
    assert check_count == 2

    first_registry.refresh()
    first_registry.status_snapshot()
    assert check_count == 3


def test_skill_detail_checks_only_requested_skill_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """详情读取不能因为无关 Skill 很多而扫描整库依赖。"""

    _write_skill(tmp_path, skill_id="target-skill", dependency="target-tool")
    _write_skill(tmp_path, skill_id="unrelated-skill", dependency="unrelated-tool")
    checked_names: list[str] = []
    monkeypatch.setattr(
        "companion.background_agent.skill_dependencies.shutil.which",
        lambda name: checked_names.append(name),
    )
    registry = SkillRegistry(asset_root=tmp_path)

    detail = registry.detail("target-skill")

    assert detail["id"] == "target-skill"
    assert checked_names == ["target-tool"]


def test_skill_update_checks_target_once_and_ignores_unrelated_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """PATCH 只检查目标依赖一次，响应阶段不再重复扫描。"""

    _write_skill(tmp_path, skill_id="target-skill", dependency="target-tool")
    _write_skill(tmp_path, skill_id="unrelated-skill", dependency="unrelated-tool")
    checked_names: list[str] = []
    monkeypatch.setattr(
        "companion.background_agent.skill_dependencies.shutil.which",
        lambda name: checked_names.append(name),
    )
    registry = SkillRegistry(asset_root=tmp_path)
    expected_revision = registry.revision_for_preferences({})

    _preferences, status, _revision = registry.update_enabled(
        preferences={},
        skill_id="target-skill",
        enabled=False,
        expected_revision=expected_revision,
    )

    assert status.enabled is False
    assert checked_names == ["target-tool"]


def test_concurrent_dependency_reads_share_one_in_flight_check(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """多个线程同时冷读同一依赖时只能有一个线程执行真实探测。"""

    _write_skill(
        tmp_path,
        skill_id="document-read",
        dependency="document-tool",
        minimum_version="1.0.0",
    )
    worker_count = 8
    workers_ready = Event()
    allow_probe_to_finish = Event()
    probe_started = Event()
    state_lock = Lock()
    ready_count = 0
    subprocess_count = 0

    def read_version(
        *_args: object,
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        """暂停首个版本子进程，为并发调用制造稳定重叠。

        Args:
            _args: subprocess.run 的位置参数。
            _kwargs: subprocess.run 的关键字参数。

        Returns:
            固定报告 1.0.0 的命令结果。
        """

        nonlocal subprocess_count
        with state_lock:
            subprocess_count += 1
        probe_started.set()
        if not allow_probe_to_finish.wait(timeout=2):
            raise AssertionError("依赖探测未按测试计划放行")
        return subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="document-tool version 1.0.0",
            stderr="",
        )

    def read_status(registry: SkillRegistry) -> bool:
        """等待所有线程就绪后同时读取同一 Skill 状态。

        Args:
            registry: 所有线程共享的真实 Skill Registry。

        Returns:
            目标 Skill 的依赖就绪状态。
        """

        nonlocal ready_count
        with state_lock:
            ready_count += 1
            if ready_count == worker_count:
                workers_ready.set()
        if not workers_ready.wait(timeout=2):
            raise AssertionError("并发读取线程未全部就绪")
        return registry.status_snapshot().statuses[0].ready

    monkeypatch.setattr(
        "companion.background_agent.skill_dependencies.shutil.which",
        lambda _name: "C:/tools/document-tool.exe",
    )
    monkeypatch.setattr(
        "companion.background_agent.skill_dependencies.subprocess.run",
        read_version,
    )
    registry = SkillRegistry(asset_root=tmp_path)

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = [executor.submit(read_status, registry) for _ in range(worker_count)]
        assert probe_started.wait(timeout=2)
        allow_probe_to_finish.set()
        readiness = [future.result(timeout=2) for future in futures]

    assert readiness == [True] * worker_count
    assert subprocess_count == 1


def test_distinct_dependency_checks_run_with_bounded_overlap(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """全库冷读时不同命令并行探测，不能按超时逐项串行等待。"""

    _write_skill(tmp_path, skill_id="audio-convert", dependency="audio-tool")
    _write_skill(tmp_path, skill_id="document-read", dependency="document-tool")
    both_probes_started = Event()
    allow_probes_to_finish = Event()
    state_lock = Lock()
    started_names: set[str] = set()

    def find_executable(name: str) -> None:
        """暂停两项 PATH 查询，证明它们在同一批次内发生重叠。

        Args:
            name: 当前依赖清单声明的命令名。

        Returns:
            始终返回空，模拟命令未安装。
        """

        with state_lock:
            started_names.add(name)
            if len(started_names) == 2:
                both_probes_started.set()
        if not allow_probes_to_finish.wait(timeout=2):
            raise AssertionError("不同依赖没有按测试计划并行放行")
        return None

    monkeypatch.setattr(
        "companion.background_agent.skill_dependencies.shutil.which",
        find_executable,
    )
    registry = SkillRegistry(asset_root=tmp_path)

    with ThreadPoolExecutor(max_workers=1) as executor:
        snapshot_future = executor.submit(registry.status_snapshot)
        assert both_probes_started.wait(timeout=2)
        allow_probes_to_finish.set()
        snapshot = snapshot_future.result(timeout=2)

    assert started_names == {"audio-tool", "document-tool"}
    assert all(status.ready is False for status in snapshot.statuses)


def _write_skill(
    asset_root: Path,
    *,
    skill_id: str,
    dependency: str,
    minimum_version: str | None = None,
) -> None:
    """写入依赖缓存测试使用的最小合法 Skill。

    Args:
        asset_root: 产品工作区资产根目录。
        skill_id: Skill 的稳定编号和目录名。
        dependency: 该 Skill 声明的外部命令名。
        minimum_version: 可选的最低版本要求。

    Returns:
        无返回值。
    """

    skill_root = asset_root / ".claude" / "skills" / skill_id
    skill_root.mkdir(parents=True)
    (skill_root / "SKILL.md").write_text(
        (
            "---\n"
            f'name: "{skill_id}"\n'
            "description: 依赖缓存回归测试 Skill。\n"
            "---\n\n"
            "# 使用说明\n"
        ),
        encoding="utf-8",
    )
    dependency_payload: dict[str, str] = {
        "kind": "executable",
        "name": dependency,
    }
    if minimum_version is not None:
        dependency_payload["minimum_version"] = minimum_version
    (skill_root / "manifest.json").write_text(
        json.dumps(
            {
                "id": skill_id,
                "name": f"{skill_id} 名称",
                "summary": "验证依赖缓存。",
                "category": "测试",
                "version": "1.0.0",
                "default_enabled": True,
                "dependencies": [dependency_payload],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
