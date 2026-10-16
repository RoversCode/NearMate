from __future__ import annotations

import os

from audio_aec_sidecar import parent_watchdog
from audio_aec_sidecar.parent_watchdog import ParentWatchdog, windows_process_exists


class FakeKernel32:
    """模拟 Windows kernel32 进程查询 API。

    功能：提供 OpenProcess、GetExitCodeProcess、CloseHandle 三个方法，供 Windows 分支测试替换真实 ctypes 调用。
    核心入参：handle 是 OpenProcess 返回的伪句柄，exit_code 是 GetExitCodeProcess 写回的进程退出码。
    预期输出：记录 API 调用顺序，并按配置返回成功或失败。
    边界异常：本替身不抛出系统异常，Windows API 失败通过返回 0 表达。
    """

    def __init__(self, *, handle: int, exit_code: int, get_exit_code_success: bool = True) -> None:
        """初始化可控的 kernel32 测试替身。

        功能：保存伪句柄、退出码和 GetExitCodeProcess 成功标志。
        核心入参：handle 控制 OpenProcess 是否成功，exit_code 控制进程是否仍为 STILL_ACTIVE。
        预期输出：实例方法可被被测函数调用，并在 calls 中留下调用记录。
        边界异常：参数类型由测试固定提供，不模拟 ctypes 类型转换错误。
        """
        # 伪句柄为 0 表示 OpenProcess 失败，非 0 表示拿到可查询句柄。
        self.handle = handle
        # Windows STILL_ACTIVE 退出码表示进程仍在运行，其它退出码表示进程已退出。
        self.exit_code = exit_code
        # 该标志用于覆盖 GetExitCodeProcess 返回失败时被测函数的保守处理。
        self.get_exit_code_success = get_exit_code_success
        # 调用日志用于验证 API 顺序和 CloseHandle 清理行为。
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def OpenProcess(self, access: int, inherit_handle: bool, pid: int) -> int:
        """模拟 OpenProcess 返回进程句柄。

        功能：记录访问权限、继承标记和 pid，并返回预设伪句柄。
        核心入参：access 应为 PROCESS_QUERY_LIMITED_INFORMATION，inherit_handle 应为 False。
        预期输出：返回 self.handle。
        边界异常：不抛异常，失败通过返回 0 表示。
        """
        # 记录参数可以证明被测函数使用有限查询权限，减少对目标进程的侵入性。
        self.calls.append(("OpenProcess", (access, inherit_handle, pid)))
        return self.handle

    def GetExitCodeProcess(self, handle: int, exit_code_pointer) -> int:
        """模拟 GetExitCodeProcess 写回退出码。

        功能：记录句柄，将预设退出码写入 ctypes byref 指针。
        核心入参：handle 是 OpenProcess 返回的伪句柄，exit_code_pointer 是 DWORD 输出参数。
        预期输出：成功时返回 1，失败时返回 0。
        边界异常：不模拟指针错误，测试只覆盖被测函数的返回语义。
        """
        # 写回退出码让被测函数按真实 Windows API 方式判断进程是否仍然运行。
        self.calls.append(("GetExitCodeProcess", (handle,)))
        exit_code_pointer._obj.value = self.exit_code
        return 1 if self.get_exit_code_success else 0

    def CloseHandle(self, handle: int) -> int:
        """模拟 CloseHandle 清理进程句柄。

        功能：记录待关闭句柄，返回成功。
        核心入参：handle 是 OpenProcess 返回的伪句柄。
        预期输出：返回 1 表示关闭成功。
        边界异常：不模拟关闭失败，当前测试只关心被测函数确实执行清理。
        """
        # 即使只是测试替身，也保留关闭调用断言，防止真实实现泄漏系统句柄。
        self.calls.append(("CloseHandle", (handle,)))
        return 1


