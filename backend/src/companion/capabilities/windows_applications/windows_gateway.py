"""通过 Windows AppsFolder、Shell、窗口和进程 API 执行真实应用操作。"""

from __future__ import annotations

import ctypes
import os
import re
import sys
from collections.abc import Sequence
from contextlib import contextmanager
from ctypes import wintypes
from pathlib import Path
from typing import Any, Iterator

from companion.capabilities.windows_applications.models import (
    ApplicationRuntimeSnapshot,
    InstalledApplication,
)


_WINDOWS_DRIVE_PATH = re.compile(r"^[a-zA-Z]:[\\/]")
_URI_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")
_VERB_ACCELERATOR_SUFFIX = re.compile(r"\s*\([^)]{1,4}\)\s*$")
_UNINSTALL_DISPLAY_PREFIXES = (
    "卸载",
    "uninstall",
    "remove ",
)
_UNINSTALL_EXECUTABLE_PREFIXES = (
    "unins",
    "uninstall",
    "unwise",
    "remove",
)


class WindowsApplicationGateway:
    """把 Windows 原生 API 收口成应用启动业务需要的窄端口。"""

    @property
    def is_supported(self) -> bool:
        """返回当前进程是否运行在 Windows。"""

        return sys.platform == "win32"

    def list_installed_applications(self) -> tuple[InstalledApplication, ...]:
        """枚举 AppsFolder 中真实可打开的应用并过滤危险快捷方式。

        Returns:
            去除 URL、卸载器和重复入口后的不可变应用目录。
        """

        if not self.is_supported:
            return ()
        with _com_apartment():
            import win32com.client

            namespace = win32com.client.Dispatch("Shell.Application").Namespace(
                "shell:AppsFolder"
            )
            if namespace is None:
                return ()
            applications: dict[str, InstalledApplication] = {}
            for item in namespace.Items():
                application = _application_from_shell_item(item)
                if application is None:
                    continue
                identity = application.application_id.casefold()
                applications.setdefault(identity, application)
            return tuple(
                sorted(
                    applications.values(),
                    key=lambda application: (
                        application.display_name.casefold(),
                        application.application_id.casefold(),
                    ),
                )
            )

    def resolve_default_browser(
        self,
        applications: Sequence[InstalledApplication],
    ) -> InstalledApplication | None:
        """解析 Windows 默认 HTTP 浏览器并映射到应用目录。

        Args:
            applications: 当前应用目录快照。

        Returns:
            对应的目录应用；目录缺项时返回由可执行文件构造的最小描述。
        """

        if not self.is_supported:
            return None
        executable_path = _query_default_http_executable()
        if executable_path is None:
            return None
        normalized_executable = _normalize_windows_path(executable_path)
        for application in applications:
            if application.executable_path is None:
                continue
            if _normalize_windows_path(application.executable_path) == normalized_executable:
                return application

        executable = Path(executable_path)
        if not executable.is_file():
            return None
        display_name = executable.stem or "默认浏览器"
        return InstalledApplication(
            display_name=display_name,
            application_id=str(executable),
            launch_target=str(executable),
            executable_path=str(executable),
            aliases=("浏览器", "默认浏览器", display_name),
        )

    def snapshot(self, application: InstalledApplication) -> ApplicationRuntimeSnapshot:
        """返回匹配应用主程序或 AppUserModelID 的窗口和进程。

        Args:
            application: 已经从目录唯一解析出的应用。

        Returns:
            窗口保持 Windows Z 序，进程使用不可变集合。
        """

        if not self.is_supported:
            return ApplicationRuntimeSnapshot()

        # 窗口 AppUserModelID 来自 COM 属性存储。Inline 在线程池中运行，不能假定
        # 工作线程已经初始化 COM，所以整次快照都放在当前线程自己的 apartment 内。
        with _com_apartment():
            import win32gui
            import win32process

            window_handles: list[int] = []
            process_ids: set[int] = set()

            def collect_window(window_handle: int, _context: object) -> bool:
                """收集属于目标应用的普通顶层窗口。"""

                if not _is_normal_top_level_window(window_handle):
                    return True
                _thread_id, process_id = win32process.GetWindowThreadProcessId(
                    window_handle
                )
                if _window_matches_application(
                    window_handle=window_handle,
                    process_id=process_id,
                    application=application,
                ):
                    window_handles.append(int(window_handle))
                    process_ids.add(int(process_id))
                return True

            win32gui.EnumWindows(collect_window, None)
            if application.executable_path is not None:
                expected_path = _normalize_windows_path(application.executable_path)
                for process_id in win32process.EnumProcesses():
                    process_path = _query_process_path(int(process_id))
                    if process_path is None:
                        continue
                    if _normalize_windows_path(process_path) == expected_path:
                        process_ids.add(int(process_id))
            return ApplicationRuntimeSnapshot(
                window_handles=tuple(window_handles),
                process_ids=frozenset(process_ids),
            )

    def focus_window(self, window_handle: int) -> bool:
        """恢复并置前窗口，然后核实它是否成为真实前台窗口。

        Args:
            window_handle: 需要唤起的普通顶层窗口句柄。

        Returns:
            Windows 确认该窗口成为前台时返回真。
        """

        if not self.is_supported:
            return False
        import win32api
        import win32con
        import win32gui
        import win32process

        if not win32gui.IsWindow(window_handle):
            return False
        try:
            if win32gui.IsIconic(window_handle):
                win32gui.ShowWindow(window_handle, win32con.SW_RESTORE)
            else:
                win32gui.ShowWindow(window_handle, win32con.SW_SHOW)
            win32gui.BringWindowToTop(window_handle)
            win32gui.SetForegroundWindow(window_handle)
        except Exception:
            pass
        if int(win32gui.GetForegroundWindow()) == int(window_handle):
            return True

        # Windows 会阻止后台线程直接抢前台。把当前线程临时接入前台窗口和目标窗口的
        # 输入队列后再试一次，结束后立刻解除，既能尊重系统规则也不会长期串联输入状态。
        try:
            current_thread_id = int(win32api.GetCurrentThreadId())
            target_thread_id, _target_process_id = (
                win32process.GetWindowThreadProcessId(window_handle)
            )
            foreground_window = int(win32gui.GetForegroundWindow())
            foreground_thread_id = 0
            if foreground_window:
                foreground_thread_id, _foreground_process_id = (
                    win32process.GetWindowThreadProcessId(foreground_window)
                )
        except Exception:
            return False
        attached_thread_ids: list[int] = []
        try:
            for thread_id in (foreground_thread_id, target_thread_id):
                normalized_thread_id = int(thread_id)
                if (
                    normalized_thread_id <= 0
                    or normalized_thread_id == current_thread_id
                    or normalized_thread_id in attached_thread_ids
                ):
                    continue
                win32process.AttachThreadInput(
                    current_thread_id,
                    normalized_thread_id,
                    True,
                )
                attached_thread_ids.append(normalized_thread_id)
            if win32gui.IsIconic(window_handle):
                win32gui.ShowWindow(window_handle, win32con.SW_RESTORE)
            else:
                win32gui.ShowWindow(window_handle, win32con.SW_SHOW)
            win32gui.BringWindowToTop(window_handle)
            win32gui.SetForegroundWindow(window_handle)
        except Exception:
            return False
        finally:
            for thread_id in reversed(attached_thread_ids):
                try:
                    win32process.AttachThreadInput(
                        current_thread_id,
                        thread_id,
                        False,
                    )
                except Exception:
                    continue
        return int(win32gui.GetForegroundWindow()) == int(window_handle)

    def launch(self, application: InstalledApplication) -> bool:
        """把规范启动目标直接交给 Windows Shell。

        Args:
            application: 已唯一匹配的应用。

        Returns:
            Shell 接受启动请求时返回真。
        """

        if not self.is_supported:
            return False
        shell_execute = ctypes.windll.shell32.ShellExecuteW
        shell_execute.argtypes = [
            wintypes.HWND,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            ctypes.c_int,
        ]
        shell_execute.restype = wintypes.HINSTANCE
        result = shell_execute(
            None,
            "open",
            application.launch_target,
            None,
            None,
            1,
        )
        return int(result) > 32


