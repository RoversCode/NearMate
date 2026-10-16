from __future__ import annotations

import asyncio
import time
import uuid

from fastapi import APIRouter, HTTPException

from companion.core.api.dependencies import SettingsDependency
from companion.core.api.schemas import (
    AudioDeviceApplyResultRequest,
    AudioDeviceSettingsRequest,
    AudioDeviceTestRequest,
    SettingsRequest,
    ShortcutStatusReportRequest,
    ToolWaitingFamilyPolicyRequest,
    ToolWaitingToolPolicyRequest,
)
from companion.core.application.general_settings import GeneralSettingsUpdate
from companion.core.audio_devices import (
    AudioDeviceProbeService,
    audio_device_selection_payload_from_snapshot,
)
from companion.core.settings import Settings
from companion.core.shortcut_status import ShortcutStatusItem
from companion.observability.logging_config import get_backend_logger


logger = get_backend_logger(__name__)
router = APIRouter()


def trim_optional_string(value: str | None) -> str | None:
    """裁剪外部请求里的可选字符串。

    Args:
        value: 可选原始字符串。

    Returns:
        裁剪后的值；空白或 None 返回 None。
    """

    if value is None:
        return None
    normalized_value = value.strip()
    return normalized_value or None


@router.get("/api/settings")
async def get_settings(container: SettingsDependency) -> dict:
    """读取本地控制台设置。

    Returns:
        普通设置页可编辑快照；当前角色名必须从角色卡接口读取，避免普通设置成为第二个人格来源。
    """

    return await container.user_settings.current_settings_payload()


@router.get("/api/settings/shortcut-status")
async def get_shortcut_status(container: SettingsDependency) -> dict[str, object]:
    """读取桌面全局快捷键的当前注册状态。

    Args:
        container: 当前 Backend 的设置领域依赖。

    Returns:
        与已提交设置 revision 对齐的快捷键状态。
    """

    return await container.user_settings.current_shortcut_status_payload()


@router.get("/api/tool-waiting-policies")
async def get_tool_waiting_policies(container: SettingsDependency) -> dict[str, object]:
    """读取工具等待反馈策略和工具目录。"""

    return container.hub.current_tool_waiting_policies_payload()


@router.put("/api/tool-waiting-policies/families/{family_id}", status_code=202)
async def update_tool_waiting_family_policy(
    family_id: str,
    payload: ToolWaitingFamilyPolicyRequest,
    container: SettingsDependency,
) -> dict[str, object]:
    """保存 family 级工具等待反馈策略。"""

    policy_view = container.hub.current_tool_waiting_policies_payload()
    configurable_family_ids = {
        str(family.get("family_id") or "").strip()
        for family in policy_view.get("families", [])
        if isinstance(family, dict)
    }
    if family_id.strip() not in configurable_family_ids:
        raise HTTPException(status_code=404, detail="工具族不支持固定等待反馈")

    try:
        container.hub.tool_waiting_policy_store.save_family_policy(
            family_id,
            enabled=payload.enabled,
            phrases=payload.phrases,
        )
    except ValueError as exception:
        raise HTTPException(status_code=400, detail=str(exception)) from exception
    return container.hub.current_tool_waiting_policies_payload()


@router.put("/api/tool-waiting-policies/tools/{tool_name}", status_code=202)
async def update_tool_waiting_tool_policy(
    tool_name: str,
    payload: ToolWaitingToolPolicyRequest,
    container: SettingsDependency,
) -> dict[str, object]:
    """保存或清除单工具等待反馈 override。"""

    policy_view = container.hub.current_tool_waiting_policies_payload()
    configurable_tool_names = {
        str(tool.get("name") or "").strip()
        for family in policy_view.get("families", [])
        if isinstance(family, dict)
        for tool in family.get("tools", [])
        if isinstance(tool, dict)
    }
    if tool_name.strip() not in configurable_tool_names:
        raise HTTPException(status_code=404, detail="工具不支持固定等待反馈")

    try:
        if payload.inherit_family:
            container.hub.tool_waiting_policy_store.clear_tool_override(tool_name)
        else:
            container.hub.tool_waiting_policy_store.save_tool_override(
                tool_name,
                enabled=payload.enabled,
                phrases=payload.phrases,
            )
    except ValueError as exception:
        raise HTTPException(status_code=400, detail=str(exception)) from exception
    return container.hub.current_tool_waiting_policies_payload()


@router.get("/api/audio/devices")
async def get_audio_devices(container: SettingsDependency) -> dict[str, object]:
    """读取本机音频设备与探测状态。

    Args:
        container: 当前 Backend 的设置领域依赖。

    Returns:
        本机设备列表、探测状态和已提交的用户选择。

    Raises:
        HTTPException: 本机音频设备枚举失败时抛出。
    """

    try:
        selection = (
            await container.user_settings.current_audio_device_selection_payload()
        )
        # 设备打开和 RDP 预缓冲会等待数秒；放到工作线程，避免饿死同一事件循环中的实时麦克风消费。
        return await asyncio.to_thread(lambda: AudioDeviceProbeService().inspect_devices(selection=selection))
    except Exception as exception:
        logger.exception("音频设备枚举失败")
        raise HTTPException(status_code=500, detail=str(exception)) from exception


@router.post("/api/audio/devices/test")
async def test_audio_device(
    payload: AudioDeviceTestRequest,
) -> dict[str, object]:
    """录制指定麦克风，返回三秒人声测试回放。

    Args:
        payload: 用户选择的麦克风身份。

    Returns:
        采集状态、音量统计和可回放的 WAV 数据。
    """

    try:
        # 录音等待不能占住控制面事件循环，否则测试期间媒体队列会积压，正常监听也会失去实时性。
        return await asyncio.to_thread(
            lambda: AudioDeviceProbeService().test_microphone(
                microphone_id=payload.microphone_id,
                microphone_name=payload.microphone_name,
            )
        )
    except Exception as exception:
        logger.exception("音频设备测试失败")
        raise HTTPException(status_code=500, detail=str(exception)) from exception