def test_parent_watchdog_delegates_liveness_check_to_injected_probe() -> None:
    """验证 ParentWatchdog 使用注入的探测函数检查父进程存活。"""
    # 调用日志记录被测类传入的 pid，证明 watchdog 没有自行改写父进程编号。
    observed_pids: list[int] = []

    def process_exists(pid: int) -> bool:
        """记录 pid 并返回固定存活结果，隔离真实操作系统进程表。

        功能：作为 ParentWatchdog 的测试探测函数，捕获调用参数并返回 True。
        核心入参：pid 是待检测的父进程编号。
        预期输出：返回 True，表示测试中的父进程仍然存活。
        边界异常：本替身不模拟异常，异常分支由 windows_process_exists 的独立测试覆盖。
        """
        # 保存 pid 便于断言 watchdog 委托检查时使用了初始化传入的父进程编号。
        observed_pids.append(pid)
        return True

    watchdog = ParentWatchdog(parent_pid=4321, process_exists=process_exists)

    # is_parent_alive 是运行时循环唯一需要调用的公开接口，应直接返回探测函数结果。
    assert watchdog.is_parent_alive() is True
    assert observed_pids == [4321]


def test_windows_process_exists_returns_false_for_non_positive_pid() -> None:
    """验证非正 pid 直接返回 False，不触碰 os.kill。"""
    # 非正 pid 不是可用的父进程编号，提前返回可以避免平台相关的 os.kill 行为差异。
    assert windows_process_exists(0) is False
    assert windows_process_exists(-1) is False


def test_windows_process_exists_uses_windows_api_without_calling_os_kill(monkeypatch) -> None:
    """验证 Windows 分支使用 kernel32 API，且不会调用 os.kill。"""
    # Windows 上 os.kill 的非 CTRL 信号存在终止目标进程风险，因此必须证明该路径完全不触碰 os.kill。
    fake_kernel32 = FakeKernel32(handle=99, exit_code=parent_watchdog.STILL_ACTIVE)

    def fake_windll(name: str, use_last_error: bool):
        """返回 fake kernel32，模拟 ctypes.WinDLL 加载系统库。

        功能：拦截 WinDLL 调用，确保测试不访问真实操作系统 API。
        核心入参：name 是库名，use_last_error 表示是否启用 ctypes last error。
        预期输出：返回 FakeKernel32 实例。
        边界异常：若被测函数加载非 kernel32 库，断言会让测试失败。
        """
        # 只允许加载 kernel32，确保测试覆盖的正是 Windows 进程查询路径。
        assert name == "kernel32"
        assert use_last_error is True
        return fake_kernel32

    def fail_if_os_kill_is_called(pid: int, signal_number: int) -> None:
        """在 Windows 分支误调用 os.kill 时让测试失败。

        功能：作为 os.kill 的陷阱替身，暴露危险调用。
        核心入参：pid 和 signal_number 只用于匹配 os.kill 签名。
        预期输出：无正常返回。
        边界异常：任何调用都会抛出 AssertionError。
        """
        # Windows 安全修复的核心要求就是不调用 os.kill，因此这里直接失败。
        raise AssertionError("os.kill must not be called on Windows process probe.")

    monkeypatch.setattr(parent_watchdog.os, "name", "nt")
    monkeypatch.setattr(parent_watchdog.ctypes, "WinDLL", fake_windll, raising=False)
    monkeypatch.setattr(os, "kill", fail_if_os_kill_is_called)

    assert windows_process_exists(55) is True
    assert fake_kernel32.calls == [
        ("OpenProcess", (parent_watchdog.PROCESS_QUERY_LIMITED_INFORMATION, False, 55)),
        ("GetExitCodeProcess", (99,)),
        ("CloseHandle", (99,)),
    ]


def test_windows_process_exists_returns_true_when_windows_open_process_is_access_denied(monkeypatch) -> None:
    """验证 Windows OpenProcess 权限不足时按进程存在处理。"""
    # 权限不足通常意味着 pid 存在但当前进程不能查询，watchdog 不应因此误杀侧车。
    fake_kernel32 = FakeKernel32(handle=0, exit_code=0)

    def fake_windll(name: str, use_last_error: bool):
        """返回 OpenProcess 失败的 fake kernel32。

        功能：模拟 Windows API 加载，并让 OpenProcess 返回 0。
        核心入参：name 是库名，use_last_error 表示是否启用 ctypes last error。
        预期输出：返回 FakeKernel32 实例。
        边界异常：不模拟库加载失败。
        """
        # 这里复用同一个 fake kernel32，使测试可以检查只调用了 OpenProcess。
        return fake_kernel32

    monkeypatch.setattr(parent_watchdog.os, "name", "nt")
    monkeypatch.setattr(parent_watchdog.ctypes, "WinDLL", fake_windll, raising=False)
    monkeypatch.setattr(parent_watchdog.ctypes, "get_last_error", lambda: parent_watchdog.ERROR_ACCESS_DENIED)

    assert windows_process_exists(56) is True
    assert fake_kernel32.calls == [
        ("OpenProcess", (parent_watchdog.PROCESS_QUERY_LIMITED_INFORMATION, False, 56)),
    ]


