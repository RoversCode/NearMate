from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from companion.core.settings import Settings
from companion.background_agent.settings import (
    BackgroundAgentSettings,
    background_agent_settings_payload_needs_migration,
    load_background_agent_settings_payload,
    migrate_background_agent_settings_payload,
)
from companion.observability.logging_config import get_backend_logger
from companion.records.corrupt_file_quarantine import quarantine_corrupt_file


logger = get_backend_logger(__name__)


USER_SETTINGS_FIELDS = (
    "show_speech_bubbles",
    "followup_toggle_shortcut_enabled",
    "followup_toggle_shortcut",
    "avatar_visibility_shortcut_enabled",
    "avatar_visibility_shortcut",
    "selection_capture_shortcut_enabled",
    "selection_capture_shortcut",
    "selected_live2d_model_id",
    "user_self_intro",
    "user_extra_notes",
    "streamvox_device",
    "streamvox_llama_backend",
    "speaker_verification_enabled",
    "speaker_whitelist_audio_paths",
    "audio_input_device_id",
    "audio_input_device_name",
    "audio_output_reference_device_id",
    "audio_output_reference_device_name",
    "audio_device_mode",
    "background_agent",
)
SETTINGS_REVISION_FIELD = "settings_revision"
MAX_SAFE_SETTINGS_REVISION = 2**53 - 1

BOOLEAN_USER_SETTINGS_FIELDS = {
    "show_speech_bubbles",
    "followup_toggle_shortcut_enabled",
    "avatar_visibility_shortcut_enabled",
    "selection_capture_shortcut_enabled",
    "speaker_verification_enabled",
}

LIST_USER_SETTINGS_FIELDS = {
    "speaker_whitelist_audio_paths",
}

NULLABLE_USER_SETTINGS_FIELDS = {
    "streamvox_llama_backend",
    "audio_input_device_id",
    "audio_input_device_name",
    "audio_output_reference_device_id",
    "audio_output_reference_device_name",
}

REQUIRED_STRING_USER_SETTINGS_FIELDS = {
    "followup_toggle_shortcut",
    "avatar_visibility_shortcut",
    "selection_capture_shortcut",
}


