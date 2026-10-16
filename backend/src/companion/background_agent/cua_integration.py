"""把官方 Cua Driver Embedded Host 接入 Background Agent。"""

from __future__ import annotations

import asyncio
import ctypes
import hashlib
import json
import os
import shutil
import subprocess
import threading
from collections.abc import Callable
from ctypes import wintypes
from pathlib import Path
from typing import Any

import yaml
from claude_agent_sdk.types import McpStdioServerConfig

from companion.observability.logging_config import get_backend_logger

from cua_driver import EmbeddedCuaDriverHost, EmbeddedDriverConnection, get_binary_path


CUA_SKILL_ID = "cua-driver"
CUA_MCP_SERVER_NAME = "cua_driver"
CUA_DRIVER_VERSION = "0.22.1"
CUA_BROWSER_PROFILE_NAME = "nearmate-default"
CUA_BROWSER_SOCKET_ENVIRONMENT_VARIABLE = "NEARMATE_CUA_BROWSER_SOCKET"
CUA_TOOL_PREFIX = f"mcp__{CUA_MCP_SERVER_NAME}__"
CUA_BROWSER_MCP_TOOL_NAMES = frozenset(
    f"{CUA_TOOL_PREFIX}{tool_name}"
    for tool_name in (
        "browser_prepare",
        "get_browser_state",
        "browser_click",
        "browser_dialog",
        "browser_download",
        "browser_navigate",
        "browser_pointer",
        "browser_set_input_files",
        "browser_type",
        "page",
    )
)
CUA_HOST_BUNDLE_ID = "com.nearmate.virtual-companion"
CUA_SESSION_CLEANUP_TIMEOUT_SECONDS = 2.0

logger = get_backend_logger(__name__)


class CuaIntegrationError(RuntimeError):
    """表示官方 Binary、Skill、桌面或 Embedded Host 不可用。"""


class CuaIntegrationCancelled(RuntimeError):
    """表示 Activity 在 CUA Host 启动前已经取消。"""


