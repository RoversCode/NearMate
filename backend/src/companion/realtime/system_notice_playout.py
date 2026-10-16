"""跟踪仍可能发声的权限 System Notice 请求。"""

from __future__ import annotations

from collections.abc import Iterable
from threading import RLock


class SystemNoticePlayoutTracker:
    """保存权限交互与 System Notice 请求之间的短期播放映射。

    这里只记录仍可能继续生成或播放语音的请求。文本已经最终显示不代表音频结束，
    因此映射要等音频完成、中断或精确取消后才释放。
    """

    def __init__(self) -> None:
        """初始化空映射和跨线程锁。"""

        self._interaction_ids_by_request: dict[str, frozenset[str]] = {}
        self._request_ids_by_interaction: dict[str, set[str]] = {}
        self._lock = RLock()

    def register_request(
        self,
        *,
        request_id: str,
        interaction_request_ids: Iterable[str],
    ) -> None:
        """登记一条 System Notice 承载的权限交互。

        Args:
            request_id: 已准备入队的 System Notice 请求编号。
            interaction_request_ids: 本次语音会表达的权限交互请求编号。

        Raises:
            ValueError: System Notice 请求编号为空。
        """

        normalized_request_id = request_id.strip()
        if not normalized_request_id:
            raise ValueError("System Notice 请求编号不能为空")
        normalized_interaction_ids = frozenset(
            interaction_id.strip()
            for interaction_id in interaction_request_ids
            if interaction_id.strip()
        )
        with self._lock:
            self._finish_request_locked(normalized_request_id)
            if not normalized_interaction_ids:
                return
            self._interaction_ids_by_request[normalized_request_id] = (
                normalized_interaction_ids
            )
            for interaction_id in normalized_interaction_ids:
                self._request_ids_by_interaction.setdefault(interaction_id, set()).add(
                    normalized_request_id
                )

    def resolve_interaction(self, interaction_request_id: str) -> tuple[str, ...]:
        """取出并释放承载指定权限交互的全部 System Notice 请求。

        Args:
            interaction_request_id: 已经得到用户决定的权限交互请求编号。

        Returns:
            需要停止生成或播放的 System Notice 请求编号，按字符串稳定排序。
        """

        normalized_interaction_id = interaction_request_id.strip()
        if not normalized_interaction_id:
            return ()
        with self._lock:
            request_ids = tuple(
                sorted(
                    self._request_ids_by_interaction.get(
                        normalized_interaction_id,
                        set(),
                    )
                )
            )
            for request_id in request_ids:
                self._finish_request_locked(request_id)
            return request_ids

    def finish_request(self, request_id: str) -> None:
        """释放已经完成、中断或取消的 System Notice 请求映射。

        Args:
            request_id: 已经不会再产生语音的 System Notice 请求编号。
        """

        normalized_request_id = request_id.strip()
        if not normalized_request_id:
            return
        with self._lock:
            self._finish_request_locked(normalized_request_id)

    def clear(self) -> None:
        """在媒体会话结束时清空全部短期映射。"""

        with self._lock:
            self._interaction_ids_by_request.clear()
            self._request_ids_by_interaction.clear()

    def _finish_request_locked(self, request_id: str) -> None:
        """在持锁状态下同时删除正向和反向映射。"""

        interaction_ids = self._interaction_ids_by_request.pop(request_id, ())
        for interaction_id in interaction_ids:
            request_ids = self._request_ids_by_interaction.get(interaction_id)
            if request_ids is None:
                continue
            request_ids.discard(request_id)
            if not request_ids:
                self._request_ids_by_interaction.pop(interaction_id, None)