@router.post("/api/settings/audio-devices", status_code=202)
async def update_audio_device_settings(
    payload: AudioDeviceSettingsRequest,
    container: SettingsDependency,
) -> dict[str, object]:
    """保存 Console 中选择的音频设备。"""

    request_id = f"audio-device-{uuid.uuid4()}"

    def prepare_audio_device_update(
        _current_settings: Settings,
    ) -> dict[str, object]:
        """构造音频设备接口拥有的字段 patch。

        Args:
            _current_settings: 拿到统一提交锁后的最新设置；静态请求值无需读取它。

        Returns:
            已裁剪的音频设备选择字段。
        """

        return {
            "audio_device_mode": payload.mode,
            "audio_input_device_id": trim_optional_string(payload.microphone_id),
            "audio_input_device_name": trim_optional_string(payload.microphone_name),
            "audio_output_reference_device_id": trim_optional_string(
                payload.speaker_loopback_id
            ),
            "audio_output_reference_device_name": trim_optional_string(
                payload.speaker_loopback_name
            ),
        }

    async def publish_audio_device_change(
        committed_snapshot: dict[str, object],
    ) -> None:
        """按设置提交顺序通知 Desktop 应用音频设备。

        Args:
            committed_snapshot: 已持久化且带新 revision 的完整设置快照。

        Returns:
            无返回值。
        """

        await container.hub.publisher("audio_devices").publish(
            {
                "type": "audio.devices_change_requested",
                "payload": {
                    "request_id": request_id,
                    "selection": audio_device_selection_payload_from_snapshot(
                        committed_snapshot
                    ),
                    "requested_at": time.time(),
                },
            }
        )

    committed_snapshot = await container.user_settings.commit_user_settings(
        prepare_update=prepare_audio_device_update,
        after_commit=publish_audio_device_change,
    )
    # 音频设备和快捷键共享同一 settings_revision。把提交时冻结的完整快照
    # 一并返回，Console 才能在设备保存后继续携带最新版本提交其它设置，
    # 不会把旧页面快照重新写回去并制造假 409。
    response_payload: dict[str, object] = {
        "accepted": True,
        "request_id": request_id,
    }
    if committed_snapshot is not None:
        response_payload["settings"] = committed_snapshot
    return response_payload


@router.post("/api/audio/devices/apply-result", status_code=202)
async def report_audio_device_apply_result(
    payload: AudioDeviceApplyResultRequest,
    container: SettingsDependency,
) -> dict[str, object]:
    """接收 Desktop 热更新音频设备的结果并广播给 Console。

    Args:
        payload: Desktop 实际应用设备后的结果。
        container: 当前 Backend 的设置领域依赖。

    Returns:
        表示上报已被接纳的固定响应。
    """

    selection = await container.user_settings.current_audio_device_selection_payload()

    await container.hub.publisher("audio_devices").publish(
        {
            "type": "audio.devices_apply_result",
            "payload": {
                "request_id": payload.request_id,
                "status": payload.status,
                "mode": selection["mode"],
                "reason": payload.reason,
                "applied_at": time.time(),
            },
        }
    )
    return {"accepted": True}


@router.post("/api/desktop/shortcut-status", status_code=202)
async def report_desktop_shortcut_status(
    payload: ShortcutStatusReportRequest,
    container: SettingsDependency,
) -> dict[str, object]:
    """接收 Desktop main 上报的全局快捷键注册结果。

    Args:
        payload: Desktop 针对某个设置 revision 的快捷键检查结果。
        container: 当前 Backend 的设置领域依赖。

    Returns:
        表示上报已保存并完成广播的固定响应。
    """

    await container.user_settings.report_shortcut_status(
        items=[
            ShortcutStatusItem(
                shortcut_id=item.shortcut_id,
                accelerator=item.accelerator,
                enabled=item.enabled,
                status=item.status,
                settings_revision=item.settings_revision,
                checked_at=item.checked_at,
                message=item.message,
            )
            for item in payload.items
        ],
        settings_revision=payload.settings_revision,
    )
    return {"accepted": True}


@router.post("/api/settings", status_code=202)
async def update_settings(
    payload: SettingsRequest,
    container: SettingsDependency,
) -> dict:
    """按乐观锁版本保存普通设置页拥有的字段。

    Args:
        payload: 已校验的普通设置 patch 和客户端已知版本。
        container: 当前 Backend 组合根依赖。

    Returns:
        提交后的完整权威设置快照。
    """

    return await container.user_settings.update_general_settings(
        update=GeneralSettingsUpdate(
            show_speech_bubbles=payload.show_speech_bubbles,
            followup_toggle_shortcut_enabled=(
                payload.followup_toggle_shortcut_enabled
            ),
            followup_toggle_shortcut=payload.followup_toggle_shortcut,
            avatar_visibility_shortcut_enabled=(
                payload.avatar_visibility_shortcut_enabled
            ),
            avatar_visibility_shortcut=payload.avatar_visibility_shortcut,
            selection_capture_shortcut_enabled=(
                payload.selection_capture_shortcut_enabled
            ),
            selection_capture_shortcut=payload.selection_capture_shortcut,
            selected_live2d_model_id=payload.selected_live2d_model_id,
            user_self_intro=payload.user_self_intro,
            user_extra_notes=payload.user_extra_notes,
        ),
        expected_revision=payload.expected_revision,
    )