class CuaIntegration:
    """校验官方 CUA 安装并持有一个共享 Embedded Runtime。

    Args:
        executable: 用于版本和 doctor 检查的官方 Binary 名称或路径。
        skill_root: NearMate 受管官方 `cua-driver` Skill 根目录。
        host_bundle_id: 传给官方 Host 的稳定宿主身份。
        host_factory: 测试替身或平台适配器使用的 Host 构造器。
    """

    def __init__(
        self,
        *,
        executable: str = "cua-driver",
        skill_root: Path,
        host_bundle_id: str = CUA_HOST_BUNDLE_ID,
        host_factory: Callable[[str, str], Any] | None = None,
    ) -> None:
        """保存校验入口和受管 Skill，尚不启动 Runtime。"""

        normalized_executable = executable.strip()
        if not normalized_executable:
            raise ValueError("cua-driver executable 不能为空")
        normalized_bundle_id = host_bundle_id.strip()
        if not normalized_bundle_id:
            raise ValueError("CUA Host bundle ID 不能为空")
        self._executable = normalized_executable
        self._skill_root = skill_root.absolute()
        self._host_bundle_id = normalized_bundle_id
        self._host_factory = host_factory or EmbeddedCuaDriverHost
        self._lock = threading.RLock()
        self._host: Any | None = None
        self._connection: EmbeddedDriverConnection | None = None
        self._validated = False

    @property
    def mcp_server_name(self) -> str:
        """返回 Claude Agent SDK 中稳定的官方 MCP Server 名称。"""

        return CUA_MCP_SERVER_NAME

    @property
    def connection(self) -> EmbeddedDriverConnection:
        """返回已经启动的 Embedded Runtime 连接。

        Raises:
            CuaIntegrationError: Host 尚未成功启动。
        """

        with self._lock:
            if self._connection is None:
                raise CuaIntegrationError("CUA Embedded Host 尚未启动")
            return self._connection

    @property
    def browser_socket_path(self) -> str:
        """返回 MCP 与 CLI 共用的同一私有 socket。"""

        return self.connection.socket_path

    def browser_cli_session_name(self, activity_id: str) -> str:
        """根据 Activity ID 生成稳定且不暴露原始身份的 Browser CLI Session。

        Args:
            activity_id: 当前后台任务的稳定 Activity ID。

        Returns:
            仅包含 ASCII 字符的短 CLI Session 名称。

        Raises:
            ValueError: Activity ID 为空。
        """

        normalized_activity_id = activity_id.strip()
        if not normalized_activity_id:
            raise ValueError("Browser session 的 Activity ID 不能为空")
        digest = hashlib.sha256(normalized_activity_id.encode("utf-8")).hexdigest()[:24]
        return f"nm-browser-{digest}"

    async def start(self, *, cancel_event: threading.Event | None = None) -> EmbeddedDriverConnection:
        """校验运行条件并启动官方 Embedded Host。

        Args:
            cancel_event: 可选的 Activity 取消事件。

        Returns:
            MCP 和 CLI 共用的同一连接对象。

        Raises:
            CuaIntegrationCancelled: 启动前 Activity 已取消。
            CuaIntegrationError: 校验失败或官方 Host 启动失败。
        """

        self._raise_if_cancelled(cancel_event)
        with self._lock:
            connection = self._connection
            host = self._host
            if host is None:
                binary_path = self._resolve_binary_path()
                self._validate_integrity_level()
                if not self._validated:
                    self._validate_installation(binary_path)
                    self._validated = True
                host = self._host_factory(str(binary_path), self._host_bundle_id)
                self._host = host
        self._raise_if_cancelled(cancel_event)
        if connection is None:
            try:
                connection = await host.start()
            except asyncio.CancelledError:
                raise
            except Exception as exception:
                with self._lock:
                    if self._connection is None:
                        self._host = None
                raise CuaIntegrationError("CUA Embedded Host 启动失败") from exception
            self._validate_connection(connection)
            with self._lock:
                if self._connection is None:
                    self._connection = connection
                connection = self._connection
        self._raise_if_cancelled(cancel_event)
        return connection

    def start_blocking(self) -> EmbeddedDriverConnection:
        """在 Activity 启动线性化点同步启动官方 Host。"""

        return asyncio.run(self.start())

    def mcp_server_config(self) -> McpStdioServerConfig:
        """只读取已准备好的 Connection MCP 配置，不执行探活或同步启动。

        Returns:
            Claude Agent SDK 可直接使用的 stdio Server 配置。

        Raises:
            CuaIntegrationError: Embedded Host 尚未准备连接。
        """

        mcp = self.connection.mcp
        return {
            "type": "stdio",
            "command": mcp.command,
            "args": list(mcp.args),
            "env": {item.name: item.value for item in mcp.environment},
        }

    def transport_instructions(self, activity_id: str, *, browser_guide_path: Path) -> str:
        """生成当前 Activity 的网页意图与 MCP/CLI 使用声明。

        Args:
            activity_id: 当前后台任务的稳定 Activity ID。
            browser_guide_path: 私有配置中官方 BROWSER.md 的只读路径。

        Returns:
            追加到产品 system prompt 的中文传输约束。
        """

        browser_session = self.browser_cli_session_name(activity_id)
        guide_path = browser_guide_path.absolute()
        return f"""当前 Activity 已连接同一个官方 Cua Driver Embedded Runtime。原生桌面操作和用户可见网页启动使用官方 Computer MCP 工具；Computer MCP Session 由 SDK transport 管理。页面自动化使用官方 Browser CLI，不调用 Browser MCP 工具。
按用户想得到的结果选择工具，不要用关键词或固定步骤替代语义判断：
1. 用户可见网页启动：用户只要求“打开网站”“打开这个链接给我看”时，调用 `{CUA_TOOL_PREFIX}launch_app` 打开日常默认浏览器，参数只传 `urls`。单纯打开 URL 时不得调用 `browser_prepare`，也不要在 Activity 结束时关闭这个作为交付结果的窗口；它应在 Activity 完成后保持打开。
   只有当完整任务已经没有其他未完成目标，剩余目标仅仅是把网页交给用户的默认浏览器时：确定目标 URL 后调用一次 `{CUA_TOOL_PREFIX}launch_app`；ToolResult 没有错误或 refusal 就表示 URL 已交给默认浏览器，立即提交 StructuredOutput。此规则是最终 URL 交付的窄例外，优先于动作后重新观察的一般规则：禁止继续调用 `list_windows`、`get_window_state`，禁止检查窗口是否最小化、是否 active 或页面是否完全加载。即使返回 `{{"running": true, "active": false, "windows": []}}` 也必须结束。准确结果是“目标网页已交给系统默认浏览器打开”，不能声称已经确认页面加载完成。
   `{CUA_TOOL_PREFIX}launch_app` 返回错误或 refusal 时如实报告；不得改用 Shell 或其它浏览器启动方式，不得回退到 Browser CLI，也不得因为验证欲望重复调用 `{CUA_TOOL_PREFIX}launch_app`。
2. 页面自动化：用户要求读取、搜索、点击、填写或下载网页内容时，从 `browser_prepare` 开始全程使用 Browser CLI 和隔离 Profile，不得改用日常默认浏览器。窗口发现、绑定、导航、snapshot、动作、上传和下载都必须连接 `--socket "$NEARMATE_CUA_BROWSER_SOCKET"`，并在所有支持 `session` 的调用中重复传入 Browser CLI Session `{browser_session}`。
3. 混合交付：用户要求先查找或操作页面，最后明确要求“打开给我看”时，先用 Browser CLI 查找或操作；完成后只能把 fresh snapshot 中确认的最终 URL 交给 `{CUA_TOOL_PREFIX}launch_app`，且参数只传 `urls`。仍有读取、搜索、点击、填写或下载目标时不得提前结束；只有最终 URL 交付后没有其它目标，才应用第 1 条的立即收口规则。没有“打开给我看”这项明确要求时，不向日常默认浏览器交付页面。
4. 意图不明确：无法可靠判断用户要看一个保留的网页窗口，还是要你操作网页时，必须在任何 CUA 调用前先使用 `AskUserQuestion` 澄清。
5. 网站识别：高置信度的常见网站直接使用已经确认的官方 URL。陌生网站只允许一次有界搜索或解析，唯一确认官网后才能打开；存在多个候选时使用 `AskUserQuestion` 询问用户，不得猜测域名。
不要依赖上一次调用隐式继承 Browser CLI Session，也不要自行调用 `end_session`；Runner 会在 Activity 收口时结束该 CLI Session。默认浏览器不属于 Browser CLI Session，因此不会随它结束。
MCP 与 CLI 只共享 Embedded Runtime 和 socket，transport-local Session 相互独立。不能在 MCP 与 CLI 之间传递或复用 Session、target、tab、ref 或其它状态，也不能在日常默认浏览器与 `{CUA_BROWSER_PROFILE_NAME}` 之间传递 Cookie、Session、tab、ref 或登录态。Browser 自动化使用 `isolated_named` Profile `{CUA_BROWSER_PROFILE_NAME}`；不得使用 `existing_profile`，也不要要求用户预先打开浏览器。需要参数细节时只 Read 官方指南 `{guide_path}`，然后直接执行官方 workflow。
Browser CLI 的上传和下载可直接使用官方工具。需要交付的下载文件应写入任务说明给出的当前 Activity 正式输出目录，NearMate 不解析 CLI JSON 或强制限制 Browser 文件路径。
Binary、版本、官方 Skill、Windows Integrity 和 doctor 已由 NearMate 在启动前验证；不要运行 where/which、--help、--version 或 doctor 探索，也不要枚举 Skill 目录。普通文件、Bash 和其它工具仍可正常使用。"""

    def end_browser_session(self, activity_id: str) -> None:
        """用同一 Embedded Runtime 的 socket 结束 Activity Browser Session。

        Args:
            activity_id: 需要收口的 Activity ID。

        Raises:
            CuaIntegrationError: CLI 无法执行或返回失败状态。
        """

        self._run_cli(
            "call",
            "end_session",
            "--socket",
            self.browser_socket_path,
            stdin_payload={"session": self.browser_cli_session_name(activity_id)},
            timeout=CUA_SESSION_CLEANUP_TIMEOUT_SECONDS,
        )

    def shutdown(self, timeout: float = 1.0) -> None:
        """按关闭预算同步桥接官方 Host.stop，供 Assistant Runtime 使用。

        Args:
            timeout: 允许等待官方 Host.stop 完成的最长秒数。
        """

        normalized_timeout = max(0.0, timeout)
        with self._lock:
            host = self._host
            self._host = None
            self._connection = None
        if host is None:
            return
        failure: list[BaseException] = []

        def stop_host() -> None:
            """在临时线程中运行官方异步 stop。"""

            try:
                asyncio.run(host.stop())
            except BaseException as thread_exception:  # noqa: BLE001
                failure.append(thread_exception)

        thread = threading.Thread(target=stop_host, name="cua-host-stop", daemon=True)
        thread.start()
        thread.join(normalized_timeout)
        if thread.is_alive():
            # 官方 Host 自己负责父进程存活回收；NearMate 不能为了等一个失去响应的
            # stop 协程而卡住整个 Assistant 关闭流程。
            logger.warning(
                "官方 CUA Host.stop 超过关闭预算，交由 Host 继续后台回收"
            )
            return
        if failure:
            raise failure[0]

    def _resolve_binary_path(self) -> Path:
        """解析官方 Binary 的绝对路径，不负责安装或升级。"""

        if self._executable == "cua-driver":
            return Path(get_binary_path()).absolute()
        candidate = Path(self._executable)
        if candidate.is_file():
            return candidate.absolute()
        resolved = shutil.which(self._executable)
        if resolved is None:
            raise CuaIntegrationError("cua-driver Binary 不在 PATH 中")
        return Path(resolved).absolute()

    def _validate_installation(self, binary_path: Path) -> None:
        """检查官方 manifest、版本、Skill 和交互桌面。"""

        manifest = self._read_json_command(
            binary_path,
            "manifest",
            description="manifest",
        )
        self._validate_manifest(manifest, binary_path)
        version = self._run_command(
            binary_path,
            "--version",
            description="版本检查",
        ).stdout.strip()
        if version != f"cua-driver {CUA_DRIVER_VERSION}":
            raise CuaIntegrationError(f"cua-driver Binary 必须精确匹配官方版本 {CUA_DRIVER_VERSION}")
        self._validate_skill_version()
        self._validate_doctor(self._read_json_command(binary_path, "doctor", "--json", description="doctor"))

    def _validate_manifest(self, payload: dict[str, Any], binary_path: Path) -> None:
        """确认 manifest 指向同一个固定版本官方 Binary。"""

        if payload.get("schema_version") != "1" or payload.get("binary_version") != CUA_DRIVER_VERSION:
            raise CuaIntegrationError(f"CUA manifest 必须精确匹配官方版本 {CUA_DRIVER_VERSION}")
        declared_path = payload.get("binary_path")
        if not isinstance(declared_path, str) or Path(declared_path).resolve() != binary_path.resolve():
            raise CuaIntegrationError("CUA manifest 的 Binary 路径无效")

    def _validate_skill_version(self) -> None:
        """确认受管 Skill 的官方文件和版本完整。"""

        instruction_path = self._skill_root / "SKILL.md"
        if not instruction_path.is_file() or any(
            not (self._skill_root / name).is_file()
            for name in ("WINDOWS.md", "BROWSER.md")
        ):
            raise CuaIntegrationError("CUA 官方 Skill 缺少 SKILL.md、WINDOWS.md 或 BROWSER.md")
        try:
            lines = instruction_path.read_text(encoding="utf-8").splitlines()
            metadata = yaml.safe_load("\n".join(lines[1 : lines.index("---", 1)]))
        except (OSError, UnicodeError, ValueError, yaml.YAMLError) as exception:
            raise CuaIntegrationError("CUA 官方 Skill frontmatter 无法读取") from exception
        if not isinstance(metadata, dict) or metadata.get("name") != CUA_SKILL_ID:
            raise CuaIntegrationError("CUA 官方 Skill 身份无效")
        if metadata.get("version") != CUA_DRIVER_VERSION:
            raise CuaIntegrationError(f"CUA 官方 Skill 必须精确匹配版本 {CUA_DRIVER_VERSION}")

    def _validate_doctor(self, payload: dict[str, Any]) -> None:
        """要求 doctor 的交互桌面、UIA 和可见窗口探针全部健康。"""

        probes = payload.get("probes")
        if payload.get("ok") is not True or not isinstance(probes, list):
            raise CuaIntegrationError("CUA 交互桌面预检失败")
        by_label = {
            str(item.get("label", "")).casefold(): item
            for item in probes
            if isinstance(item, dict)
        }
        required = (by_label.get("interactive session"), by_label.get("ui automation"), by_label.get("enumwindows visible"))
        if any(not isinstance(item, dict) or item.get("status") != "ok" for item in required):
            raise CuaIntegrationError("CUA 交互桌面、UI Automation 或窗口枚举不可用")
        message = str(required[2].get("message", ""))
        digits = "".join(character for character in message if character.isdigit())
        if not digits or int(digits) <= 0:
            raise CuaIntegrationError("CUA 交互桌面没有可见窗口")

    def _read_json_command(self, binary_path: Path, *args: str, description: str) -> dict[str, Any]:
        """运行一条只读官方命令并解析对象 JSON。"""

        completed = self._run_command(binary_path, *args, description=description)
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exception:
            raise CuaIntegrationError(f"CUA {description} 没有返回合法 JSON") from exception
        if not isinstance(payload, dict):
            raise CuaIntegrationError(f"CUA {description} JSON 根节点不是对象")
        return payload

    def _run_command(
        self,
        binary_path: Path,
        *args: str,
        description: str,
        stdin_payload: dict[str, Any] | None = None,
        timeout: float = 30.0,
    ) -> subprocess.CompletedProcess[str]:
        """以无 Shell 方式运行官方短命令。

        Args:
            binary_path: 已校验的官方 Binary 路径。
            *args: 直接传给 Binary 的参数。
            description: 错误诊断中使用的命令用途。
            stdin_payload: 可选的结构化标准输入对象。
            timeout: 当前命令允许占用的最长秒数。

        Returns:
            已成功执行的子进程结果。

        Raises:
            CuaIntegrationError: 命令无法执行、超时或返回非零状态。
        """

        command = [str(binary_path), *args]
        try:
            completed = subprocess.run(
                command,
                input=(json.dumps(stdin_payload, separators=(",", ":")) if stdin_payload is not None else None),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=max(0.0, timeout),
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.TimeoutExpired) as exception:
            raise CuaIntegrationError(f"CUA {description} 无法执行") from exception
        if completed.returncode != 0:
            raise CuaIntegrationError(f"CUA {description} 执行失败")
        return completed

    def _run_cli(
        self,
        *args: str,
        stdin_payload: dict[str, Any],
        timeout: float,
    ) -> None:
        """通过 Connection socket 执行一个官方 CLI 工具。

        Args:
            *args: 直接传给官方 CLI 的工具参数。
            stdin_payload: 通过标准输入传入的结构化参数。
            timeout: CLI 清理调用的最长秒数。

        Returns:
            无返回值。
        """

        self._run_command(
            Path(self._resolve_binary_path()),
            *args,
            description="Browser session 清理",
            stdin_payload=stdin_payload,
            timeout=timeout,
        )

    def _validate_connection(self, connection: EmbeddedDriverConnection) -> None:
        """确认 Host 返回的连接确实属于固定官方版本。"""

        if not connection.socket_path or not connection.mcp.command or connection.driver_version != CUA_DRIVER_VERSION:
            raise CuaIntegrationError("CUA Embedded Host 返回了无效连接")

    @staticmethod
    def _validate_integrity_level() -> None:
        """Windows High/System Integrity 不启动 CUA，避免浏览器自动发现失效。"""

        if os.name != "nt":
            return
        level = _windows_integrity_level()
        if level is not None and level >= 0x3000:
            raise CuaIntegrationError("当前 Windows Integrity Level 不允许使用 CUA")

    @staticmethod
    def _raise_if_cancelled(cancel_event: threading.Event | None) -> None:
        """在启动前及时响应所属 Activity 的取消。"""

        if cancel_event is not None and cancel_event.is_set():
            raise CuaIntegrationCancelled("CUA Host 启动已取消")


