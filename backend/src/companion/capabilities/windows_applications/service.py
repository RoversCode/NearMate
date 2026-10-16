"""实现与 Windows API 解耦的应用匹配和同步启动业务。"""

from __future__ import annotations

import logging
import math
import re
import threading
import time
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Protocol

from pypinyin import lazy_pinyin

from companion.capabilities.windows_applications.models import (
    ApplicationLaunchResult,
    ApplicationRuntimeSnapshot,
    InstalledApplication,
    LaunchMode,
)
from companion.observability.logging_config import get_backend_logger
from companion.observability.structured_logging import log_event


logger = get_backend_logger(__name__)

DEFAULT_BROWSER_NAMES = frozenset(
    {
        "浏览器",
        "默认浏览器",
        "browser",
        "defaultbrowser",
    }
)
_FORBIDDEN_TARGET_PATTERN = re.compile(r"(?:^[a-zA-Z][a-zA-Z0-9+.-]*:|[\\/])")
_CATALOG_WAIT_INTERVAL_SECONDS = 0.05


@dataclass
class _CatalogRefresh:
    """保存一次目录扫描的完成信号和纯数据结果，供多个调用共同等待。"""

    completed: threading.Event = field(default_factory=threading.Event)
    applications: tuple[InstalledApplication, ...] | None = None


class WindowsApplicationGatewayPort(Protocol):
    """声明业务服务需要的最小 Windows 能力端口。"""

    @property
    def is_supported(self) -> bool:
        """返回当前运行平台是否支持真实 Windows 应用启动。"""

    def list_installed_applications(self) -> tuple[InstalledApplication, ...]:
        """返回当前 Windows AppsFolder 中可安全启动的应用。"""

    def resolve_default_browser(
        self,
        applications: Sequence[InstalledApplication],
    ) -> InstalledApplication | None:
        """把 Windows 默认 HTTP 浏览器解析成应用描述。"""

    def snapshot(self, application: InstalledApplication) -> ApplicationRuntimeSnapshot:
        """返回应用当前普通窗口和进程快照。"""

    def focus_window(self, window_handle: int) -> bool:
        """恢复并置前指定普通窗口，随后验证真实前台身份。"""

    def launch(self, application: InstalledApplication) -> bool:
        """通过 Windows Shell 提交应用的注册启动目标。"""


