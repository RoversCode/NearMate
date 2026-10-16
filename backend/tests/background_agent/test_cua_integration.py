from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
from threading import Event
from pathlib import Path

import pytest
from cua_driver import (
    EmbeddedDriverConnection,
    EmbeddedEnvironmentVariable,
    EmbeddedMcpConfiguration,
)

from companion.background_agent import cua_integration as integration_module
from companion.background_agent.cua_integration import (
    CUA_DRIVER_VERSION,
    CUA_SESSION_CLEANUP_TIMEOUT_SECONDS,
    CuaIntegration,
    CuaIntegrationError,
)


class _FakeEmbeddedHost:
    """记录官方 Host start/stop 调用的异步测试替身。"""

    def __init__(self, connection: EmbeddedDriverConnection) -> None:
        """保存 start 将返回的固定连接。"""

        self.connection_value = connection
        self.start_calls = 0
        self.stop_calls = 0

    async def start(self) -> EmbeddedDriverConnection:
        """返回同一个共享 Runtime 连接。"""

        self.start_calls += 1
        return self.connection_value

    async def stop(self) -> None:
        """记录官方幂等关闭调用。"""

        self.stop_calls += 1


class _BlockingEmbeddedHost(_FakeEmbeddedHost):
    """让 Host.stop 忽略短时关闭预算，验证同步桥接不会无限等待。"""

    def __init__(self, connection: EmbeddedDriverConnection) -> None:
        super().__init__(connection)
        self.stop_started = Event()
        self.stop_finished = Event()
        self.release_stop = Event()

    async def stop(self) -> None:
        """等待测试释放关闭动作。

        Returns:
            无返回值；完成和开始事件由测试夹具记录。
        """

        self.stop_calls += 1
        self.stop_started.set()
        try:
            while not self.release_stop.is_set():
                await asyncio.sleep(0.01)
        finally:
            self.stop_finished.set()


def _write_official_skill(skill_root: Path, *, version: str = CUA_DRIVER_VERSION) -> None:
    """创建满足集成校验的最小官方 Skill 测试资产。"""

    skill_root.mkdir(parents=True)
    (skill_root / "SKILL.md").write_text(
        "---\nname: cua-driver\ndescription: 官方测试 Skill\n"
        f"version: {version}\n---\n",
        encoding="utf-8",
    )
    (skill_root / "WINDOWS.md").write_text("Windows", encoding="utf-8")
    (skill_root / "BROWSER.md").write_text("Browser", encoding="utf-8")


def _connection(executable: str) -> EmbeddedDriverConnection:
    """生成 MCP 与 CLI 指向同一 socket 的官方连接对象。"""

    return EmbeddedDriverConnection(
        socket_path=r"\\.\pipe\nearmate-cua-test",
        pid=1234,
        generation="generation-a",
        driver_version=CUA_DRIVER_VERSION,
        contract_version="1",
        mcp_protocol_version="2025-03-26",
        mcp=EmbeddedMcpConfiguration(
            command=executable,
            args=["mcp", "--socket", r"\\.\pipe\nearmate-cua-test"],
            environment=[
                EmbeddedEnvironmentVariable(name="CUA_TEST", value="connected")
            ],
        ),
    )


def _install_validation_stub(
    monkeypatch: pytest.MonkeyPatch,
    *,
    executable: Path,
    kwargs_log: list[dict[str, object]] | None = None,
) -> list[tuple[str, ...]]:
    """替换 manifest/version/doctor 三条只读检查。

    Args:
        monkeypatch: Pytest 提供的临时属性替换工具。
        executable: 测试使用的官方 Binary 路径。
        kwargs_log: 可选的 subprocess 关键字参数记录列表。

    Returns:
        按调用顺序保存的命令参数列表。
    """

    calls: list[tuple[str, ...]] = []

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        """按命令返回固定官方检查结果。"""

        calls.append(tuple(command))
        if kwargs_log is not None:
            kwargs_log.append(dict(kwargs))
        if command[1:] == ["manifest"]:
            payload: object = {
                "schema_version": "1",
                "binary_version": CUA_DRIVER_VERSION,
                "binary_path": str(executable),
            }
            stdout = json.dumps(payload)
        elif command[1:] == ["--version"]:
            stdout = f"cua-driver {CUA_DRIVER_VERSION}"
        elif command[1:] == ["doctor", "--json"]:
            stdout = json.dumps(
                {
                    "ok": True,
                    "probes": [
                        {"label": "interactive session", "status": "ok"},
                        {"label": "UI Automation", "status": "ok"},
                        {
                            "label": "EnumWindows visible",
                            "status": "ok",
                            "message": "12 windows",
                        },
                    ],
                }
            )
        elif command[1:3] == ["call", "end_session"]:
            stdout = json.dumps({"active": False})
        else:
            raise AssertionError(f"未声明的 CUA 命令：{command}")
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(integration_module.subprocess, "run", fake_run)
    monkeypatch.setattr(integration_module.CuaIntegration, "_validate_integrity_level", staticmethod(lambda: None))
    return calls