def _windows_integrity_level() -> int | None:
    """读取当前进程 Token 的 Mandatory Integrity Level，失败时返回空。"""

    if os.name != "nt":
        return None
    try:
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.argtypes = []
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        advapi32.OpenProcessToken.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.HANDLE),
        ]
        advapi32.OpenProcessToken.restype = wintypes.BOOL
        advapi32.GetTokenInformation.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        advapi32.GetTokenInformation.restype = wintypes.BOOL
        advapi32.GetSidSubAuthorityCount.argtypes = [wintypes.LPVOID]
        advapi32.GetSidSubAuthorityCount.restype = ctypes.POINTER(ctypes.c_ubyte)
        advapi32.GetSidSubAuthority.argtypes = [wintypes.LPVOID, wintypes.DWORD]
        advapi32.GetSidSubAuthority.restype = ctypes.POINTER(wintypes.DWORD)
        token = wintypes.HANDLE()
        if not advapi32.OpenProcessToken(
            kernel32.GetCurrentProcess(),
            0x0008,
            ctypes.byref(token),
        ):
            return None
        try:
            size = wintypes.DWORD()
            advapi32.GetTokenInformation(token, 25, None, 0, ctypes.byref(size))
            if size.value <= 0:
                return None
            buffer = ctypes.create_string_buffer(size.value)
            if not advapi32.GetTokenInformation(
                token,
                25,
                buffer,
                size.value,
                ctypes.byref(size),
            ):
                return None
            sid_pointer = ctypes.cast(
                buffer,
                ctypes.POINTER(wintypes.LPVOID),
            ).contents.value
            if not sid_pointer:
                return None
            count_pointer = advapi32.GetSidSubAuthorityCount(sid_pointer)
            if not count_pointer:
                return None
            sub_authority_count = count_pointer.contents.value
            if sub_authority_count <= 0:
                return None
            authority_pointer = advapi32.GetSidSubAuthority(
                sid_pointer,
                sub_authority_count - 1,
            )
            if not authority_pointer:
                return None
            return authority_pointer.contents.value
        finally:
            kernel32.CloseHandle(token)
    except (AttributeError, OSError, ValueError):
        return None
