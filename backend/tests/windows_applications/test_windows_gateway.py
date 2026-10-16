"""验证 Windows AppsFolder 项目的安全归一化，不调用真实 Shell。"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest

import companion.capabilities.windows_applications.windows_gateway as gateway_module
from companion.capabilities.windows_applications.models import InstalledApplication
from companion.capabilities.windows_applications.windows_gateway import (
    WindowsApplicationGateway,
    _application_from_shell_item,
    _query_window_application_id,
)


@dataclass
class FakeVerb:
    """模拟 Shell Verb。"""

    Name: str


class FakeVerbs:
    """模拟支持 Count/Item 的 COM Verb 集合。"""

    def __init__(self, names: list[str]) -> None:
        self._verbs = [FakeVerb(name) for name in names]
        self.Count = len(self._verbs)

    def Item(self, index: int) -> FakeVerb:
        """按下标返回模拟 Verb。"""

        return self._verbs[index]


class FakeShellItem:
    """模拟 AppsFolder 项目及其扩展属性。"""

    def __init__(
        self,
        *,
        name: str,
        path: str,
        target_path: str,
        application_id: str,
        verbs: list[str] | None = None,
    ) -> None:
        self.Name = name
        self.Path = path
        self._properties: dict[str, Any] = {
            "System.Link.TargetParsingPath": target_path,
            "System.AppUserModel.ID": application_id,
        }
        self._verbs = FakeVerbs(verbs or ["Open"])

    def ExtendedProperty(self, property_name: str) -> Any:
        """返回模拟扩展属性。"""

        return self._properties.get(property_name)

    def Verbs(self) -> FakeVerbs:
        """返回模拟打开动作集合。"""

        return self._verbs


def test_apps_folder_item_keeps_registered_identity_and_executable_alias() -> None:
    """不透明 AppsFolder 身份应保留注册启动目标和主程序路径。"""

    application = _application_from_shell_item(
        FakeShellItem(
            name="QQ",
            path="QQ",
            target_path="E:\\QQNT\\QQ.exe",
            application_id="QQ",
        )
    )

    assert application is not None
    assert application.display_name == "QQ"
    assert application.launch_target == "shell:AppsFolder\\QQ"
    assert application.executable_path == "E:\\QQNT\\QQ.exe"
    assert "QQ" in application.aliases


def test_direct_executable_item_launches_its_registered_file() -> None:
    """直接可执行入口不应被拼接成无效 AppsFolder URI。"""

    application = _application_from_shell_item(
        FakeShellItem(
            name="网易云音乐",
            path="E:\\CloudMusic\\cloudmusic.exe",
            target_path="E:\\CloudMusic\\cloudmusic.exe",
            application_id="E:\\CloudMusic\\cloudmusic.exe",
        )
    )

    assert application is not None
    assert application.launch_target == "E:\\CloudMusic\\cloudmusic.exe"


def test_url_shortcut_is_filtered_from_application_catalog() -> None:
    """网站快捷方式不能进入纯应用启动目录。"""

    application = _application_from_shell_item(
        FakeShellItem(
            name="哔哩哔哩官网",
            path="https://www.bilibili.com/?from_start_menu",
            target_path="https://www.bilibili.com/?from_start_menu",
            application_id="https://www.bilibili.com/?from_start_menu",
        )
    )

    assert application is None


def test_uninstaller_is_filtered_from_application_catalog() -> None:
    """卸载器不能因名称重合被同步 Tool 启动。"""

    application = _application_from_shell_item(
        FakeShellItem(
            name="卸载网易云音乐",
            path="E:\\CloudMusic\\UnInstall.exe",
            target_path="E:\\CloudMusic\\UnInstall.exe",
            application_id="E:\\CloudMusic\\UnInstall.exe",
        )
    )

    assert application is None


def test_uninstaller_marker_inside_name_is_also_filtered() -> None:
    """应用名或程序名中间出现卸载标记时也必须过滤。"""

    application = _application_from_shell_item(
        FakeShellItem(
            name="英雄联盟卸载",
            path="E:\\lol\\英雄联盟卸载.exe",
            target_path="E:\\lol\\英雄联盟卸载.exe",
            application_id="E:\\lol\\英雄联盟卸载.exe",
        )
    )

    assert application is None


def test_opaque_item_without_open_action_is_filtered() -> None:
    """没有可执行路径和打开动作的目录项目不是可启动应用。"""

    application = _application_from_shell_item(
        FakeShellItem(
            name="损坏入口",
            path="Broken.App",
            target_path="",
            application_id="Broken.App",
            verbs=["Properties"],
        )
    )

    assert application is None


def test_localized_open_verb_with_accelerator_is_accepted() -> None:
    """中文 Shell 的快捷键尾缀不能让真实打开动作被误判为不可用。"""

    application = _application_from_shell_item(
        FakeShellItem(
            name="计算器",
            path="Microsoft.WindowsCalculator_8wekyb3d8bbwe!App",
            target_path="",
            application_id="Microsoft.WindowsCalculator_8wekyb3d8bbwe!App",
            verbs=["打开(&O)"],
        )
    )

    assert application is not None


def test_snapshot_owns_com_apartment_on_inline_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """窗口 AppUserModelID 查询前后必须由当前工作线程初始化并释放 COM。

    Args:
        monkeypatch: 临时替换平台、COM 和 Win32 模块的测试夹具。
    """

    lifecycle: list[str] = []

    @contextmanager
    def recording_com_apartment() -> Any:
        """记录测试快照是否完整进入当前线程的 COM 生命周期。"""

        lifecycle.append("entered")
        try:
            yield
        finally:
            lifecycle.append("exited")

    fake_win32gui = SimpleNamespace(
        EnumWindows=lambda _callback, _context: None,
    )
    fake_win32process = SimpleNamespace(
        EnumProcesses=lambda: [],
    )
    monkeypatch.setattr(
        WindowsApplicationGateway,
        "is_supported",
        property(lambda _self: True),
    )
    monkeypatch.setattr(gateway_module, "_com_apartment", recording_com_apartment)
    monkeypatch.setitem(sys.modules, "win32gui", fake_win32gui)
    monkeypatch.setitem(sys.modules, "win32process", fake_win32process)

    result = WindowsApplicationGateway().snapshot(
        InstalledApplication(
            display_name="计算器",
            application_id="Calculator",
            launch_target="shell:AppsFolder\\Calculator",
        )
    )

    assert result.window_handles == ()
    assert result.process_ids == frozenset()
    assert lifecycle == ["entered", "exited"]


def test_window_application_id_unwraps_pypropvariant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """pywin32 的属性包装对象必须先取真实值，不能拿对象地址参与身份比较。

    Args:
        monkeypatch: 临时替换 win32com 属性模块的测试夹具。
    """

    expected_application_id = "Microsoft.WindowsCalculator_8wekyb3d8bbwe!App"

    class FakePropertyVariant:
        """模拟 pywin32 312 返回的 PyPROPVARIANT。"""

        def GetValue(self) -> str:
            """返回包装的 AppUserModelID 字符串。"""

            return expected_application_id

    fake_property_store = SimpleNamespace(
        GetValue=lambda _property_key: FakePropertyVariant()
    )
    fake_propsys = SimpleNamespace(
        SHGetPropertyStoreForWindow=lambda _window_handle: fake_property_store
    )
    fake_propsys_package = SimpleNamespace(
        pscon=SimpleNamespace(PKEY_AppUserModel_ID=object()),
        propsys=fake_propsys,
    )
    monkeypatch.setitem(
        sys.modules,
        "win32com.propsys",
        fake_propsys_package,
    )

    assert _query_window_application_id(101) == expected_application_id


def test_focus_window_attaches_input_threads_when_direct_focus_is_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """后台线程直接置前失败时，应临时接入输入队列后重试并解除连接。

    Args:
        monkeypatch: 临时替换 Win32 模块和平台判断的测试夹具。
    """

    target_window = 101
    foreground_window = 202
    target_thread = 11
    foreground_thread = 22
    current_thread = 33
    foreground_state = {"window": foreground_window}
    input_attached = {"value": False}
    attachment_calls: list[tuple[int, int, bool]] = []

    def set_foreground_window(window_handle: int) -> None:
        """只有接入输入队列后才模拟 Windows 接受置前请求。"""

        if input_attached["value"]:
            foreground_state["window"] = window_handle

    def attach_thread_input(
        source_thread: int,
        target_thread_id: int,
        attach: bool,
    ) -> None:
        """记录连接生命周期，并让第二次置前请求可以成功。"""

        attachment_calls.append((source_thread, target_thread_id, attach))
        input_attached["value"] = attach or any(
            call_attach for _source, _target, call_attach in attachment_calls
        )
        if not attach:
            input_attached["value"] = False

    fake_win32gui = SimpleNamespace(
        IsWindow=lambda _window_handle: True,
        IsIconic=lambda _window_handle: False,
        ShowWindow=lambda _window_handle, _command: None,
        BringWindowToTop=lambda _window_handle: None,
        SetForegroundWindow=set_foreground_window,
        GetForegroundWindow=lambda: foreground_state["window"],
    )
    fake_win32process = SimpleNamespace(
        GetWindowThreadProcessId=lambda window_handle: (
            (target_thread, 1)
            if window_handle == target_window
            else (foreground_thread, 2)
        ),
        AttachThreadInput=attach_thread_input,
    )
    monkeypatch.setattr(
        WindowsApplicationGateway,
        "is_supported",
        property(lambda _self: True),
    )
    monkeypatch.setitem(
        sys.modules,
        "win32api",
        SimpleNamespace(GetCurrentThreadId=lambda: current_thread),
    )
    monkeypatch.setitem(
        sys.modules,
        "win32con",
        SimpleNamespace(SW_RESTORE=9, SW_SHOW=5),
    )
    monkeypatch.setitem(sys.modules, "win32gui", fake_win32gui)
    monkeypatch.setitem(sys.modules, "win32process", fake_win32process)

    assert WindowsApplicationGateway().focus_window(target_window) is True
    assert attachment_calls == [
        (current_thread, foreground_thread, True),
        (current_thread, target_thread, True),
        (current_thread, target_thread, False),
        (current_thread, foreground_thread, False),
    ]
