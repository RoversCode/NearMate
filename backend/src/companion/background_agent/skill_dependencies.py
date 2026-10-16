"""检查产品 Skill 声明的外部可执行程序依赖。"""

from __future__ import annotations

import re
import shutil
import subprocess
import time
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from threading import Event, Lock

from companion.background_agent.skill_models import (
    SkillDependencySpec,
    SkillDependencyStatus,
)


_VERSION_PATTERN = re.compile(r"(?<![\d.])\d+(?:\.\d+)*(?![\d.])")
_DEFAULT_CACHE_TTL_SECONDS = 5.0
_MAX_PARALLEL_DEPENDENCY_CHECKS = 8


@dataclass(frozen=True, slots=True)
class _CachedDependencyStatus:
    """保存一项依赖状态及其失效时刻。

    Args:
        status: 已完成的依赖检查结果。
        expires_at: 单调时钟到达该秒数后缓存失效。
    """

    status: SkillDependencyStatus
    expires_at: float


@dataclass(frozen=True, slots=True)
class _InFlightDependencyCheck:
    """记录正在执行且可供其它线程等待的同一项检查。

    Args:
        completed: 首个检查线程结束时设置的同步事件。
        cache_generation: 检查开始时的缓存世代，用于隔离 refresh。
    """

    completed: Event
    cache_generation: int