class UserSettingsStore:
    """本地用户设置文件仓储。"""

    def __init__(self, path: str | Path) -> None:
        """创建本地用户设置仓储。"""

        # 用户设置文件路径由 Settings 统一配置，保存为 Path 便于跨平台创建父目录和临时文件。
        self.path = Path(path)

    def load(self, settings: Settings) -> dict[str, Any] | None:
        """读取本地用户设置快照；文件损坏时备份留证并按默认设置回落。"""

        # 本地文件不存在表示用户从未保存过偏好，调用方可以继续使用 shared 默认或执行旧数据库迁移。
        if not self.path.exists():
            return None

        try:
            raw_payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeError) as exception:
            # 只有内容本身已经确认损坏时才隔离文件；普通磁盘权限、共享冲突
            # 等 OSError 必须继续向上抛出，避免把合法配置误判成坏文件。
            backup_path = quarantine_corrupt_file(self.path)
            logger.warning(
                "用户设置文件无法解析，已备份为 %s 并按默认设置启动：%s",
                backup_path,
                exception,
            )
            return None

        # 用户设置文件的根节点必须是对象，否则无法安全按字段合并到 Settings。
        if not isinstance(raw_payload, dict):
            backup_path = quarantine_corrupt_file(self.path)
            logger.warning(
                "用户设置文件根节点不是 JSON 对象，已备份为 %s 并按默认设置启动",
                backup_path,
            )
            return None

        raw_background_agent = raw_payload.get("background_agent")
        validated_migration: BackgroundAgentSettings | None = None
        if background_agent_settings_payload_needs_migration(raw_background_agent):
            try:
                validated_migration = BackgroundAgentSettings.model_validate(
                    migrate_background_agent_settings_payload(raw_background_agent)
                )
            except ValueError:
                # 迁移失败时原文件是唯一可恢复来源，不能把整段配置的兜底值覆盖回磁盘。
                logger.warning("后台工作旧配置包含非法字段，已保留原文件并按字段恢复")

        normalized_payload = normalize_user_settings_payload(raw_payload, settings)
        raw_settings_revision = raw_payload.get(SETTINGS_REVISION_FIELD)
        settings_revision_is_valid = is_valid_settings_revision(raw_settings_revision)
        normalized_payload[SETTINGS_REVISION_FIELD] = normalize_settings_revision(
            raw_settings_revision
        )

        # 旧文件没有 revision 时也要落盘修复；否则每次启动都会重新从零建轴并重复打印诊断。
        # 回写只改元数据和已成功迁移的后台配置，原始用户字段与未知字段仍原样保留。
        if validated_migration is not None or not settings_revision_is_valid:
            repaired_payload = dict(raw_payload)
            if not settings_revision_is_valid:
                repaired_payload[SETTINGS_REVISION_FIELD] = normalized_payload[
                    SETTINGS_REVISION_FIELD
                ]
            if validated_migration is not None:
                repaired_payload["background_agent"] = validated_migration.model_dump(
                    mode="json"
                )
            try:
                self._atomic_write(repaired_payload)
            except OSError:
                # 原子替换失败不会影响本次已经恢复的内存快照，下次启动会再次尝试修复。
                logger.exception("原子回写用户设置迁移结果失败")
        return normalized_payload

    def save(self, snapshot: dict[str, Any]) -> None:
        """原子写入本地用户设置快照。"""

        # 用户设置属于本机运行数据，首次保存时需要创建 backend/data 目录。
        self.path.parent.mkdir(parents=True, exist_ok=True)

        # revision 是乐观锁的持久化元数据；其它字段仍只允许公开可编辑集合，
        # 避免误把路径、密钥或内部运行配置写进用户偏好文件。
        settings_revision = snapshot.get(SETTINGS_REVISION_FIELD)
        if not is_valid_settings_revision(settings_revision):
            raise ValueError("用户设置快照缺少合法 settings_revision")
        persisted_snapshot = {
            SETTINGS_REVISION_FIELD: settings_revision,
            **{
                field_name: snapshot[field_name]
                for field_name in USER_SETTINGS_FIELDS
            },
        }
        self._atomic_write(persisted_snapshot)

    def _atomic_write(self, payload: dict[str, Any]) -> None:
        """把一个完整 JSON 对象原子替换到用户设置路径。

        Args:
            payload: 需要完整写入磁盘的用户设置对象。
        """

        self.path.parent.mkdir(parents=True, exist_ok=True)
        serialized_snapshot = json.dumps(payload, ensure_ascii=False, indent=2)

        # 临时文件放在同一目录，os.replace 在同一文件系统内可原子替换，避免进程中断留下半截 JSON。
        temporary_path = self.path.with_name(f"{self.path.name}.tmp")
        try:
            temporary_path.write_text(f"{serialized_snapshot}\n", encoding="utf-8")
            os.replace(temporary_path, self.path)
        finally:
            temporary_path.unlink(missing_ok=True)


def normalize_user_settings_payload(payload: dict[str, Any], settings: Settings) -> dict[str, Any]:
    """把外部设置载荷归一成完整用户设置快照。"""

    normalized_payload: dict[str, Any] = {}
    for field_name in USER_SETTINGS_FIELDS:
        # 当前 Settings 值是每个字段的兜底来源，确保 shared 默认和 env.yaml 仍能参与缺省决策。
        fallback_value = getattr(settings, field_name)
        candidate_value = payload.get(field_name, fallback_value)

        # 布尔开关只接受真实 boolean，避免字符串 "false" 被 Python bool 转成 True。
        if field_name in BOOLEAN_USER_SETTINGS_FIELDS:
            normalized_payload[field_name] = candidate_value if isinstance(candidate_value, bool) else fallback_value
            continue

        if field_name == "background_agent":
            normalized_payload[field_name] = load_background_agent_settings_payload(
                candidate_value,
                fallback_value,
            )
            continue

        if field_name in LIST_USER_SETTINGS_FIELDS:
            normalized_payload[field_name] = _normalize_string_list(candidate_value, fallback_value)
            continue

        if field_name in NULLABLE_USER_SETTINGS_FIELDS:
            if field_name.startswith("audio_"):
                normalized_payload[field_name] = _normalize_nullable_string(candidate_value, fallback_value)
                continue
            normalized_payload[field_name] = _normalize_nullable_streamvox_llama_backend(
                candidate_value,
                fallback_value,
            )
            continue

        if field_name == "audio_device_mode":
            normalized_payload[field_name] = _normalize_audio_device_mode(candidate_value, fallback_value)
            continue

        # 字符串字段只接受真实字符串；类型错误时回到 Settings 当前值，避免把数字或对象灌入前端契约。
        if not isinstance(candidate_value, str):
            normalized_payload[field_name] = fallback_value
            continue

        normalized_value = candidate_value.strip()
        if field_name in REQUIRED_STRING_USER_SETTINGS_FIELDS and not normalized_value:
            normalized_payload[field_name] = fallback_value
            continue

        normalized_payload[field_name] = normalized_value

    return normalized_payload