class WindowsApplicationLaunchService:
    """按用户意图匹配已安装应用并同步执行启动动作。"""

    def __init__(
        self,
        *,
        gateway: WindowsApplicationGatewayPort,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        total_budget_seconds: float = 3.8,
        new_instance_verification_seconds: float = 3.0,
        poll_interval_seconds: float = 0.1,
    ) -> None:
        """初始化应用启动服务。

        Args:
            gateway: 隔离 Windows COM、Shell、窗口和进程调用的端口。
            monotonic: 可注入的单调时钟。
            sleep: 可注入的等待函数。
            total_budget_seconds: 单次业务调用的内部总预算。
            new_instance_verification_seconds: 新实例证据的最长等待时间。
            poll_interval_seconds: 新实例验证的轮询间隔。
        """

        self._gateway = gateway
        self._monotonic = monotonic
        self._sleep = sleep
        self._total_budget_seconds = max(0.1, float(total_budget_seconds))
        self._new_instance_verification_seconds = max(
            0.0,
            float(new_instance_verification_seconds),
        )
        self._poll_interval_seconds = max(0.01, float(poll_interval_seconds))
        self._catalog_lock = threading.RLock()
        self._applications: tuple[InstalledApplication, ...] = ()
        self._catalog_initialized = False
        self._catalog_refresh: _CatalogRefresh | None = None

    def prewarm(self) -> None:
        """在后台预热应用目录，提交后立即返回，不阻塞助手启动。

        已在扫描时复用该次扫描；失败只记录诊断，后续 Tool 调用可以重试。

        Returns:
            无返回值；目录是否可用由真实 Tool 调用在自身预算内判断。
        """

        if not self._gateway.is_supported:
            return
        try:
            self._start_catalog_refresh()
        except Exception:
            logger.exception("Windows 应用目录预热失败")

    def open_application(
        self,
        *,
        application_name: str,
        launch_mode: str | LaunchMode = LaunchMode.REUSE_EXISTING,
        side_effect_deadline: float | None = None,
        cancellation_requested: Callable[[], bool] | None = None,
    ) -> ApplicationLaunchResult:
        """匹配并打开一个 Windows 已安装应用。

        Args:
            application_name: 用户希望打开的单个应用名称。
            launch_mode: 复用现有窗口或明确新开实例。
            side_effect_deadline: 编排层允许产生本地副作用的绝对截止时间。
            cancellation_requested: 返回当前调用是否已经取消的无参函数。

        Returns:
            不泄露本机执行身份的结构化 Tool 结果。
        """

        started_at = self._monotonic()
        internal_deadline = started_at + self._total_budget_seconds
        deadline = (
            min(internal_deadline, side_effect_deadline)
            if side_effect_deadline is not None
            else internal_deadline
        )
        is_cancelled = cancellation_requested or (lambda: False)
        normalized_name = normalize_application_name(application_name)
        if not normalized_name or _looks_like_forbidden_target(application_name):
            return ApplicationLaunchResult.rejected(
                reason="invalid_application_name",
                safe_message="这里只能填写一个已安装应用的名称。",
            )
        try:
            normalized_mode = LaunchMode(launch_mode)
        except (TypeError, ValueError):
            return ApplicationLaunchResult.rejected(
                reason="invalid_launch_mode",
                safe_message="应用启动方式无效。",
            )
        if not self._gateway.is_supported:
            return ApplicationLaunchResult.failed(
                reason="unsupported_platform",
                safe_message="当前系统不支持 Windows 应用同步启动。",
            )

        stopped = self._execution_stop_result(
            application=None,
            deadline=deadline,
            cancellation_requested=is_cancelled,
        )
        if stopped is not None:
            return stopped

        try:
            resolved = self._resolve_application(
                application_name=application_name,
                normalized_name=normalized_name,
                deadline=deadline,
                cancellation_requested=is_cancelled,
            )
        except Exception:
            stopped = self._execution_stop_result(
                application=None,
                deadline=deadline,
                cancellation_requested=is_cancelled,
            )
            if stopped is not None:
                return stopped
            logger.exception("读取 Windows 已安装应用目录失败")
            return ApplicationLaunchResult.failed(
                reason="application_catalog_unavailable",
                safe_message="当前无法读取 Windows 已安装应用。",
            )
        stopped = self._execution_stop_result(
            application=(
                resolved if isinstance(resolved, InstalledApplication) else None
            ),
            deadline=deadline,
            cancellation_requested=is_cancelled,
        )
        if stopped is not None:
            return stopped
        if isinstance(resolved, ApplicationLaunchResult):
            return resolved

        try:
            if normalized_mode is LaunchMode.REUSE_EXISTING:
                result = self._reuse_or_launch(
                    application=resolved,
                    deadline=deadline,
                    cancellation_requested=is_cancelled,
                )
            else:
                result = self._launch_new_instance(
                    application=resolved,
                    deadline=deadline,
                    cancellation_requested=is_cancelled,
                )
            # Shell 明确拒绝请求时没有发生启动副作用，常见原因是启动后应用入口已经变化。
            # 此时只刷新一次目录并重新解析；已接受的请求绝不能为了“确认”而重复提交。
            if result.reason == "application_launch_failed":
                stopped = self._execution_stop_result(
                    application=resolved,
                    deadline=deadline,
                    cancellation_requested=is_cancelled,
                )
                if stopped is not None:
                    return stopped
                refreshed = self._resolve_application(
                    application_name=application_name,
                    normalized_name=normalized_name,
                    deadline=deadline,
                    cancellation_requested=is_cancelled,
                    force_refresh=True,
                )
                if isinstance(refreshed, ApplicationLaunchResult):
                    return refreshed
                resolved = refreshed
                if normalized_mode is LaunchMode.REUSE_EXISTING:
                    result = self._reuse_or_launch(
                        application=resolved,
                        deadline=deadline,
                        cancellation_requested=is_cancelled,
                    )
                else:
                    result = self._launch_new_instance(
                        application=resolved,
                        deadline=deadline,
                        cancellation_requested=is_cancelled,
                    )
        except Exception:
            logger.exception("Windows 应用同步启动失败")
            result = ApplicationLaunchResult.failed(
                reason="application_launch_failed",
                safe_message=f"没有成功打开{resolved.display_name}。",
                application_name=resolved.display_name,
            )

        log_event(
            logger,
            logging.INFO if result.state == "completed" else logging.WARNING,
            "windows_application_launch_finished",
            state=result.state,
            outcome=result.outcome or "",
            reason=result.reason or "",
            launch_mode=normalized_mode.value,
        )
        return result

    def _resolve_application(
        self,
        *,
        application_name: str,
        normalized_name: str,
        deadline: float,
        cancellation_requested: Callable[[], bool],
        force_refresh: bool = False,
    ) -> InstalledApplication | ApplicationLaunchResult:
        """从缓存目录解析唯一应用，未命中时刷新一次。

        Args:
            application_name: 原始应用名称，用于安全失败说明。
            normalized_name: 已归一化的匹配名称。
            deadline: 本次调用允许等待目录和执行动作的绝对截止时间。
            cancellation_requested: 返回当前调用是否已经取消的无参函数。
            force_refresh: 是否先刷新目录，用于替换已被 Shell 拒绝的失效入口。

        Returns:
            唯一应用描述，或不应继续执行的结构化结果。
        """

        applications = self._catalog_snapshot(
            force_refresh=force_refresh,
            deadline=deadline,
            cancellation_requested=cancellation_requested,
        )
        if isinstance(applications, ApplicationLaunchResult):
            return applications
        if normalized_name in DEFAULT_BROWSER_NAMES:
            browser = self._gateway.resolve_default_browser(applications)
            if browser is not None:
                return browser
            if force_refresh:
                return ApplicationLaunchResult.rejected(
                    reason="default_browser_unavailable",
                    safe_message="没有找到 Windows 当前的默认浏览器。",
                )
            applications = self._catalog_snapshot(
                force_refresh=True,
                deadline=deadline,
                cancellation_requested=cancellation_requested,
            )
            if isinstance(applications, ApplicationLaunchResult):
                return applications
            browser = self._gateway.resolve_default_browser(applications)
            if browser is not None:
                return browser
            return ApplicationLaunchResult.rejected(
                reason="default_browser_unavailable",
                safe_message="没有找到 Windows 当前的默认浏览器。",
            )

        matches = match_installed_applications(application_name, applications)
        if not matches and not force_refresh:
            applications = self._catalog_snapshot(
                force_refresh=True,
                deadline=deadline,
                cancellation_requested=cancellation_requested,
            )
            if isinstance(applications, ApplicationLaunchResult):
                return applications
            matches = match_installed_applications(application_name, applications)
        if not matches:
            return ApplicationLaunchResult.rejected(
                reason="application_not_found",
                safe_message=f"没有找到已安装应用“{application_name.strip()}”。",
            )
        if len(matches) > 1:
            candidates = tuple(application.display_name for application in matches)
            return ApplicationLaunchResult.rejected(
                reason="ambiguous_application",
                safe_message="找到多个匹配程度相同的应用，请确认要打开哪一个。",
                candidates=candidates,
            )
        return matches[0]

    def _catalog_snapshot(
        self,
        *,
        deadline: float,
        cancellation_requested: Callable[[], bool],
        force_refresh: bool = False,
    ) -> tuple[InstalledApplication, ...] | ApplicationLaunchResult:
        """读取缓存，或在当前调用的预算内等待共享扫描完成。

        Args:
            deadline: 本次调用允许等待目录的绝对截止时间。
            cancellation_requested: 返回当前调用是否已经取消的无参函数。
            force_refresh: 是否忽略现有快照并重新枚举 AppsFolder。

        Returns:
            不可变应用描述，或目录不可用、取消、超时的结构化失败结果。
        """

        stopped = self._execution_stop_result(
            application=None,
            deadline=deadline,
            cancellation_requested=cancellation_requested,
        )
        if stopped is not None:
            return stopped
        with self._catalog_lock:
            # 刷新可能卡在 Windows 系统调用里，已有缓存仍应随时可读。
            if self._catalog_initialized and not force_refresh:
                return self._applications
            refresh = self._start_catalog_refresh()

        while True:
            stopped = self._execution_stop_result(
                application=None,
                deadline=deadline,
                cancellation_requested=cancellation_requested,
            )
            if stopped is not None:
                return stopped
            if refresh.completed.is_set():
                break
            # 取消仅结束当前调用的等待，不能中止其他调用也在使用的目录扫描。
            remaining = deadline - self._monotonic()
            refresh.completed.wait(
                timeout=max(0.0, min(_CATALOG_WAIT_INTERVAL_SECONDS, remaining)),
            )

        if refresh.applications is not None:
            return refresh.applications
        return ApplicationLaunchResult.failed(
            reason="application_catalog_unavailable",
            safe_message="当前无法读取 Windows 已安装应用。",
        )

    def _start_catalog_refresh(self) -> _CatalogRefresh:
        """复用进行中的扫描，或登记一个不会拖住进程退出的后台扫描。"""

        with self._catalog_lock:
            if (
                self._catalog_refresh is not None
                and not self._catalog_refresh.completed.is_set()
            ):
                return self._catalog_refresh
            refresh = _CatalogRefresh()
            self._catalog_refresh = refresh
            try:
                threading.Thread(
                    target=self._refresh_catalog,
                    args=(refresh,),
                    name="windows-application-catalog",
                    daemon=True,
                ).start()
            except Exception:
                # 线程没有启动也必须结束本次扫描，让下一次调用仍有机会重试。
                refresh.completed.set()
                logger.exception("启动 Windows 应用目录后台扫描失败")
            return refresh

    def _refresh_catalog(self, refresh: _CatalogRefresh) -> None:
        """在锁外枚举 Windows 目录，再用短锁发布纯数据快照。"""

        try:
            # COM 调用可能永不返回，绝不能持有缓存锁或占用助手启动线程。
            applications = tuple(self._gateway.list_installed_applications())
            with self._catalog_lock:
                self._applications = applications
                self._catalog_initialized = True
                refresh.applications = applications
            log_event(
                logger,
                logging.INFO,
                "windows_application_catalog_refreshed",
                application_count=len(applications),
            )
        except Exception:
            logger.exception("读取 Windows 已安装应用目录失败")
        finally:
            # 每个等待者保留自己的扫描结果，后续重试不能覆盖这次失败或成功。
            refresh.completed.set()

    def _reuse_or_launch(
        self,
        *,
        application: InstalledApplication,
        deadline: float,
        cancellation_requested: Callable[[], bool],
    ) -> ApplicationLaunchResult:
        """优先置前现有窗口，没有普通窗口时提交注册启动目标。

        Args:
            application: 已唯一匹配的应用。
            deadline: 包含目录解析耗时在内的绝对内部截止时间。
            cancellation_requested: 返回当前调用是否已经取消的无参函数。

        Returns:
            真实置前、启动请求或失败事实。
        """

        stopped = self._execution_stop_result(
            application=application,
            deadline=deadline,
            cancellation_requested=cancellation_requested,
        )
        if stopped is not None:
            return stopped
        current = self._gateway.snapshot(application)
        stopped = self._execution_stop_result(
            application=application,
            deadline=deadline,
            cancellation_requested=cancellation_requested,
        )
        if stopped is not None:
            return stopped
        if current.window_handles:
            if self._gateway.focus_window(current.window_handles[0]):
                return ApplicationLaunchResult.completed(
                    outcome="focused_existing",
                    application_name=application.display_name,
                )
            return ApplicationLaunchResult.failed(
                reason="focus_existing_window_failed",
                safe_message=f"找到了{application.display_name}窗口，但没能把它切到前台。",
                application_name=application.display_name,
            )

        stopped = self._execution_stop_result(
            application=application,
            deadline=deadline,
            cancellation_requested=cancellation_requested,
        )
        if stopped is not None:
            return stopped
        if self._gateway.launch(application):
            return ApplicationLaunchResult.completed(
                outcome="launch_requested",
                application_name=application.display_name,
            )
        return ApplicationLaunchResult.failed(
            reason="application_launch_failed",
            safe_message=f"Windows 没有接受{application.display_name}的启动请求。",
            application_name=application.display_name,
        )

    def _launch_new_instance(
        self,
        *,
        application: InstalledApplication,
        deadline: float,
        cancellation_requested: Callable[[], bool],
    ) -> ApplicationLaunchResult:
        """启动并验证一个不会被旧窗口冒充的新实例。

        Args:
            application: 已唯一匹配的应用。
            deadline: 包含目录解析耗时在内的绝对内部截止时间。
            cancellation_requested: 返回当前调用是否已经取消的无参函数。

        Returns:
            新实例已确认、启动失败或没有形成新实例的事实。
        """

        stopped = self._execution_stop_result(
            application=application,
            deadline=deadline,
            cancellation_requested=cancellation_requested,
        )
        if stopped is not None:
            return stopped
        before = self._gateway.snapshot(application)
        stopped = self._execution_stop_result(
            application=application,
            deadline=deadline,
            cancellation_requested=cancellation_requested,
        )
        if stopped is not None:
            return stopped
        if not self._gateway.launch(application):
            return ApplicationLaunchResult.failed(
                reason="application_launch_failed",
                safe_message=f"Windows 没有接受{application.display_name}的新实例启动请求。",
                application_name=application.display_name,
            )

        verification_deadline = min(
            deadline,
            self._monotonic() + self._new_instance_verification_seconds,
        )
        previous_new_processes: frozenset[int] = frozenset()
        had_existing_instance = bool(before.window_handles or before.process_ids)
        while self._monotonic() < verification_deadline:
            if cancellation_requested():
                return _launch_cancelled(application)
            current = self._gateway.snapshot(application)
            if cancellation_requested():
                return _launch_cancelled(application)
            new_windows = set(current.window_handles) - set(before.window_handles)
            if new_windows:
                return ApplicationLaunchResult.completed(
                    outcome="new_instance_created",
                    application_name=application.display_name,
                )

            # 已有实例时，辅助进程可能因为一次普通激活而出现，不能把它误报成新实例。
            if not had_existing_instance:
                new_processes = current.process_ids - before.process_ids
                if new_processes and new_processes == previous_new_processes:
                    return ApplicationLaunchResult.completed(
                        outcome="new_instance_created",
                        application_name=application.display_name,
                    )
                previous_new_processes = new_processes

            remaining = verification_deadline - self._monotonic()
            if remaining <= 0:
                break
            self._sleep(min(self._poll_interval_seconds, remaining))

        return ApplicationLaunchResult.failed(
            reason="new_instance_not_created",
            safe_message=f"{application.display_name}没有创建可确认的新实例。",
            application_name=application.display_name,
        )

    def _execution_stop_result(
        self,
        *,
        application: InstalledApplication | None,
        deadline: float,
        cancellation_requested: Callable[[], bool],
    ) -> ApplicationLaunchResult | None:
        """在下一步可能产生本地副作用前统一检查取消和截止时间。

        Args:
            application: 已解析出的应用；目录尚未解析时为空。
            deadline: 本次调用允许产生本地副作用的绝对截止时间。
            cancellation_requested: 返回当前调用是否已经取消的无参函数。

        Returns:
            应当立即停止时返回稳定失败事实；仍可继续时返回空值。
        """

        if cancellation_requested():
            return _launch_cancelled(application)
        if self._monotonic() >= deadline:
            return _launch_budget_exhausted(application)
        return None