@contextmanager
def _com_apartment() -> Iterator[None]:
    """在当前 Inline 工作线程内建立并释放 COM apartment。"""

    import pythoncom

    pythoncom.CoInitialize()
    try:
        yield
    finally:
        pythoncom.CoUninitialize()


def _application_from_shell_item(item: Any) -> InstalledApplication | None:
    """把单个 AppsFolder Shell 项转换成安全应用描述。"""

    display_name = str(getattr(item, "Name", "") or "").strip()
    item_path = str(getattr(item, "Path", "") or "").strip()
    target_path = str(
        _shell_property(item, "System.Link.TargetParsingPath") or ""
    ).strip()
    application_id = str(
        _shell_property(item, "System.AppUserModel.ID") or item_path
    ).strip()
    if not display_name or not item_path or not application_id:
        return None
    if _is_uri(item_path) or _is_uri(target_path):
        return None

    executable_path = _executable_path(target_path) or _executable_path(item_path)
    if _is_uninstaller(
        display_name=display_name,
        executable_path=executable_path,
    ):
        return None
    if executable_path is None and not _has_open_verb(item):
        return None

    launch_target = (
        executable_path
        if _is_windows_executable(item_path)
        else f"shell:AppsFolder\\{item_path}"
    )
    aliases = tuple(
        value
        for value in (
            item_path,
            application_id,
            Path(executable_path).stem if executable_path is not None else "",
        )
        if value
    )
    return InstalledApplication(
        display_name=display_name,
        application_id=application_id,
        launch_target=launch_target,
        executable_path=executable_path,
        aliases=aliases,
    )


