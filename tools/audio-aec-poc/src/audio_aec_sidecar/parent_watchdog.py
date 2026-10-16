from __future__ import annotations

import ctypes
import os
from collections.abc import Callable
from ctypes import wintypes

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
ERROR_ACCESS_DENIED = 5
STILL_ACTIVE = 259


def windows_process_exists(pid: int) -> bool:
    """安全探测父进程是否仍然存在。

    功能：Windows 上使用 kernel32 查询 API，非 Windows fallback 使用 os.kill(pid, 0)，并将系统结果转换为布尔存活状态。
    核心入参：pid 是宿主进程编号，必须是正整数才有探测意义。
    预期输出：进程存在且仍为运行态时返回 True；权限不足按进程可能存在处理；pid 无效、进程不存在或已退出时返回 False。
    边界异常：函数吞掉平台探测异常，不向调用方暴露操作系统细节。
    """
    # 非正 pid 不是有效宿主进程编号，直接返回 False 可以避免触发平台相关的系统调用边界行为。
    if pid <= 0:
        return False

    # Windows 上 os.kill 可能对目标进程造成破坏性影响，因此必须使用只查询进程信息的系统 API。
    if os.name == "nt":
        return _windows_process_exists_with_kernel32(pid)

    # 非 Windows 平台允许使用 signal 0 做存在性探测，该分支保留 PermissionError 表示进程存在的语义。
    return _non_windows_process_exists(pid)


def _windows_process_exists_with_kernel32(pid: int) -> bool:
    """通过 kernel32 API 查询 Windows 进程是否存在且仍在运行。

    功能：使用 OpenProcess 获取有限查询句柄，再用 GetExitCodeProcess 判断进程退出码是否为 STILL_ACTIVE。
    核心入参：pid 是已经确认大于 0 的进程编号。
    预期输出：进程存在且退出码为 STILL_ACTIVE 时返回 True；权限不足也返回 True；其它失败返回 False。
    边界异常：不抛出 Windows API 错误，所有失败都折叠为布尔结果。
    """
    # use_last_error=True 让 ctypes.get_last_error 能读取 OpenProcess 失败时的 Windows 错误码。
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    # PROCESS_QUERY_LIMITED_INFORMATION 是最小查询权限，足够读取退出码且避免请求过宽访问能力。
    process_handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)

    # OpenProcess 返回空句柄表示失败，访问被拒绝时仍可推断 pid 大概率存在。
    if not process_handle:
        if ctypes.get_last_error() == ERROR_ACCESS_DENIED:
            return True
        return False

    try:
        # 退出码输出参数由 Windows API 写回，STILL_ACTIVE 表示目标进程仍未退出。
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(process_handle, ctypes.byref(exit_code)):
            return False
        return exit_code.value == STILL_ACTIVE
    finally:
        # 只要 OpenProcess 成功拿到句柄，就必须关闭，避免侧车长期运行时泄漏系统资源。
        kernel32.CloseHandle(process_handle)


def _non_windows_process_exists(pid: int) -> bool:
    """使用非 Windows 平台的 signal 0 约定探测进程是否存在。

    功能：对指定 pid 调用 os.kill(pid, 0)，并把权限或系统错误转换为存活状态。
    核心入参：pid 是已经确认大于 0 的进程编号。
    预期输出：进程存在或仅权限不足时返回 True，系统报告不存在时返回 False。
    边界异常：函数吞掉 PermissionError 与普通 OSError，不向调用方暴露平台异常细节。
    """
    try:
        # 非 Windows 平台上的 signal 0 只做存在性和权限检查，不发送实际业务信号。
        os.kill(pid, 0)
    except PermissionError:
        # 权限不足说明目标进程仍被系统识别，只是当前进程没有访问权限，因此按存活处理。
        return True
    except OSError:
        # 普通系统错误覆盖进程不存在等场景，watchdog 应据此认为父进程已退出。
        return False

    # 没有异常表示系统接受该 pid 探测，父进程仍然存在。
    return True


class ParentWatchdog:
    """父进程存活检测器。

    功能：保存宿主进程 pid，并通过可注入的 process_exists 函数判断父进程是否仍然存活。
    核心入参：parent_pid 是宿主进程编号，process_exists 是系统进程探测函数，默认使用 windows_process_exists。
    预期输出：is_parent_alive 返回当前父进程存活布尔值，供侧车运行循环决定是否继续工作。
    边界异常：本类不捕获注入探测函数抛出的异常，默认探测函数已将系统异常折叠为布尔值。
    """

    def __init__(
        self,
        parent_pid: int,
        process_exists: Callable[[int], bool] = windows_process_exists,
    ) -> None:
        """初始化父进程 watchdog。

        功能：保存父进程编号和探测函数，允许测试或未来平台适配替换探测实现。
        核心入参：parent_pid 是需要持续监控的宿主进程，process_exists 接收 pid 并返回是否存在。
        预期输出：生成可调用 is_parent_alive 的 watchdog 实例。
        边界异常：初始化不主动校验 parent_pid，配置层负责在启动前执行严格校验。
        """
        # 父进程编号在 watchdog 生命周期内保持不变，避免运行时循环误探测其他进程。
        self._parent_pid = parent_pid
        # 探测函数通过依赖注入进入类中，让单元测试无需触碰真实系统进程表。
        self._process_exists = process_exists

    def is_parent_alive(self) -> bool:
        """返回父进程当前是否仍然存活。

        功能：调用初始化时注入的进程探测函数，并把父进程编号传给它。
        核心入参：无显式入参，使用构造阶段保存的 parent_pid。
        预期输出：返回 process_exists(parent_pid) 的布尔结果。
        边界异常：若自定义 process_exists 抛出异常，异常会透传给调用方以保留测试和平台适配的可见性。
        """
        # 只做一次委托调用，保持 watchdog 职责聚焦在父进程状态判断而非运行时调度。
        return self._process_exists(self._parent_pid)