def normalize_application_name(value: str) -> str:
    """把应用名归一成可稳定比较的字母数字序列。

    Args:
        value: 用户输入或应用目录别名。

    Returns:
        完成 NFKC、大小写和标点空白归一化的字符串。
    """

    if not isinstance(value, str):
        return ""
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(character for character in normalized if character.isalnum())


def match_installed_applications(
    query: str,
    applications: Sequence[InstalledApplication],
) -> tuple[InstalledApplication, ...]:
    """按唯一最高名称重合率筛选已安装应用。

    Args:
        query: 用户希望打开的应用名称。
        applications: 当前不可变应用目录快照。

    Returns:
        零项表示没有正分匹配；一项表示唯一最高分；多项表示最高分并列。
    """

    normalized_query = normalize_application_name(query)
    if not normalized_query:
        return ()

    scored: list[tuple[float, InstalledApplication]] = []
    for application in applications:
        aliases = _application_aliases(application)
        score = max(
            (_overlap_score(normalized_query, alias) for alias in aliases),
            default=0.0,
        )
        if score > 0.0:
            scored.append((score, application))
    if not scored:
        return ()

    highest_score = max(score for score, _application in scored)
    highest = [
        application
        for score, application in scored
        if math.isclose(score, highest_score, rel_tol=0.0, abs_tol=1e-12)
    ]
    deduplicated = {
        application.application_id.casefold(): application
        for application in highest
    }
    return tuple(
        sorted(
            deduplicated.values(),
            key=lambda application: (
                normalize_application_name(application.display_name),
                application.application_id.casefold(),
            ),
        )
    )