def test_windows_process_exists_returns_false_when_windows_exit_code_is_not_active(monkeypatch) -> None:
    """验证 Windows 查询到非 STILL_ACTIVE 退出码时返回 False。"""
    # OpenProcess 成功但退出码不是 STILL_ACTIVE，表示该 pid 对应进程已经不在运行态。
    fake_kernel32 = FakeKernel32(handle=100, exit_code=0)

    monkeypatch.setattr(parent_watchdog.os, "name", "nt")
    monkeypatch.setattr(parent_watchdog.ctypes, "WinDLL", lambda name, use_last_error: fake_kernel32, raising=False)

    assert windows_process_exists(57) is False
    assert fake_kernel32.calls[-1] == ("CloseHandle", (100,))


def test_windows_process_exists_returns_true_when_non_windows_os_kill_succeeds(monkeypatch) -> None:
    """验证非 Windows fallback 中 os.kill(pid, 0) 成功时认为进程存在。"""
    # 调用日志用于确认非 Windows fallback 使用 signal 0 存活检测约定。
    kill_calls: list[tuple[int, int]] = []

    def fake_kill(pid: int, signal_number: int) -> None:
        """模拟 os.kill 成功返回。

        功能：记录 pid 和 signal_number，不抛出异常。
        核心入参：pid 是待检测进程，signal_number 应为 0。
        预期输出：无返回值，表示操作系统接受该探测。
        边界异常：本替身不抛异常，专门覆盖成功路径。
        """
        # signal 0 表示只检测进程权限和存在性，不发送真实终止信号。
        kill_calls.append((pid, signal_number))

    monkeypatch.setattr(parent_watchdog.os, "name", "posix")
    monkeypatch.setattr(os, "kill", fake_kill)

    assert windows_process_exists(55) is True
    assert kill_calls == [(55, 0)]


def test_windows_process_exists_returns_true_for_non_windows_permission_error(monkeypatch) -> None:
    """验证非 Windows fallback 的 PermissionError 表示进程存在但当前进程无权访问。"""

    def fake_kill(pid: int, signal_number: int) -> None:
        """模拟目标进程存在但缺少访问权限。

        功能：抛出 PermissionError，复现 os.kill 探测受权限限制的情况。
        核心入参：pid 和 signal_number 只用于匹配 os.kill 签名。
        预期输出：无正常返回，调用方应把该异常解释为进程仍存在。
        边界异常：PermissionError 是 OSError 子类，测试确保被测函数先处理权限语义。
        """
        # 权限不足时不能说明进程不存在，因此 watchdog 应继续认为父进程存活。
        raise PermissionError("access denied")

    monkeypatch.setattr(parent_watchdog.os, "name", "posix")
    monkeypatch.setattr(os, "kill", fake_kill)

    assert windows_process_exists(56) is True


def test_windows_process_exists_returns_false_for_non_windows_os_error(monkeypatch) -> None:
    """验证非 Windows fallback 的普通 OSError 表示进程不存在或无法被系统定位。"""

    def fake_kill(pid: int, signal_number: int) -> None:
        """模拟操作系统无法找到目标进程。

        功能：抛出 OSError，复现 pid 不存在等探测失败场景。
        核心入参：pid 和 signal_number 只用于匹配 os.kill 签名。
        预期输出：无正常返回，调用方应返回 False。
        边界异常：本替身只覆盖普通 OSError，不覆盖权限异常。
        """
        # 普通系统错误按父进程已经退出处理，让侧车可以及时收尾。
        raise OSError("not found")

    monkeypatch.setattr(parent_watchdog.os, "name", "posix")
    monkeypatch.setattr(os, "kill", fake_kill)

    assert windows_process_exists(57) is False
