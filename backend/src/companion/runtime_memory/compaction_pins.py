"""管理恢复关系原子提交期间的 Runtime Memory 短时压缩门禁。"""

from __future__ import annotations

from threading import RLock


class RecoveryCompactionPinRegistry:
    """用自我反思编号幂等保护一次尚未完成的恢复记忆提交。"""

    def __init__(self) -> None:
        """创建空门禁集合。"""

        self._self_reflection_ids: set[str] = set()
        self._lock = RLock()

    @property
    def active(self) -> bool:
        """返回当前是否至少有一笔恢复关系尚未可靠写入。

        Returns:
            任意自我反思仍持有压缩门禁时返回真。
        """

        with self._lock:
            return bool(self._self_reflection_ids)

    def contains(self, self_reflection_id: str) -> bool:
        """判断指定自我反思是否仍持有压缩门禁。

        Args:
            self_reflection_id: 前台自我反思编号。

        Returns:
            该编号仍等待恢复记忆提交时返回真。
        """

        normalized = self_reflection_id.strip()
        with self._lock:
            return normalized in self._self_reflection_ids

    def retain(self, self_reflection_id: str) -> bool:
        """为一笔即将写入的恢复关系取得压缩门禁。

        Args:
            self_reflection_id: 非空前台自我反思编号。

        Returns:
            首次取得时返回真，重复取得返回假。
        """

        normalized = self_reflection_id.strip()
        if not normalized:
            raise ValueError("self_reflection_id 不能为空")
        with self._lock:
            if normalized in self._self_reflection_ids:
                return False
            self._self_reflection_ids.add(normalized)
            return True

    def release(self, self_reflection_id: str) -> bool:
        """幂等释放一笔已经提交或明确放弃的恢复门禁。

        Args:
            self_reflection_id: 要收口的前台自我反思编号。

        Returns:
            实际释放时返回真，不存在时返回假。
        """

        normalized = self_reflection_id.strip()
        with self._lock:
            if normalized not in self._self_reflection_ids:
                return False
            self._self_reflection_ids.remove(normalized)
            return True