def is_valid_settings_revision(value: Any) -> bool:
    """判断值是否是可以跨 Backend、Console 和 Desktop 无损传递的版本号。

    Args:
        value: 用户设置文件或设置事务中的候选版本号。

    Returns:
        候选值是非负 JavaScript 安全整数时返回 True，否则返回 False。
    """

    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and 0 <= value <= MAX_SAFE_SETTINGS_REVISION
    )


def normalize_settings_revision(value: Any) -> int:
    """把本地版本元数据收敛成浏览器可以无损表示的非负整数。

    Args:
        value: 用户设置文件中的原始 settings_revision。

    Returns:
        合法安全整数；旧文件缺失或内容损坏时返回零。
    """

    if not is_valid_settings_revision(value):
        logger.warning("用户设置 settings_revision 非法，已从零重新建立版本轴")
        return 0
    return value


def _normalize_string_list(candidate_value: Any, fallback_value: Any) -> list[str]:
    """归一化控制台提交的字符串列表。

    Args:
        candidate_value: 外部载荷中的候选值。
        fallback_value: 当前 Settings 中的兜底值。

    Returns:
        已裁剪、去空、去重的字符串列表；候选值不可用时返回兜底列表。
    """

    fallback_keywords = [item.strip() for item in fallback_value if isinstance(item, str) and item.strip()]
    if not isinstance(candidate_value, list):
        return fallback_keywords

    normalized_values: list[str] = []
    seen_values: set[str] = set()
    for item in candidate_value:
        if not isinstance(item, str):
            continue
        normalized_item = item.strip()
        if not normalized_item or normalized_item in seen_values:
            continue
        normalized_values.append(normalized_item)
        seen_values.add(normalized_item)

    return normalized_values or fallback_keywords


def _normalize_nullable_streamvox_llama_backend(candidate_value: Any, fallback_value: Any) -> str | None:
    """归一化可为空的 StreamVox llama.cpp 后端配置。

    Args:
        candidate_value: 用户设置文件中的候选值。
        fallback_value: Settings 当前兜底值。

    Returns:
        None、cuda 或 vulkan；旧配置中的 auto 会回到兜底值。
    """

    fallback_backend = fallback_value if fallback_value in {"cuda", "vulkan", None} else None
    if candidate_value is None:
        return None
    if not isinstance(candidate_value, str):
        return fallback_backend

    normalized_value = candidate_value.strip().lower()
    if not normalized_value:
        return None
    if normalized_value in {"cuda", "vulkan"}:
        return normalized_value
    return fallback_backend


def _normalize_nullable_string(candidate_value: Any, fallback_value: Any) -> str | None:
    """归一化可为空字符串字段。

    Args:
        candidate_value: 外部载荷中的候选值。
        fallback_value: 当前 Settings 中的兜底值。

    Returns:
        裁剪后的字符串或 None；类型错误时返回兜底值。
    """

    fallback_string = fallback_value if isinstance(fallback_value, str) and fallback_value.strip() else None
    if candidate_value is None:
        return None
    if not isinstance(candidate_value, str):
        return fallback_string
    normalized_value = candidate_value.strip()
    return normalized_value or None


def _normalize_audio_device_mode(candidate_value: Any, fallback_value: Any) -> str:
    """归一化音频设备选择模式。

    Args:
        candidate_value: 外部载荷中的候选值。
        fallback_value: 当前 Settings 中的兜底值。

    Returns:
        system_default 或 manual。
    """

    fallback_mode = fallback_value if fallback_value in {"system_default", "manual"} else "system_default"
    if not isinstance(candidate_value, str):
        return fallback_mode
    normalized_value = candidate_value.strip()
    if normalized_value in {"system_default", "manual"}:
        return normalized_value
    return fallback_mode


def apply_user_settings_snapshot(settings: Settings, snapshot: dict[str, Any]) -> None:
    """把用户设置快照回填到运行时 Settings。"""

    for field_name in USER_SETTINGS_FIELDS:
        # 这里只写控制台可编辑字段，业务意图是避免用户偏好文件反向污染路径、密钥和内部运行参数。
        setattr(settings, field_name, snapshot[field_name])
