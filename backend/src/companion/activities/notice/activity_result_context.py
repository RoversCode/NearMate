"""把后台工作完成摘要安全投影给本轮 Foreground Notice。"""

from __future__ import annotations

from collections.abc import Callable

from companion.activities.runtime.models import ActivityCompletionResult
from companion.observability.logging_config import get_backend_logger


logger = get_backend_logger(__name__)
# 后台工作摘要本身应当简短；上限只负责隔离异常数据，不能回退读取完整成果。
DEFAULT_ACTIVITY_NOTICE_RESULT_MAX_CHARS = 2_000


class ActivityNoticeResultContextProjector:
    """把后台工作短交付摘要临时放进完成提醒。

    投影器只在 System Notice 或用户轮附着 Notice 构造 Foreground 输入时读取
    Activity Registry。它不保存成果、不修改提醒领取顺序，也不参与 Notice 完成判定。
    只有 ``work_result`` 可以读取中立 ``delivery_brief``，以及任务是否向聊天窗口
    登记了结果卡的布尔事实；完整 ``display_content`` 和输出路径永远不进入
    Foreground Prompt 和 TTS。
    """

    def __init__(
        self,
        *,
        result_provider: Callable[[str], ActivityCompletionResult],
        max_chars: int = DEFAULT_ACTIVITY_NOTICE_RESULT_MAX_CHARS,
    ) -> None:
        """初始化通用 Activity 结果上下文投影器。

        Args:
            result_provider: 按 Activity 编号读取结构化完成结果的只读端口。
            max_chars: 单次 Foreground 提醒允许携带的结果事实最大字符数。

        Raises:
            ValueError: 结果事实长度上限不是正数。
        """

        if max_chars <= 0:
            raise ValueError("max_chars 必须大于零")
        self._result_provider = result_provider
        self._max_chars = max_chars

    def project(self, notice_context: dict[str, object]) -> dict[str, object]:
        """为需要表达的后台成功终态附加一次性结果上下文。

        Args:
            notice_context: Notice 协调器生成、即将传给 Foreground 的提醒上下文。

        Returns:
            新的提醒上下文字典。无法读取、为空或超长时保持原提醒事实。
        """

        raw_notices = notice_context.get("notice_batch")
        if not isinstance(raw_notices, list):
            return notice_context

        projected_notices: list[object] = []
        for raw_notice in raw_notices:
            # 跨进程边界的提醒快照是普通字典；异常形状必须原样保留，不能静默丢提醒。
            if not isinstance(raw_notice, dict):
                projected_notices.append(raw_notice)
                continue
            notice = dict(raw_notice)
            if not _is_completed_activity_notice(notice):
                projected_notices.append(notice)
                continue

            # work_result 的结果上下文只能来自下方权威结果提供器。即使上游字典异常
            # 携带旧字段，也要先删掉，避免读取失败时把完整成果原样送进 Prompt。
            notice.pop("result_context", None)
            # 结果卡登记事实同理：伪造的 deliverable 标记会让前台指引一个不存在的入口。
            notice.pop("deliverable", None)

            activity_id = str(notice.get("activity_id") or "").strip()
            try:
                completion_result = self._result_provider(activity_id)
            except Exception:
                # 成果只用于丰富自然汇报，读取失败不能阻断已经成立的完成提醒。
                logger.warning(
                    "主动提醒读取 Activity 完成结果失败 activity_id=%s",
                    activity_id,
                    exc_info=True,
                )
                projected_notices.append(notice)
                continue
            result_context = completion_result.delivery_brief.strip()
            # 局部截断会把残缺内容伪装成完整事实；超限时只根据任务目标表达完成。
            if result_context and len(result_context) <= self._max_chars:
                notice["result_context"] = result_context
            # 摘要缺失或超长不影响结果卡事实：登记了就必须指引，没登记就保持静默。
            if completion_result.deliverable:
                notice["deliverable"] = True
            projected_notices.append(notice)

        projected = dict(notice_context)
        projected["notice_batch"] = projected_notices
        return projected


def _is_completed_activity_notice(notice: dict[str, object]) -> bool:
    """判断提醒是否有资格读取 Activity 完成结果。

    Args:
        notice: Notice 协调器生成的单条提醒事实。

    Returns:
        提醒属于成功终态且携带有效 Activity 编号时返回真。
    """

    return notice.get("intent") == "work_result" and bool(
        str(notice.get("activity_id") or "").strip()
    )