def _shell_property(item: Any, property_name: str) -> Any:
    """读取可选 Shell 属性，单项损坏时返回空值。"""

    try:
        return item.ExtendedProperty(property_name)
    except Exception:
        return None


def _has_open_verb(item: Any) -> bool:
    """判断没有直接可执行路径的 Shell 项是否声明打开动作。"""

    try:
        verbs = item.Verbs()
        for index in range(int(verbs.Count)):
            verb = verbs.Item(index)
            name = str(getattr(verb, "Name", "") or "")
            normalized = name.replace("&", "").strip().casefold()
            normalized = _VERB_ACCELERATOR_SUFFIX.sub("", normalized).strip()
            if normalized in {"open", "打开", "启动"}:
                return True
    except Exception:
        return False
    return False


def _is_uri(value: str) -> bool:
    """识别 HTTP、协议和其它非文件启动目标。"""

    normalized = value.strip()
    if not normalized or _WINDOWS_DRIVE_PATH.match(normalized):
        return False
    return _URI_SCHEME.match(normalized) is not None


def _is_windows_executable(value: str) -> bool:
    """判断字符串是否是 Windows 可执行文件路径。"""

    return bool(_WINDOWS_DRIVE_PATH.match(value)) and Path(value).suffix.casefold() == ".exe"


def _executable_path(value: str) -> str | None:
    """从 Shell 属性中提取并规整真实可执行文件路径。"""

    expanded = os.path.expandvars(value.strip())
    if not _is_windows_executable(expanded):
        return None
    return os.path.normpath(expanded)


def _is_uninstaller(
    *,
    display_name: str,
    executable_path: str | None,
) -> bool:
    """过滤卸载入口，避免模糊匹配触发破坏性程序。"""

    normalized_display = display_name.strip().casefold()
    if any(marker in normalized_display for marker in _UNINSTALL_DISPLAY_PREFIXES):
        return True
    if executable_path is None:
        return False
    executable_stem = Path(executable_path).stem.casefold()
    return any(marker in executable_stem for marker in _UNINSTALL_EXECUTABLE_PREFIXES)


