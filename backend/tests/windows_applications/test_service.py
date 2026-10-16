"""验证 Windows 应用匹配和同步启动业务，不触发真实系统动作。"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Sequence

import pytest

from companion.capabilities.windows_applications.models import (
    ApplicationLaunchResult,
    ApplicationRuntimeSnapshot,
    InstalledApplication,
)
from companion.capabilities.windows_applications.service import (
    WindowsApplicationLaunchService,
    match_installed_applications,
    normalize_application_name,
)


QQ_APP = InstalledApplication(
    display_name="QQ",
    application_id="QQ",
    launch_target="shell:AppsFolder\\QQ",
    executable_path="E:\\QQNT\\QQ.exe",
    aliases=("QQ.exe",),
)
QQ_BROWSER_APP = InstalledApplication(
    display_name="QQ浏览器",
    application_id="QQBrowser",
    launch_target="shell:AppsFolder\\QQBrowser",
    executable_path="C:\\QQBrowser\\QQBrowser.exe",
)
BILIBILI_APP = InstalledApplication(
    display_name="哔哩哔哩",
    application_id="BiliBiliPC",
    launch_target="shell:AppsFolder\\BiliBiliPC",
    executable_path="C:\\Program Files\\bilibili\\哔哩哔哩.exe",
)


@dataclass
class FakeClock:
    """提供由测试显式推进的单调时钟。"""

    current: float = 0.0

    def monotonic(self) -> float:
        """返回当前测试时间。"""

        return self.current

    def sleep(self, seconds: float) -> None:
        """推进测试时间，不执行真实等待。"""

        self.current += seconds


@dataclass
class FakeWindowsGateway:
    """记录业务服务请求并返回预设 Windows 事实。"""

    application_batches: deque[tuple[InstalledApplication, ...]]
    snapshots: deque[ApplicationRuntimeSnapshot] = field(default_factory=deque)
    default_browser: InstalledApplication | None = None
    focus_succeeded: bool = True
    launch_succeeded: bool = True
    is_supported: bool = True
    list_calls: int = 0
    focus_calls: list[int] = field(default_factory=list)
    launch_calls: list[InstalledApplication] = field(default_factory=list)
    _last_snapshot: ApplicationRuntimeSnapshot = field(
        default_factory=ApplicationRuntimeSnapshot
    )

    def list_installed_applications(self) -> tuple[InstalledApplication, ...]:
        """返回下一批应用目录，耗尽后复用最后一批。"""

        self.list_calls += 1
        if len(self.application_batches) > 1:
            return self.application_batches.popleft()
        if self.application_batches:
            return self.application_batches[0]
        return ()

    def resolve_default_browser(
        self,
        applications: Sequence[InstalledApplication],
    ) -> InstalledApplication | None:
        """返回预设默认浏览器。"""

        del applications
        return self.default_browser

    def snapshot(self, application: InstalledApplication) -> ApplicationRuntimeSnapshot:
        """返回下一项运行时快照。"""

        del application
        if self.snapshots:
            self._last_snapshot = self.snapshots.popleft()
        return self._last_snapshot

    def focus_window(self, window_handle: int) -> bool:
        """记录窗口置前请求。"""

        self.focus_calls.append(window_handle)
        return self.focus_succeeded

    def launch(self, application: InstalledApplication) -> bool:
        """记录 Shell 启动请求。"""

        self.launch_calls.append(application)
        return self.launch_succeeded


def test_normalize_application_name_removes_formatting_noise() -> None:
    """应用名匹配不应受全角、空格和标点影响。"""

    assert normalize_application_name(" ＱＱ-浏览器 ") == "qq浏览器"


def test_exact_match_beats_longer_containing_application() -> None:
    """QQ 精确匹配必须压过 QQ 浏览器。"""

    matches = match_installed_applications("QQ", (QQ_BROWSER_APP, QQ_APP))

    assert matches == (QQ_APP,)


def test_display_name_pinyin_can_match_bilibili() -> None:
    """中文展示名应提供拼音别名。"""

    assert match_installed_applications("bilibili", (BILIBILI_APP,)) == (
        BILIBILI_APP,
    )


def test_equal_highest_score_returns_sorted_candidates() -> None:
    """最高重合率相同时必须返回稳定候选，不能擅自启动。"""

    app_ab = InstalledApplication("AB", "ab", "ab.exe")
    app_ac = InstalledApplication("AC", "ac", "ac.exe")

    assert match_installed_applications("A", (app_ac, app_ab)) == (app_ab, app_ac)


def test_zero_overlap_returns_no_match() -> None:
    """完全没有字符重合时不能把任意应用当成最高匹配。"""

    assert match_installed_applications("Z", (InstalledApplication("AB", "ab", "ab.exe"),)) == ()


def test_equal_highest_score_returns_ambiguous_result_without_launch() -> None:
    """业务入口遇到最高分并列时必须只返回候选，不产生任何本地副作用。"""

    app_ab = InstalledApplication("AB", "ab", "ab.exe")
    app_ac = InstalledApplication("AC", "ac", "ac.exe")
    gateway = FakeWindowsGateway(deque([(app_ac, app_ab)]))
    service = WindowsApplicationLaunchService(gateway=gateway)

    result = service.open_application(application_name="A")

    assert result.state == "rejected"
    assert result.reason == "ambiguous_application"
    assert result.candidates == ("AB", "AC")
    assert gateway.launch_calls == []
    assert gateway.focus_calls == []


def test_zero_overlap_refreshes_once_then_returns_not_found() -> None:
    """两份目录都没有正分匹配时应返回稳定未找到事实。"""

    gateway = FakeWindowsGateway(deque([(QQ_APP,), (QQ_APP,)]))
    service = WindowsApplicationLaunchService(gateway=gateway)

    result = service.open_application(application_name="完全不同的名字")

    assert result.state == "rejected"
    assert result.reason == "application_not_found"
    assert gateway.list_calls == 2
    assert gateway.launch_calls == []


def test_url_is_rejected_before_catalog_or_shell_access() -> None:
    """URL 不能伪装成应用名称进入同步启动能力。"""

    gateway = FakeWindowsGateway(deque([(QQ_APP,)]))
    service = WindowsApplicationLaunchService(gateway=gateway)

    result = service.open_application(application_name="https://www.bilibili.com")

    assert result.reason == "invalid_application_name"
    assert gateway.list_calls == 0
    assert gateway.launch_calls == []


def test_invalid_launch_mode_is_rejected_before_catalog_access() -> None:
    """未知启动模式不能读取目录或产生本地副作用。"""

    gateway = FakeWindowsGateway(deque([(QQ_APP,)]))
    service = WindowsApplicationLaunchService(gateway=gateway)

    result = service.open_application(
        application_name="QQ",
        launch_mode="restart_existing",
    )

    assert result.reason == "invalid_launch_mode"
    assert gateway.list_calls == 0
    assert gateway.launch_calls == []


def test_unsupported_platform_returns_stable_failure_without_catalog_access() -> None:
    """非 Windows 平台保留合同，但不能尝试枚举或启动应用。"""

    gateway = FakeWindowsGateway(
        deque([(QQ_APP,)]),
        is_supported=False,
    )
    service = WindowsApplicationLaunchService(gateway=gateway)

    result = service.open_application(application_name="QQ")

    assert result.state == "failed"
    assert result.reason == "unsupported_platform"
    assert gateway.list_calls == 0
    assert gateway.launch_calls == []


def test_catalog_refreshes_once_after_initial_miss() -> None:
    """Assistant 启动后新安装的应用应在首次未命中时被重新发现。"""

    gateway = FakeWindowsGateway(deque([(), (QQ_APP,)]))
    service = WindowsApplicationLaunchService(gateway=gateway)

    result = service.open_application(application_name="QQ")

    assert result.outcome == "launch_requested"
    assert gateway.list_calls == 2
    assert gateway.launch_calls == [QQ_APP]


def test_stale_entry_refreshes_once_after_shell_rejection() -> None:
    """旧入口被 Shell 拒绝后，应刷新目录并且只重试新解析出的入口。"""

    refreshed_qq_app = InstalledApplication(
        display_name="QQ",
        application_id="QQ",
        launch_target="shell:AppsFolder\\QQ.Refreshed",
        executable_path="E:\\QQNT\\QQ.exe",
    )

    class StaleEntryGateway(FakeWindowsGateway):
        """第一次拒绝旧入口，第二次只接受刷新后的入口。"""

        def launch(self, application: InstalledApplication) -> bool:
            """记录入口，并模拟 Windows 只接受刷新后的注册目标。"""

            self.launch_calls.append(application)
            return application is refreshed_qq_app

    gateway = StaleEntryGateway(
        deque(
            [
                (QQ_APP,),
                (refreshed_qq_app,),
            ]
        )
    )
    service = WindowsApplicationLaunchService(gateway=gateway)

    result = service.open_application(application_name="QQ")

    assert result.outcome == "launch_requested"
    assert gateway.list_calls == 2
    assert gateway.launch_calls == [QQ_APP, refreshed_qq_app]


def test_shell_rejection_refreshes_once_then_returns_stable_failure() -> None:
    """刷新后的入口仍被 Shell 拒绝时必须失败，不能循环重试或伪报完成。"""

    gateway = FakeWindowsGateway(
        deque([(QQ_APP,), (QQ_APP,)]),
        launch_succeeded=False,
    )
    service = WindowsApplicationLaunchService(gateway=gateway)

    result = service.open_application(application_name="QQ")

    assert result.state == "failed"
    assert result.reason == "application_launch_failed"
    assert gateway.list_calls == 2
    assert gateway.launch_calls == [QQ_APP, QQ_APP]


def test_catalog_delay_cannot_launch_after_internal_deadline() -> None:
    """目录读取耗尽内部预算后必须在任何 Shell 副作用前失败。"""

    clock = FakeClock()

    class SlowCatalogGateway(FakeWindowsGateway):
        """在枚举目录时故意耗尽业务预算。"""

        def list_installed_applications(self) -> tuple[InstalledApplication, ...]:
            """推进时钟后返回原应用目录。"""

            clock.current += 4.0
            return super().list_installed_applications()

    gateway = SlowCatalogGateway(deque([(QQ_APP,)]))
    service = WindowsApplicationLaunchService(
        gateway=gateway,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )

    result = service.open_application(application_name="QQ")

    assert result.reason == "application_launch_timeout"
    assert gateway.launch_calls == []
    assert gateway.focus_calls == []


def test_generation_cancellation_during_catalog_read_prevents_late_launch() -> None:
    """目录读取期间取消 generation 后，工作线程恢复时不得再启动应用。"""

    catalog_started = threading.Event()
    allow_catalog_return = threading.Event()
    cancellation_requested = threading.Event()

    class BlockingCatalogGateway(FakeWindowsGateway):
        """把只读目录阶段停住，方便测试取消与启动之间的竞态。"""

        def list_installed_applications(self) -> tuple[InstalledApplication, ...]:
            """等待测试触发取消后再返回应用目录。"""

            catalog_started.set()
            if not allow_catalog_return.wait(timeout=1.0):
                raise TimeoutError("测试没有释放应用目录")
            return super().list_installed_applications()

    gateway = BlockingCatalogGateway(deque([(QQ_APP,)]))
    service = WindowsApplicationLaunchService(gateway=gateway)
    results: list[ApplicationLaunchResult] = []

    def open_application() -> None:
        """在独立线程执行真实业务服务，记录最终结构化结果。"""

        results.append(
            service.open_application(
                application_name="QQ",
                cancellation_requested=cancellation_requested.is_set,
            )
        )

    worker = threading.Thread(target=open_application)
    worker.start()
    assert catalog_started.wait(timeout=0.5) is True
    cancellation_requested.set()
    allow_catalog_return.set()
    worker.join(timeout=1.0)

    assert worker.is_alive() is False
    assert results[0].reason == "application_launch_cancelled"
    assert gateway.focus_calls == []
    assert gateway.launch_calls == []


def test_generic_browser_uses_windows_default_browser() -> None:
    """通用“浏览器”名称必须解析默认浏览器，而不是做普通模糊匹配。"""

    gateway = FakeWindowsGateway(
        deque([(QQ_BROWSER_APP,)]),
        default_browser=QQ_BROWSER_APP,
    )
    service = WindowsApplicationLaunchService(gateway=gateway)

    result = service.open_application(application_name="浏览器")

    assert result.outcome == "launch_requested"
    assert result.application_name == "QQ浏览器"
    assert gateway.launch_calls == [QQ_BROWSER_APP]


def test_reuse_existing_focuses_first_window_without_launching() -> None:
    """reuse_existing 找到普通窗口时只唤起 Z 序第一项。"""

    gateway = FakeWindowsGateway(
        deque([(QQ_APP,)]),
        snapshots=deque(
            [ApplicationRuntimeSnapshot(window_handles=(101, 202), process_ids=frozenset({1}))]
        ),
    )
    service = WindowsApplicationLaunchService(gateway=gateway)

    result = service.open_application(application_name="QQ")

    assert result.outcome == "focused_existing"
    assert gateway.focus_calls == [101]
    assert gateway.launch_calls == []


def test_focus_failure_does_not_fall_back_to_second_instance() -> None:
    """现有窗口置前失败时不能通过启动命令偷偷新开实例。"""

    gateway = FakeWindowsGateway(
        deque([(QQ_APP,)]),
        snapshots=deque([ApplicationRuntimeSnapshot(window_handles=(101,))]),
        focus_succeeded=False,
    )
    service = WindowsApplicationLaunchService(gateway=gateway)

    result = service.open_application(application_name="QQ")

    assert result.reason == "focus_existing_window_failed"
    assert gateway.launch_calls == []


def test_new_instance_accepts_stable_process_when_no_instance_existed() -> None:
    """首次启动没有及时见窗时，连续两次存在的新主进程可以证明实例已创建。"""

    clock = FakeClock()
    gateway = FakeWindowsGateway(
        deque([(QQ_APP,)]),
        snapshots=deque(
            [
                ApplicationRuntimeSnapshot(),
                ApplicationRuntimeSnapshot(process_ids=frozenset({7})),
                ApplicationRuntimeSnapshot(process_ids=frozenset({7})),
            ]
        ),
    )
    service = WindowsApplicationLaunchService(
        gateway=gateway,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
        new_instance_verification_seconds=0.5,
    )

    result = service.open_application(
        application_name="QQ",
        launch_mode="new_instance",
    )

    assert result.outcome == "new_instance_created"


def test_existing_instance_requires_new_window_not_helper_process() -> None:
    """已有实例时新增辅助进程不能冒充新实例。"""

    clock = FakeClock()
    gateway = FakeWindowsGateway(
        deque([(QQ_APP,)]),
        snapshots=deque(
            [
                ApplicationRuntimeSnapshot(
                    window_handles=(101,),
                    process_ids=frozenset({1}),
                ),
                ApplicationRuntimeSnapshot(
                    window_handles=(101,),
                    process_ids=frozenset({1, 2}),
                ),
            ]
        ),
    )
    service = WindowsApplicationLaunchService(
        gateway=gateway,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
        total_budget_seconds=0.25,
        new_instance_verification_seconds=0.2,
        poll_interval_seconds=0.1,
    )

    result = service.open_application(
        application_name="QQ",
        launch_mode="new_instance",
    )

    assert result.reason == "new_instance_not_created"


def test_existing_instance_succeeds_only_after_new_window_appears() -> None:
    """已有实例时观察到新的普通窗口才确认 new_instance。"""

    clock = FakeClock()
    gateway = FakeWindowsGateway(
        deque([(QQ_APP,)]),
        snapshots=deque(
            [
                ApplicationRuntimeSnapshot(window_handles=(101,)),
                ApplicationRuntimeSnapshot(window_handles=(101, 202)),
            ]
        ),
    )
    service = WindowsApplicationLaunchService(
        gateway=gateway,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )

    result = service.open_application(
        application_name="QQ",
        launch_mode="new_instance",
    )

    assert result.outcome == "new_instance_created"


@dataclass
class BlockingCatalogGateway(FakeWindowsGateway):
    """用可释放的阻塞模拟 AppsFolder 卡住，不访问真实 Windows 应用。"""

    blocked_call_number: int = 1
    fail_blocked_call: bool = False
    catalog_started: threading.Event = field(default_factory=threading.Event)
    release_catalog: threading.Event = field(default_factory=threading.Event)
    catalog_worker: threading.Thread | None = None

    def list_installed_applications(self) -> tuple[InstalledApplication, ...]:
        """仅阻塞指定的一次扫描，便于验证缓存、取消和失败重试。

        Returns:
            预设的不可变应用目录。
        """

        applications = super().list_installed_applications()
        if self.list_calls != self.blocked_call_number:
            return applications
        self.catalog_worker = threading.current_thread()
        self.catalog_started.set()
        if not self.release_catalog.wait(timeout=3.0):
            raise TimeoutError("测试没有释放应用目录")
        if self.fail_blocked_call:
            raise OSError("模拟 Windows 目录不可用")
        return applications


def test_blocked_prewarm_returns_immediately_and_reuses_one_daemon_worker() -> None:
    """可选预热不能挡住 Assistant，重复调用也不能堆积卡死的系统线程。"""

    gateway = BlockingCatalogGateway(deque([(QQ_APP,)]))
    service = WindowsApplicationLaunchService(gateway=gateway)
    try:
        started_at = time.monotonic()
        service.prewarm()
        assert time.monotonic() - started_at < 0.5
        assert gateway.catalog_started.wait(timeout=0.5)
        assert gateway.catalog_worker is not None
        assert gateway.catalog_worker.daemon
        for _ in range(5):
            service.prewarm()

        started_at = time.monotonic()
        result = service.open_application(
            application_name="QQ",
            side_effect_deadline=started_at + 0.12,
        )

        assert result.reason == "application_launch_timeout"
        assert time.monotonic() - started_at < 0.5
        assert not gateway.release_catalog.is_set()
        assert gateway.list_calls == 1
        assert gateway.launch_calls == []
    finally:
        gateway.release_catalog.set()
        if gateway.catalog_worker is not None:
            gateway.catalog_worker.join(timeout=1.0)

    # 超时之后完成的扫描只能填充缓存，不会补做已失效调用的启动动作。
    assert gateway.launch_calls == []
    assert gateway.focus_calls == []
    assert service.open_application(application_name="QQ").outcome == "launch_requested"
    assert gateway.list_calls == 1


def test_concurrent_catalog_waiters_can_cancel_independently() -> None:
    """一个用户轮取消时要立即返回，另一个等待者仍可使用同一次扫描。"""

    gateway = BlockingCatalogGateway(deque([(QQ_APP,)]))
    service = WindowsApplicationLaunchService(gateway=gateway)
    cancelled = threading.Event()
    results: dict[str, ApplicationLaunchResult] = {}

    def open_application(name: str) -> None:
        """把每个调用的结果记在独立位置。"""

        results[name] = service.open_application(
            application_name="QQ",
            cancellation_requested=cancelled.is_set if name == "cancelled" else None,
        )

    workers = [
        threading.Thread(target=open_application, args=(name,), daemon=True)
        for name in ("cancelled", "active")
    ]
    try:
        for worker in workers:
            worker.start()
        assert gateway.catalog_started.wait(timeout=0.5)
        cancelled.set()
        workers[0].join(timeout=0.5)

        assert not workers[0].is_alive()
        assert results["cancelled"].reason == "application_launch_cancelled"
        assert not gateway.release_catalog.is_set()
        assert gateway.launch_calls == []
        assert workers[1].is_alive()

        gateway.release_catalog.set()
        workers[1].join(timeout=1.0)
        assert not workers[1].is_alive()
        assert results["active"].outcome == "launch_requested"
        assert gateway.list_calls == 1
        assert gateway.launch_calls == [QQ_APP]
    finally:
        gateway.release_catalog.set()
        for worker in workers:
            worker.join(timeout=1.0)


def test_failed_catalog_scan_can_retry_on_next_call() -> None:
    """目录失败后必须结束该次扫描，后续调用可以重新枚举并缓存成功结果。"""

    gateway = BlockingCatalogGateway(deque([(QQ_APP,)]), fail_blocked_call=True)
    gateway.release_catalog.set()
    service = WindowsApplicationLaunchService(gateway=gateway)

    failed = service.open_application(application_name="QQ")
    succeeded = service.open_application(application_name="QQ")
    cached = service.open_application(application_name="QQ")

    assert failed.reason == "application_catalog_unavailable"
    assert succeeded.outcome == cached.outcome == "launch_requested"
    assert gateway.list_calls == 2
    assert gateway.launch_calls == [QQ_APP, QQ_APP]


def test_failed_prewarm_can_retry_on_first_tool_call() -> None:
    """后台预热失败不能让第一次真正使用应用启动工具也固定失败。"""

    gateway = BlockingCatalogGateway(deque([(QQ_APP,)]), fail_blocked_call=True)
    gateway.release_catalog.set()
    service = WindowsApplicationLaunchService(gateway=gateway)
    service.prewarm()
    assert gateway.catalog_started.wait(timeout=0.5)
    assert gateway.catalog_worker is not None
    gateway.catalog_worker.join(timeout=1.0)

    result = service.open_application(application_name="QQ")

    assert result.outcome == "launch_requested"
    assert gateway.list_calls == 2


def test_blocked_refresh_does_not_lock_out_cached_reads() -> None:
    """新应用未命中触发的刷新卡住时，已知应用仍然可从旧缓存启动。"""

    gateway = BlockingCatalogGateway(deque([(QQ_APP,)]), blocked_call_number=2)
    service = WindowsApplicationLaunchService(gateway=gateway)
    assert service.open_application(application_name="QQ").outcome == "launch_requested"
    results: list[ApplicationLaunchResult] = []
    caller = threading.Thread(
        target=lambda: results.append(service.open_application(
            application_name="完全不同的名字",
            side_effect_deadline=time.monotonic() + 0.25,
        )),
        daemon=True,
    )
    try:
        caller.start()
        assert gateway.catalog_started.wait(timeout=0.5)
        started_at = time.monotonic()
        cached = service.open_application(application_name="QQ")
        assert time.monotonic() - started_at < 0.5
        assert cached.outcome == "launch_requested"
        caller.join(timeout=0.5)
        assert not caller.is_alive()
        assert results[0].reason == "application_launch_timeout"
        assert gateway.list_calls == 2
        assert gateway.launch_calls == [QQ_APP, QQ_APP]
    finally:
        gateway.release_catalog.set()
        caller.join(timeout=1.0)


def test_stale_entry_refresh_obeys_original_deadline() -> None:
    """Shell 拒绝后的第二次目录扫描也必须及时超时，不能耗尽后继续启动。"""

    gateway = BlockingCatalogGateway(
        deque([(QQ_APP,)]), blocked_call_number=2, launch_succeeded=False,
    )
    service = WindowsApplicationLaunchService(gateway=gateway)
    try:
        started_at = time.monotonic()
        result = service.open_application(
            application_name="QQ",
            side_effect_deadline=started_at + 0.15,
        )

        assert result.reason == "application_launch_timeout"
        assert time.monotonic() - started_at < 0.5
        assert gateway.catalog_started.is_set()
        assert gateway.launch_calls == [QQ_APP]
    finally:
        gateway.release_catalog.set()
        if gateway.catalog_worker is not None:
            gateway.catalog_worker.join(timeout=1.0)

    assert gateway.launch_calls == [QQ_APP]


def test_stale_entry_refresh_failure_is_catalog_unavailable() -> None:
    """失效入口刷新失败时保留目录不可用事实，不能误报第二次启动被拒绝。"""

    gateway = BlockingCatalogGateway(
        deque([(QQ_APP,)]), blocked_call_number=2, fail_blocked_call=True,
        launch_succeeded=False,
    )
    gateway.release_catalog.set()
    service = WindowsApplicationLaunchService(gateway=gateway)

    result = service.open_application(application_name="QQ")

    assert result.reason == "application_catalog_unavailable"
    assert gateway.list_calls == 2
    assert gateway.launch_calls == [QQ_APP]


def test_catalog_worker_start_failure_can_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """线程资源暂时不足时返回目录失败，恢复后不能仍等待一个不存在的线程。"""

    gateway = FakeWindowsGateway(deque([(QQ_APP,)]))
    service = WindowsApplicationLaunchService(gateway=gateway)

    def fail_start(_thread: threading.Thread) -> None:
        """模拟后台线程尚未创建就失败。"""

        raise RuntimeError("模拟线程资源不足")

    with monkeypatch.context() as patch:
        patch.setattr(threading.Thread, "start", fail_start)
        failed = service.open_application(application_name="QQ")

    result = service.open_application(application_name="QQ")

    assert failed.reason == "application_catalog_unavailable"
    assert result.outcome == "launch_requested"
    assert gateway.list_calls == 1