@pytest.mark.asyncio
async def test_embedded_host_connection_is_the_only_mcp_and_cli_source(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """MCP 配置和 CLI socket 必须来自同一个官方 Connection。"""

    executable = tmp_path / "cua-driver.exe"
    executable.write_bytes(b"official")
    skill_root = tmp_path / "skill"
    _write_official_skill(skill_root)
    calls = _install_validation_stub(monkeypatch, executable=executable)
    connection = _connection(str(executable))
    host = _FakeEmbeddedHost(connection)
    integration = CuaIntegration(
        executable=str(executable),
        skill_root=skill_root,
        host_factory=lambda _binary, _bundle: host,
    )

    started_connection = await integration.start()

    assert started_connection is connection
    assert integration.browser_socket_path == connection.socket_path
    assert integration.mcp_server_config() == {
        "type": "stdio",
        "command": str(executable),
        "args": ["mcp", "--socket", connection.socket_path],
        "env": {"CUA_TEST": "connected"},
    }
    assert host.start_calls == 1
    assert [call[1:] for call in calls] == [
        ("manifest",),
        ("--version",),
        ("doctor", "--json"),
    ]


def test_mcp_server_config_does_not_start_or_probe_runtime(tmp_path: Path) -> None:
    """重复读取 MCP 配置不能隐式预检或阻塞调用线程。"""

    host = _FakeEmbeddedHost(_connection("cua-driver"))
    integration = CuaIntegration(
        executable="cua-driver",
        skill_root=tmp_path / "skill",
        host_factory=lambda _binary, _bundle: host,
    )

    with pytest.raises(CuaIntegrationError, match="尚未启动"):
        integration.mcp_server_config()

    assert host.start_calls == 0


def test_transport_instructions_route_visible_urls_to_computer_mcp(
    tmp_path: Path,
) -> None:
    """用户只想看到网页时，Agent 必须打开日常默认浏览器。

    Args:
        tmp_path: pytest 提供的隔离目录。

    Returns:
        无返回值；声明内容由断言验证。
    """

    integration = CuaIntegration(
        executable="cua-driver",
        skill_root=tmp_path / "skill",
    )

    instructions = integration.transport_instructions(
        "activity-a",
        browser_guide_path=tmp_path / "BROWSER.md",
    )

    assert "用户可见网页启动" in instructions
    assert "`mcp__cua_driver__launch_app`" in instructions
    assert "参数只传 `urls`" in instructions
    assert "不得调用 `browser_prepare`" in instructions
    assert "日常默认浏览器" in instructions
    assert "Activity 完成后保持打开" in instructions
    assert "不得回退到 Browser CLI" in instructions


def test_transport_instructions_route_page_automation_to_browser_cli(
    tmp_path: Path,
) -> None:
    """网页读取和交互必须留在隔离 Browser CLI 流程中。

    Args:
        tmp_path: pytest 提供的隔离目录。

    Returns:
        无返回值；声明内容由断言验证。
    """

    integration = CuaIntegration(
        executable="cua-driver",
        skill_root=tmp_path / "skill",
    )
    expected_session = integration.browser_cli_session_name("activity-a")

    instructions = integration.transport_instructions(
        "activity-a",
        browser_guide_path=tmp_path / "BROWSER.md",
    )

    assert f"Browser CLI Session `{expected_session}`" in instructions
    assert "页面自动化" in instructions
    assert "读取、搜索、点击、填写或下载" in instructions
    assert "从 `browser_prepare` 开始全程使用 Browser CLI 和隔离 Profile" in instructions
    assert "不得改用日常默认浏览器" in instructions
    assert "Browser 自动化使用 `isolated_named` Profile `nearmate-default`" in instructions
    assert "Browser MCP 工具" in instructions
    assert "不能在 MCP 与 CLI 之间传递" in instructions
    assert "Browser CLI 的上传和下载可直接使用官方工具" in instructions
    assert '--socket "$NEARMATE_CUA_BROWSER_SOCKET"' in instructions


def test_transport_instructions_define_mixed_delivery_and_ambiguity(
    tmp_path: Path,
) -> None:
    """混合任务只能交付已确认 URL，歧义目标必须先询问用户。

    Args:
        tmp_path: pytest 提供的隔离目录。

    Returns:
        无返回值；声明内容由断言验证。
    """

    integration = CuaIntegration(
        executable="cua-driver",
        skill_root=tmp_path / "skill",
    )

    instructions = integration.transport_instructions(
        "activity-a",
        browser_guide_path=tmp_path / "BROWSER.md",
    )

    assert "明确要求“打开给我看”" in instructions
    assert "先用 Browser CLI 查找或操作" in instructions
    assert (
        "完成后只能把 fresh snapshot 中确认的最终 URL 交给 "
        "`mcp__cua_driver__launch_app`"
    ) in instructions
    assert "`AskUserQuestion`" in instructions
    assert "任何 CUA 调用前" in instructions
    assert (
        "不能在日常默认浏览器与 `nearmate-default` 之间传递 "
        "Cookie、Session、tab、ref 或登录态"
    ) in instructions


def test_transport_instructions_finish_final_url_delivery_without_window_checks(
    tmp_path: Path,
) -> None:
    """纯网页交付在 launch_app 无错误后必须立即收口。

    Args:
        tmp_path: pytest 提供的隔离目录。
    """

    integration = CuaIntegration(
        executable="cua-driver",
        skill_root=tmp_path / "skill",
    )

    instructions = integration.transport_instructions(
        "activity-a",
        browser_guide_path=tmp_path / "BROWSER.md",
    )

    assert "完整任务已经没有其他未完成目标" in instructions
    assert "剩余目标仅仅是把网页交给用户的默认浏览器" in instructions
    assert "ToolResult 没有错误或 refusal" in instructions
    assert "立即提交 StructuredOutput" in instructions
    assert "禁止继续调用 `list_windows`、`get_window_state`" in instructions
    assert "禁止检查窗口是否最小化、是否 active 或页面是否完全加载" in instructions
    assert '即使返回 `{"running": true, "active": false, "windows": []}`' in instructions
    assert "目标网页已交给系统默认浏览器打开" in instructions
    assert "不能声称已经确认页面加载完成" in instructions


def test_transport_instructions_keep_mixed_web_goals_and_bound_site_resolution(
    tmp_path: Path,
) -> None:
    """读取型网页任务不能提前结束，陌生或歧义网站也不能猜域名。

    Args:
        tmp_path: pytest 提供的隔离目录。
    """

    integration = CuaIntegration(
        executable="cua-driver",
        skill_root=tmp_path / "skill",
    )

    instructions = integration.transport_instructions(
        "activity-a",
        browser_guide_path=tmp_path / "BROWSER.md",
    )

    assert "仍有读取、搜索、点击、填写或下载目标时不得提前结束" in instructions
    assert "陌生网站只允许一次有界搜索或解析" in instructions
    assert "存在多个候选时使用 `AskUserQuestion`" in instructions
    assert "不得猜测域名" in instructions
    assert "不得因为验证欲望重复调用 `mcp__cua_driver__launch_app`" in instructions
    assert "不得改用 Shell 或其它浏览器启动方式" in instructions


@pytest.mark.skipif(os.name != "nt", reason="只验证 Windows 进程 Token")
def test_windows_integrity_level_reads_current_process_token() -> None:
    """64 位 Windows 下必须真实读到当前进程的 Integrity Level。"""

    integrity_level = integration_module._windows_integrity_level()

    assert isinstance(integrity_level, int)
    assert integrity_level > 0


@pytest.mark.asyncio
async def test_high_integrity_fails_before_host_is_created(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """High/System Integrity 下不能启动 Embedded Host。"""

    executable = tmp_path / "cua-driver.exe"
    executable.write_bytes(b"official")
    created_hosts: list[object] = []
    monkeypatch.setattr(integration_module, "_windows_integrity_level", lambda: 0x3000)
    integration = CuaIntegration(
        executable=str(executable),
        skill_root=tmp_path / "skill",
        host_factory=lambda _binary, _bundle: created_hosts.append(object()),
    )

    with pytest.raises(CuaIntegrationError, match="Integrity"):
        await integration.start()

    assert created_hosts == []


@pytest.mark.asyncio
async def test_shutdown_uses_official_host_stop_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Assistant shutdown 只调用官方 Host.stop，不持有 Popen 或 kill 逻辑。"""

    executable = tmp_path / "cua-driver.exe"
    executable.write_bytes(b"official")
    skill_root = tmp_path / "skill"
    _write_official_skill(skill_root)
    _install_validation_stub(monkeypatch, executable=executable)
    host = _FakeEmbeddedHost(_connection(str(executable)))
    integration = CuaIntegration(
        executable=str(executable),
        skill_root=skill_root,
        host_factory=lambda _binary, _bundle: host,
    )
    await integration.start()

    integration.shutdown()
    integration.shutdown()

    assert host.stop_calls == 1


@pytest.mark.asyncio
async def test_shutdown_respects_timeout_when_official_host_stop_blocks(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """官方 Host.stop 卡住时，Assistant 关闭桥接必须按预算返回。

    Args:
        monkeypatch: pytest 属性替换器。
        tmp_path: pytest 提供的隔离目录。

    Returns:
        无返回值；关闭耗时和 Host 事件由断言验证。
    """

    executable = tmp_path / "cua-driver.exe"
    executable.write_bytes(b"official")
    skill_root = tmp_path / "skill"
    _write_official_skill(skill_root)
    _install_validation_stub(monkeypatch, executable=executable)
    host = _BlockingEmbeddedHost(_connection(str(executable)))
    integration = CuaIntegration(
        executable=str(executable),
        skill_root=skill_root,
        host_factory=lambda _binary, _bundle: host,
    )
    await integration.start()

    started_at = time.monotonic()
    integration.shutdown(timeout=0.05)
    elapsed = time.monotonic() - started_at

    assert elapsed < 0.5
    assert host.stop_started.is_set() is True
    assert host.stop_finished.is_set() is False

    host.release_stop.set()
    assert host.stop_finished.wait(timeout=1.0) is True


@pytest.mark.asyncio
async def test_end_session_uses_connection_socket_and_stable_activity_session(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Runner 必须通过共享 Runtime 精确结束 Activity 的 Browser CLI Session。"""

    executable = tmp_path / "cua-driver.exe"
    executable.write_bytes(b"official")
    skill_root = tmp_path / "skill"
    _write_official_skill(skill_root)
    subprocess_kwargs: list[dict[str, object]] = []
    calls = _install_validation_stub(
        monkeypatch,
        executable=executable,
        kwargs_log=subprocess_kwargs,
    )
    host = _FakeEmbeddedHost(_connection(str(executable)))
    integration = CuaIntegration(
        executable=str(executable),
        skill_root=skill_root,
        host_factory=lambda _binary, _bundle: host,
    )
    await integration.start()

    integration.end_browser_session("activity-a")

    assert calls[-1][1:] == (
        "call",
        "end_session",
        "--socket",
        integration.browser_socket_path,
    )
    assert subprocess_kwargs[-1]["timeout"] == CUA_SESSION_CLEANUP_TIMEOUT_SECONDS
    assert integration.browser_cli_session_name("activity-a") == integration.browser_cli_session_name("activity-a")
    assert integration.browser_cli_session_name("activity-a") != integration.browser_cli_session_name("activity-b")