class SkillDependencyChecker:
    """执行轻量、有限时的 Skill 外部命令预检。

    Args:
        timeout_seconds: 单次命令版本检查的最长等待时间。
        cache_ttl_seconds: 一项检查结果在当前 Registry 内复用的秒数。
        clock: 返回单调秒数的时钟；测试可注入可控实现。

    Raises:
        ValueError: 超时时间或缓存有效期不是正数。
    """

    def __init__(
        self,
        *,
        timeout_seconds: float = 2.0,
        cache_ttl_seconds: float = _DEFAULT_CACHE_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """创建依赖检查器。

        Args:
            timeout_seconds: 单次命令版本检查的最长等待时间。
            cache_ttl_seconds: 检查结果在当前 Checker 内复用的秒数。
            clock: 返回单调秒数的时钟；测试可注入可控实现。

        Raises:
            ValueError: 超时时间或缓存有效期不是正数。
        """

        if timeout_seconds <= 0:
            raise ValueError("Skill 依赖检查超时时间必须大于零")
        if cache_ttl_seconds <= 0:
            raise ValueError("Skill 依赖缓存有效期必须大于零")
        self.timeout_seconds = timeout_seconds
        self.cache_ttl_seconds = cache_ttl_seconds
        self._clock = clock
        self._cache_lock = Lock()
        self._cache_generation = 0
        self._cached_statuses: dict[
            SkillDependencySpec,
            _CachedDependencyStatus,
        ] = {}
        self._in_flight_checks: dict[
            SkillDependencySpec,
            _InFlightDependencyCheck,
        ] = {}

    def check(self, specification: SkillDependencySpec) -> SkillDependencyStatus:
        """线程安全地读取或执行一项外部命令依赖检查。

        Args:
            specification: 清单中已经验证过的 executable 依赖。

        Returns:
            包含路径、版本和失败原因的依赖状态。
        """

        while True:
            with self._cache_lock:
                cached_status = self._cached_statuses.get(specification)
                if (
                    cached_status is not None
                    and cached_status.expires_at > self._clock()
                ):
                    return cached_status.status
                if cached_status is not None:
                    self._cached_statuses.pop(specification, None)

                in_flight_check = self._in_flight_checks.get(specification)
                if in_flight_check is None:
                    in_flight_check = _InFlightDependencyCheck(
                        completed=Event(),
                        cache_generation=self._cache_generation,
                    )
                    self._in_flight_checks[specification] = in_flight_check
                    break
            # 首个线程在锁外执行命令。其它线程只等待同一项检查，避免把慢命令
            # 放进全局互斥区，也避免同一依赖并发启动多个子进程。
            in_flight_check.completed.wait()

        try:
            status = self._check_uncached(specification)
        except BaseException:
            with self._cache_lock:
                completed_check = self._in_flight_checks.pop(specification, None)
                if completed_check is not None:
                    completed_check.completed.set()
            raise

        with self._cache_lock:
            # refresh 会递增世代。旧世代已在执行的结果可以返回给原调用方，但
            # 不能重新塞回刚清空的缓存影响后续查询。
            if in_flight_check.cache_generation == self._cache_generation:
                self._cached_statuses[specification] = _CachedDependencyStatus(
                    status=status,
                    expires_at=self._clock() + self.cache_ttl_seconds,
                )
            completed_check = self._in_flight_checks.pop(specification, None)
            if completed_check is not None:
                completed_check.completed.set()
        return status

    def check_many(
        self,
        specifications: Iterable[SkillDependencySpec],
    ) -> dict[SkillDependencySpec, SkillDependencyStatus]:
        """按完整依赖声明去重后检查一批依赖。

        Args:
            specifications: 一个或多个 Skill 声明的依赖，可包含重复项。

        Returns:
            每项唯一依赖声明对应的当前状态，保持首次出现顺序。
        """

        unique_specifications = tuple(dict.fromkeys(specifications))
        if len(unique_specifications) <= 1:
            return {
                specification: self.check(specification)
                for specification in unique_specifications
            }

        # 不同命令彼此独立，冷启动时并行探测可以避免 30 个 Skill 把每项
        # 两秒超时串成一分钟；线程数有硬上限，不能因清单增长耗尽本机资源。
        worker_count = min(
            len(unique_specifications),
            _MAX_PARALLEL_DEPENDENCY_CHECKS,
        )
        with ThreadPoolExecutor(
            max_workers=worker_count,
            thread_name_prefix="skill-dependency",
        ) as executor:
            statuses = executor.map(self.check, unique_specifications)
            return dict(zip(unique_specifications, statuses, strict=True))

    def clear_cache(self) -> None:
        """清除当前 Registry 私有的依赖状态缓存。

        Returns:
            无返回值。
        """

        with self._cache_lock:
            self._cache_generation += 1
            self._cached_statuses.clear()

    def _check_uncached(
        self,
        specification: SkillDependencySpec,
    ) -> SkillDependencyStatus:
        """实际检查外部命令，并把失败收敛成可展示状态。

        Args:
            specification: 清单中已经验证过的 executable 依赖。

        Returns:
            本次真实探测得到的依赖状态。
        """

        executable_path = shutil.which(specification.name)
        if not executable_path:
            return SkillDependencyStatus(
                specification=specification,
                ready=False,
                reason=f"系统找不到命令 {specification.name}",
            )
        resolved_path = str(Path(executable_path).resolve())
        if specification.minimum_version is None:
            return SkillDependencyStatus(
                specification=specification,
                ready=True,
                executable_path=resolved_path,
            )
        try:
            completed = subprocess.run(
                [executable_path, "--version"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exception:
            return SkillDependencyStatus(
                specification=specification,
                ready=False,
                executable_path=resolved_path,
                reason=f"无法读取 {specification.name} 版本：{exception}",
            )
        if completed.returncode != 0:
            return SkillDependencyStatus(
                specification=specification,
                ready=False,
                executable_path=resolved_path,
                reason=f"{specification.name} 版本检查执行失败",
            )
        output = f"{completed.stdout}\n{completed.stderr}"
        match = _VERSION_PATTERN.search(output)
        if match is None:
            return SkillDependencyStatus(
                specification=specification,
                ready=False,
                executable_path=resolved_path,
                reason=f"无法识别 {specification.name} 的版本",
            )
        detected_version = match.group(0)
        if not _version_is_at_least(
            detected_version,
            specification.minimum_version,
        ):
            return SkillDependencyStatus(
                specification=specification,
                ready=False,
                executable_path=resolved_path,
                detected_version=detected_version,
                reason=(
                    f"{specification.name} 版本 {detected_version} 低于要求 "
                    f"{specification.minimum_version}"
                ),
            )
        return SkillDependencyStatus(
            specification=specification,
            ready=True,
            executable_path=resolved_path,
            detected_version=detected_version,
        )


def _version_tuple(value: str) -> tuple[int, ...]:
    """把版本号转成可比较的整数元组。

    Args:
        value: 已通过清单或命令输出校验的纯数字版本。

    Returns:
        每个点分版本段对应的整数元组。
    """

    return tuple(int(part) for part in value.split("."))


def _version_is_at_least(detected: str, minimum: str) -> bool:
    """按缺失版本段补零后比较两个纯数字版本。

    Args:
        detected: 外部命令报告的版本。
        minimum: Skill 清单声明的最低版本。

    Returns:
        当前版本不低于最低版本时返回真。
    """

    detected_parts = _version_tuple(detected)
    minimum_parts = _version_tuple(minimum)
    width = max(len(detected_parts), len(minimum_parts))
    return detected_parts + (0,) * (width - len(detected_parts)) >= (
        minimum_parts + (0,) * (width - len(minimum_parts))
    )