def _application_aliases(application: InstalledApplication) -> tuple[str, ...]:
    """生成展示名、系统身份、可执行名和拼音的去重匹配别名。"""

    raw_aliases = [
        application.display_name,
        application.application_id,
        *application.aliases,
    ]
    pinyin_alias = "".join(lazy_pinyin(application.display_name, errors="ignore"))
    if pinyin_alias:
        raw_aliases.append(pinyin_alias)
    normalized_aliases = {
        normalized
        for alias in raw_aliases
        if (normalized := normalize_application_name(alias))
    }
    return tuple(sorted(normalized_aliases))


def _overlap_score(query: str, alias: str) -> float:
    """按精确、包含覆盖率和字符序列重合率计算一个名称分数。"""

    if query == alias:
        return 1.0
    if query in alias or alias in query:
        return min(len(query), len(alias)) / max(len(query), len(alias))
    return SequenceMatcher(None, query, alias, autojunk=False).ratio()


def _looks_like_forbidden_target(value: str) -> bool:
    """拒绝 URL、协议、文件和目录形态，保持 Tool 只接收应用名。"""

    if not isinstance(value, str):
        return True
    return _FORBIDDEN_TARGET_PATTERN.search(value.strip()) is not None


def _launch_budget_exhausted(
    application: InstalledApplication | None,
) -> ApplicationLaunchResult:
    """在任何本地副作用发生前返回稳定超时事实。"""

    return ApplicationLaunchResult.failed(
        reason="application_launch_timeout",
        safe_message=(
            f"打开{application.display_name}的准备过程超时了。"
            if application is not None
            else "应用启动准备过程超时了。"
        ),
        application_name=(
            application.display_name if application is not None else None
        ),
    )


def _launch_cancelled(
    application: InstalledApplication | None,
) -> ApplicationLaunchResult:
    """返回不会暴露本机身份的稳定取消事实。"""

    return ApplicationLaunchResult.failed(
        reason="application_launch_cancelled",
        safe_message="这次应用启动已经取消。",
        application_name=(
            application.display_name if application is not None else None
        ),
    )
