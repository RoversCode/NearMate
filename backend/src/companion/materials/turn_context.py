from __future__ import annotations

import uuid
from dataclasses import replace
from threading import RLock
from typing import Any

from companion.materials.failure_reasons import describe_material_failure
from companion.materials.ingestion import MaterialIngestionService
from companion.materials.models import (
    MaterialAttachment,
    MaterialAdmissionIssue,
    MaterialAdmissionIssueCategory,
    MaterialWorkloadProfile,
    StagedMaterialAdmission,
    StagedMaterialRemoval,
    TurnMaterialContext,
)
from companion.materials.preflight import MaterialPreflightResult
from companion.observability.logging_config import get_backend_logger


logger = get_backend_logger(__name__)


class TurnMaterialContextService:
    """维护待提交材料和并发中的用户 turn 材料上下文。"""

    def __init__(self) -> None:
        """初始化纯内存材料状态。"""

        # 材料入口统一负责载荷归一化与领域对象构建。
        self._ingestion = MaterialIngestionService()

        # staged 区保存下一次 turn 尚未冻结的材料。
        self._staged_materials: list[MaterialAttachment] = []
        # active 区保存已经冻结并正在被各个 turn 消费的材料上下文。
        self._active_contexts: dict[str, TurnMaterialContext] = {}
        # retained 区只保存 turn 已结束但仍被异步任务读取的上下文，不再暴露到当前材料视图。
        self._retained_contexts: dict[str, TurnMaterialContext] = {}
        self._context_retention_counts: dict[str, int] = {}
        # 记录最近一次激活的上下文，便于默认读取当前 turn 视图。
        self._latest_context_id: str | None = None

        # 纯内存状态会被上传、预检、消费并发访问，因此需要可重入锁保护。
        self._state_lock = RLock()

        # generation 用于识别异步预检结果是否仍对应当前 staged 快照。
        self._preflight_generation = 0
        # staged 预检状态驱动前端和后续 turn 冻结时可见的材料可用性。
        self._staged_preflight_status = "closed"
        # staged 预检画像缓存本轮材料负载评估结果。
        self._staged_preflight_profile = None
        # staged 预检失败明细用于向上游暴露被剔除材料的原因。
        self._staged_preflight_failures: tuple[dict[str, str], ...] = ()

    async def append_staged(
        self,
        *,
        attachments: list[dict[str, Any]],
        created_at: str,
    ) -> StagedMaterialAdmission:
        """把用户上传的材料加入下一次 turn。

        Args:
            attachments: 外部提交的材料载荷。
            created_at: 服务端接收时间。

        Returns:
            包含真实加入材料与拒绝原因计数的批次结果。
        """

        with self._state_lock:
            existing_keys = {_dedupe_key(item.path) for item in self._staged_materials}

        # 先在批次内部做一次轻量去重，保留原始提交下标用于向前端精确回报问题。
        candidates: list[tuple[int, dict[str, Any]]] = []
        candidate_keys: list[tuple[str, str] | None] = []
        duplicate_count = 0
        issues: list[MaterialAdmissionIssue] = []
        observed_keys = set(existing_keys)
        for submitted_index, attachment in enumerate(attachments):
            key = _dedupe_key(attachment.get("path"))
            if key is not None and key in observed_keys:
                duplicate_count += 1
                issues.append(_admission_issue(submitted_index, attachment, "duplicate", "duplicate"))
                continue
            candidates.append((submitted_index, attachment))
            candidate_keys.append(key)
            if key is not None:
                observed_keys.add(key)

        added: list[MaterialAttachment] = []
        rejected: list[tuple[str, str, str]] = []
        unsupported_count = 0
        unreadable_count = 0

        # 入口归一化期间其他线程可能提交同一路径，因此写入前必须在短临界区内重新去重。
        with self._state_lock:
            committed_keys = {_dedupe_key(item.path) for item in self._staged_materials}
            for (submitted_index, attachment), key in zip(candidates, candidate_keys, strict=True):
                if key is not None and key in committed_keys:
                    duplicate_count += 1
                    issues.append(_admission_issue(submitted_index, attachment, "duplicate", "duplicate"))
                    continue
                item, rejection_reason = self._ingestion.ingest_staged_material(
                    attachment,
                    turn_id="staged",
                    created_at=created_at,
                    material_id=self._new_id("material"),
                )
                if item is None:
                    # 不支持的类型属于正常拒绝，其他失败通常意味着文件状态异常或不可读。
                    display_name = str(attachment.get("display_name") or "未命名材料").strip() or "未命名材料"
                    reason = rejection_reason or "unreadable_file"
                    rejected.append((display_name, reason, describe_material_failure(reason)))
                    if rejection_reason == "unsupported_extension":
                        unsupported_count += 1
                        issues.append(_admission_issue(submitted_index, attachment, "unsupported", reason))
                    else:
                        unreadable_count += 1
                        issues.append(_admission_issue(submitted_index, attachment, "unreadable", reason))
                    continue
                added.append(item)
                self._staged_materials.append(item)
                if key is not None:
                    committed_keys.add(key)
            if added:
                # staged 内容变化后，旧预检结论不再可信，必须递增 generation 让异步结果自动失效。
                self._preflight_generation += 1
                self._staged_preflight_status = "over_limit" if _known_top_level_file_count(self._staged_materials) > 5 else "pending"
                self._staged_preflight_profile = None
                self._staged_preflight_failures = ()

        for display_name, reason, detail in rejected:
            # 类型不支持是用户输入选择问题，读取失败才需要按异常路径提高日志级别。
            if reason == "unsupported_extension":
                logger.info(
                    "材料接纳拒绝 display_name=%s reason=%s detail=%s",
                    display_name,
                    reason,
                    detail,
                )
                continue
            logger.warning(
                "材料接纳读取失败 display_name=%s reason=%s detail=%s",
                display_name,
                reason,
                detail,
            )

        return StagedMaterialAdmission(
            submitted_count=len(attachments),
            added=added,
            unsupported_count=unsupported_count,
            unreadable_count=unreadable_count,
            duplicate_count=duplicate_count,
            issues=sorted(issues, key=lambda item: item.submitted_index),
        )

    def activate_turn(
        self,
        *,
        turn_id: str,
        created_at: str,
    ) -> TurnMaterialContext:
        """冻结 staged 和本次显式附件为新的活动 turn。

        Args:
            turn_id: 用户输入轮次编号。
            created_at: turn 创建时间。

        Returns:
            新创建的本轮材料上下文。
        """

        with self._state_lock:
            context_id = self._new_id("turn_material_context")
            frozen = list(self._staged_materials)
            self._staged_materials = []
            context = TurnMaterialContext(
                context_id=context_id,
                turn_id=turn_id,
                created_at=created_at,
                attachments=frozen,
                preflight_status=self._staged_preflight_status,
                preflight_profile=self._staged_preflight_profile,
                preflight_generation=self._preflight_generation,
                preflight_failures=self._staged_preflight_failures,
            )
            self._active_contexts[context_id] = context
            self._latest_context_id = context_id
            self._staged_preflight_status = "closed"
            self._staged_preflight_profile = None
            self._staged_preflight_failures = ()
            return context

    def staged_materials(self) -> list[MaterialAttachment]:
        """返回下一轮待提交材料的只读副本。

        Returns:
            staged 区当前材料的浅拷贝列表。
        """

        with self._state_lock:
            return list(self._staged_materials)

    def staged_preflight_snapshot(self) -> tuple[int, list[MaterialAttachment], str]:
        """返回异步预检需要的 generation、引用快照和当前状态。

        Returns:
            staged 代际、材料引用快照和预检状态。
        """

        with self._state_lock:
            return self._preflight_generation, list(self._staged_materials), self._staged_preflight_status

    def apply_preflight_result(
        self,
        generation: int,
        result: MaterialPreflightResult,
    ) -> tuple[list[str], list[dict[str, str]]] | None:
        """把仍然有效的异步结果提交到 staged 或 active 上下文。

        Args:
            generation: 预检启动时捕获的 staged 代际。
            result: 对应材料快照的预检结果。

        Returns:
            需要释放的资产编号与失败明细；结果已过期时返回空。
        """

        with self._state_lock:
            failure_ids = {item.material_id for item in result.failures}
            if generation == self._preflight_generation and self._staged_preflight_status == "pending":
                removed = [item for item in self._staged_materials if item.material_id in failure_ids]
                self._staged_materials = [item for item in self._staged_materials if item.material_id not in failure_ids]
                self._staged_preflight_status = result.status
                self._staged_preflight_profile = result.profile
                self._staged_preflight_failures = tuple(item.to_payload() for item in result.failures)
                return _managed_asset_ids(removed), list(self._staged_preflight_failures)
            for context_id, context in self._active_contexts.items():
                if context.preflight_generation != generation or context.preflight_status != "pending":
                    continue
                removed = [item for item in context.attachments if item.material_id in failure_ids]
                self._active_contexts[context_id] = TurnMaterialContext(
                    context_id=context.context_id,
                    turn_id=context.turn_id,
                    created_at=context.created_at,
                    attachments=[item for item in context.attachments if item.material_id not in failure_ids],
                    preflight_status=result.status,
                    preflight_profile=result.profile,
                    preflight_generation=context.preflight_generation,
                    preflight_failures=tuple(item.to_payload() for item in result.failures),
                )
                return _managed_asset_ids(removed), [item.to_payload() for item in result.failures]
            return None

    def freeze_active_context(
        self,
        context_id: str,
    ) -> tuple[TurnMaterialContext, MaterialWorkloadProfile] | None:
        """原子冻结 foreground 本轮使用的上下文与预检结论。

        Args:
            context_id: 需要冻结预检结论的活动材料上下文编号。

        Returns:
            同一次锁内读取的上下文与工作负载画像；上下文不存在时返回空。
        """

        with self._state_lock:
            context = self._active_contexts.get(context_id)
            if context is None:
                return None
            # 真正空 turn 没有需要预检的基础材料，closed 只表示生命周期结束，不是阻断原因。
            is_empty_turn = not context.attachments and not context.preflight_failures
            if is_empty_turn:
                profile = MaterialWorkloadProfile(
                    task_result_material_eligible=False,
                    local_file_count=0,
                    visual_unit_count=0,
                    text_byte_count=0,
                    local_total_bytes=0,
                    contains_mixed_material=False,
                    ignored_file_count=0,
                    blocking_reasons=(),
                )
            elif context.preflight_status == "ready":
                profile = context.preflight_profile
            else:
                profile = None
            resolved_profile = profile or _unavailable_profile(context.preflight_status)
            if context.preflight_failures:
                resolved_profile = replace(
                    resolved_profile,
                    task_result_material_eligible=False,
                    blocking_reasons=tuple(
                        dict.fromkeys(
                            (*resolved_profile.blocking_reasons, "preflight_failed")
                        )
                    ),
                )
            frozen_context = TurnMaterialContext(
                context_id=context.context_id,
                turn_id=context.turn_id,
                created_at=context.created_at,
                attachments=context.attachments,
                preflight_status="closed",
                preflight_profile=resolved_profile,
                preflight_generation=context.preflight_generation,
                preflight_failures=context.preflight_failures,
            )
            self._active_contexts[context_id] = frozen_context
            return frozen_context, resolved_profile

    def remove_staged(self, material_id: str) -> StagedMaterialRemoval | None:
        """删除指定 staged 材料并返回其受管资产编号。

        Args:
            material_id: 要删除的材料稳定编号。

        Returns:
            被删除材料及其受管资产编号；未命中时返回 None。
        """

        with self._state_lock:
            matched = next((item for item in self._staged_materials if item.material_id == material_id), None)
            if matched is None:
                remaining_count = len(self._staged_materials)
                released = None
            else:
                self._staged_materials = [item for item in self._staged_materials if item.material_id != material_id]
                self._preflight_generation += 1
                self._staged_preflight_status = (
                    "over_limit"
                    if _known_top_level_file_count(self._staged_materials) > 5
                    else "pending" if self._staged_materials else "closed"
                )
                self._staged_preflight_profile = None
                self._staged_preflight_failures = ()
                remaining_count = len(self._staged_materials)
                released = _managed_asset_ids([matched])

        if matched is None or released is None:
            logger.warning(
                "staged 材料删除失败 material_id=%s reason=not_found remaining_count=%s",
                material_id,
                remaining_count,
            )
            return None
        logger.info(
            "staged 材料删除成功 material_id=%s display_name=%s remaining_count=%s released_managed_asset_count=%s",
            material_id,
            matched.display_name,
            remaining_count,
            len(released),
        )
        return StagedMaterialRemoval(matched, released)

    def clear_staged(self) -> list[str]:
        """清空下一轮待提交材料并返回受管资产编号。

        Returns:
            清空后可以释放的受管资产编号。
        """

        with self._state_lock:
            released = _managed_asset_ids(self._staged_materials)
            self._staged_materials = []
            self._preflight_generation += 1
            self._staged_preflight_status = "closed"
            self._staged_preflight_profile = None
            self._staged_preflight_failures = ()
            return released

    def clear_all(self) -> list[str]:
        """清空所有 staged 和活动 turn 并返回受管资产编号。

        Returns:
            所有不再被上下文引用的受管资产编号。
        """

        with self._state_lock:
            attachments = list(self._staged_materials)
            for context_id, context in self._active_contexts.items():
                if self._context_retention_counts.get(context_id, 0) > 0:
                    self._retained_contexts[context_id] = context
                    continue
                attachments.extend(context.attachments)
            self._staged_materials = []
            self._preflight_generation += 1
            self._staged_preflight_status = "closed"
            self._staged_preflight_profile = None
            self._staged_preflight_failures = ()
            self._active_contexts = {}
            self._latest_context_id = None
            return _managed_asset_ids(attachments)

    def active_context(self, context_id: str | None = None) -> TurnMaterialContext | None:
        """读取指定或最新的活动 turn 材料上下文。

        Args:
            context_id: 可选上下文编号；为空时读取最新上下文。

        Returns:
            匹配的上下文；不存在时返回 None。
        """

        with self._state_lock:
            resolved_id = context_id or self._latest_context_id
            return self._active_contexts.get(resolved_id) if resolved_id is not None else None

    def take_active_materials(self, context_id: str) -> list[MaterialAttachment] | None:
        """从指定 turn 转出材料并保留待终态清理的空外壳。

        Args:
            context_id: route 明确认领的本轮材料上下文编号。

        Returns:
            被转出的材料；上下文不存在时返回 None。
        """

        with self._state_lock:
            context = self._active_contexts.get(context_id)
            if context is None:
                return None
            attachments = list(context.attachments)
            self._active_contexts[context_id] = TurnMaterialContext(
                context_id=context.context_id,
                turn_id=context.turn_id,
                created_at=context.created_at,
                attachments=[],
                preflight_status="closed",
                preflight_profile=context.preflight_profile,
                preflight_generation=context.preflight_generation,
                preflight_failures=context.preflight_failures,
            )
            return attachments

    def retain_context(self, context_id: str) -> bool:
        """为异步任务保留指定 turn 的材料所有权。

        Args:
            context_id: 异步任务准备读取的材料上下文编号。

        Returns:
            上下文仍存在且成功取得租约时返回真。
        """

        normalized_context_id = context_id.strip()
        if not normalized_context_id:
            return False
        with self._state_lock:
            if (
                normalized_context_id not in self._active_contexts
                and normalized_context_id not in self._retained_contexts
            ):
                return False
            self._context_retention_counts[normalized_context_id] = (
                self._context_retention_counts.get(normalized_context_id, 0) + 1
            )
            return True

    def release_context(self, context_id: str) -> list[str]:
        """释放异步任务持有的材料租约。

        Args:
            context_id: 异步任务已经读取完毕的材料上下文编号。

        Returns:
            最后一个所有者退出后可由 Desktop 删除的受管资产编号。
        """

        normalized_context_id = context_id.strip()
        if not normalized_context_id:
            return []
        with self._state_lock:
            count = self._context_retention_counts.get(normalized_context_id, 0)
            if count <= 0:
                return []
            if count > 1:
                self._context_retention_counts[normalized_context_id] = count - 1
                return []

            self._context_retention_counts.pop(normalized_context_id, None)
            context = self._retained_contexts.pop(normalized_context_id, None)
            if context is None:
                # turn 仍然活跃时，它继续拥有材料；稍后的 finish_turn 负责唯一释放。
                return []
            return _managed_asset_ids(context.attachments)

    def finish_turn(self, context_id: str) -> list[str]:
        """结束指定 turn 并返回可以释放的受管资产编号。

        Args:
            context_id: `assistant.text_final` 对应的材料上下文编号。

        Returns:
            仍由该 turn 持有的受管资产编号。
        """

        with self._state_lock:
            context = self._active_contexts.pop(context_id, None)
            if context is None:
                return []
            if self._latest_context_id == context_id:
                self._latest_context_id = next(reversed(self._active_contexts), None)
            if self._context_retention_counts.get(context_id, 0) > 0:
                self._retained_contexts[context_id] = context
                return []
            # 没人用了，返回这次 turn 所有附件的 managed_asset_id。
            return _managed_asset_ids(context.attachments)

    def current_payload(self) -> dict[str, object]:
        """返回两个阶段组成的本轮材料公开视图。

        Returns:
            包含 staged 与当前 active 上下文的公开载荷。
        """

        with self._state_lock:
            active = self._active_contexts.get(self._latest_context_id) if self._latest_context_id is not None else None
            return {
                "turn_material_context": {
                    "staged_materials": [item.to_payload() for item in self._staged_materials],
                    "active": active.to_payload() if active is not None else None,
                }
            }

    def _new_id(self, prefix: str) -> str:
        """生成带业务前缀的运行期编号。

        Args:
            prefix: 用于区分编号业务类型的前缀。

        Returns:
            由业务前缀和随机标识组成的运行期编号。
        """

        return f"{prefix}_{uuid.uuid4().hex}"