def _query_default_http_executable() -> str | None:
    """通过 Windows 文件关联 API 读取默认 HTTP 浏览器主程序。"""

    shlwapi = ctypes.windll.shlwapi
    assoc_query_string = shlwapi.AssocQueryStringW
    assoc_query_string.argtypes = [
        wintypes.DWORD,
        ctypes.c_int,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    assoc_query_string.restype = ctypes.c_long
    length = ctypes.c_ulong(32768)
    buffer = ctypes.create_unicode_buffer(length.value)
    # ASSOCSTR_EXECUTABLE=2，直接返回处理 http 协议的可执行文件，不导航到任何网页。
    result = assoc_query_string(
        0,
        2,
        "http",
        None,
        buffer,
        ctypes.byref(length),
    )
    if int(result) != 0:
        return None
    value = buffer.value.strip()
    return value or None


def _is_normal_top_level_window(window_handle: int) -> bool:
    """排除隐藏、被拥有、工具型和 DWM 隐藏窗口。"""

    import win32con
    import win32gui

    if not win32gui.IsWindowVisible(window_handle):
        return False
    if win32gui.GetWindow(window_handle, win32con.GW_OWNER):
        return False
    extended_style = win32gui.GetWindowLong(window_handle, win32con.GWL_EXSTYLE)
    if extended_style & win32con.WS_EX_TOOLWINDOW:
        return False
    return not _is_cloaked_window(window_handle)


def _is_cloaked_window(window_handle: int) -> bool:
    """读取 DWM cloaked 标记，失败时不误删普通窗口。"""

    cloaked = ctypes.c_int(0)
    try:
        get_window_attribute = ctypes.windll.dwmapi.DwmGetWindowAttribute
        get_window_attribute.argtypes = [
            wintypes.HWND,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
        ]
        get_window_attribute.restype = ctypes.c_long
        result = get_window_attribute(
            window_handle,
            14,
            ctypes.byref(cloaked),
            ctypes.sizeof(cloaked),
        )
    except Exception:
        return False
    return int(result) == 0 and bool(cloaked.value)


def _window_matches_application(
    *,
    window_handle: int,
    process_id: int,
    application: InstalledApplication,
) -> bool:
    """按主程序路径或 AppUserModelID 判断窗口归属。"""

    if application.executable_path is not None:
        process_path = _query_process_path(process_id)
        if process_path is not None and (
            _normalize_windows_path(process_path)
            == _normalize_windows_path(application.executable_path)
        ):
            return True
    window_application_id = _query_window_application_id(window_handle)
    return (
        window_application_id is not None
        and window_application_id.casefold() == application.application_id.casefold()
    )


def _query_window_application_id(window_handle: int) -> str | None:
    """读取窗口属性存储中的 AppUserModelID。"""

    try:
        from win32com.propsys import pscon, propsys

        property_store = propsys.SHGetPropertyStoreForWindow(window_handle)
        value = property_store.GetValue(pscon.PKEY_AppUserModel_ID)
        # pywin32 312 返回 PyPROPVARIANT 包装对象；直接 str() 只会得到对象地址，
        # 必须先取出真实字符串，才能和 AppsFolder 的 AppUserModelID 对上。
        if hasattr(value, "GetValue"):
            value = value.GetValue()
    except Exception:
        return None
    normalized = str(value).strip() if value is not None else ""
    return normalized or None


def _query_process_path(process_id: int) -> str | None:
    """使用最小查询权限读取进程主程序路径。"""

    if process_id <= 0:
        return None
    kernel32 = ctypes.windll.kernel32
    open_process = kernel32.OpenProcess
    open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    open_process.restype = wintypes.HANDLE
    query_process_image = kernel32.QueryFullProcessImageNameW
    query_process_image.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    query_process_image.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    process_handle = open_process(0x1000, False, int(process_id))
    if not process_handle:
        return None
    try:
        length = ctypes.c_ulong(32768)
        buffer = ctypes.create_unicode_buffer(length.value)
        succeeded = query_process_image(
            process_handle,
            0,
            buffer,
            ctypes.byref(length),
        )
        if not succeeded:
            return None
        value = buffer.value.strip()
        return value or None
    finally:
        close_handle(process_handle)


def _normalize_windows_path(value: str) -> str:
    """生成不受路径大小写和分隔符差异影响的比较值。"""

    return os.path.normcase(os.path.normpath(os.path.expandvars(value.strip())))