def _dedupe_key(path: object) -> tuple[str, str] | None:
    """生成材料在单个阶段内使用的去重键。

    Args:
        path: 材料来源路径或 URL。

    Returns:
        来源类型和规范化值组成的去重键；路径为空时返回空。
    """

    if isinstance(path, str) and path.strip():
        return "path", path.strip().casefold()
    return None


def _admission_issue(
    submitted_index: int,
    attachment: dict[str, Any],
    category: MaterialAdmissionIssueCategory,
    reason: str,
) -> MaterialAdmissionIssue:
    """生成不包含本地路径的接纳问题明细。

    Args:
        submitted_index: 材料在本次提交批次中的下标。
        attachment: 被拒绝或跳过的材料对象。
        category: 面向上游的接纳问题类别。
        reason: 不包含敏感路径的用户可见原因。

    Returns:
        可安全反馈给前端的材料接纳问题。
    """

    display_name = str(attachment.get("display_name") or "未命名材料").strip() or "未命名材料"
    return MaterialAdmissionIssue(
        submitted_index=submitted_index,
        display_name=display_name,
        category=category,
        reason=reason,
    )


def _managed_asset_ids(attachments: list[MaterialAttachment]) -> list[str]:
    """提取由项目创建且可以释放的受管资产编号。

    Args:
        attachments: 需要检查所有权的材料列表。

    Returns:
        去重后的受管资产编号列表。
    """

    return [item.managed_asset_id for item in attachments if item.managed_asset_id is not None]


def _known_top_level_file_count(attachments: list[MaterialAttachment]) -> int:
    """统计无需递归即可确认的顶层本地文件数量。

    Args:
        attachments: 需要统计的材料列表。

    Returns:
        顶层本地文件及已知目录文件数之和。
    """

    return sum(item.kind != "dragged_folder" and item.path is not None for item in attachments)


def _unavailable_profile(reason: str) -> MaterialWorkloadProfile:
    """构造不允许后台工作读取材料的内部画像。

    Args:
        reason: 当前材料不可由后台工作读取的内部原因。

    Returns:
        禁止后台工作读取材料的工作负载画像。
    """

    return MaterialWorkloadProfile(
        task_result_material_eligible=False,
        local_file_count=0,
        visual_unit_count=0,
        text_byte_count=0,
        local_total_bytes=0,
        contains_mixed_material=False,
        ignored_file_count=0,
        blocking_reasons=(reason,),
    )
