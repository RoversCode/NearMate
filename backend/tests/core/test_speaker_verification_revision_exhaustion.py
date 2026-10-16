"""验证说话人设置入口保留统一的 settings revision 冲突语义。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, NoReturn, cast

import pytest

from companion.core.api.routers.speaker_verification import (
    create_speaker_verification_sample,
    delete_speaker_verification_sample,
    update_speaker_verification_profile,
)
from companion.core.api.schemas import SpeakerVerificationProfileRequest
from companion.core.application.errors import StateConflict
from companion.core.settings import Settings
from companion.core.user_settings_store import MAX_SAFE_SETTINGS_REVISION


class RevisionExhaustedCoordinator:
    """模拟在任何领域准备动作前拒绝提交的设置协调器。"""

    async def commit_user_settings(self, **kwargs: object) -> NoReturn:
        """始终抛出稳定的版本耗尽冲突。

        Args:
            **kwargs: 路由交给真实协调器的准备与回滚回调。

        Raises:
            StateConflict: 当前设置版本已经达到安全整数上限。
        """

        _ = kwargs
        raise StateConflict(
            {
                "code": "settings_revision_exhausted",
                "actual_revision": MAX_SAFE_SETTINGS_REVISION,
            }
        )


def _revision_exhausted_container() -> Any:
    """构造只会在设置提交边界被访问的最小路由容器。

    Returns:
        带版本耗尽协调器的测试容器。
    """

    return cast(
        Any,
        SimpleNamespace(user_settings=RevisionExhaustedCoordinator()),
    )


def _assert_revision_exhausted(conflict: StateConflict) -> None:
    """断言路由没有把统一冲突包装成 HTTP 500。

    Args:
        conflict: 从说话人设置路由原样传播出的应用层冲突。

    Returns:
        无返回值。
    """

    assert conflict.detail == {
        "code": "settings_revision_exhausted",
        "actual_revision": MAX_SAFE_SETTINGS_REVISION,
    }


@pytest.mark.asyncio
async def test_profile_update_propagates_revision_exhaustion() -> None:
    """修改说话人开关时，版本耗尽必须交给全局 409 handler。

    Returns:
        无返回值。
    """

    with pytest.raises(StateConflict) as conflict:
        await update_speaker_verification_profile(
            SpeakerVerificationProfileRequest(enabled=False),
            _revision_exhausted_container(),
        )

    _assert_revision_exhausted(conflict.value)


@pytest.mark.asyncio
async def test_sample_create_propagates_revision_exhaustion() -> None:
    """保存说话人样本时，版本耗尽必须交给全局 409 handler。

    Returns:
        无返回值。
    """

    with pytest.raises(StateConflict) as conflict:
        await create_speaker_verification_sample(
            _revision_exhausted_container(),
            recording_id="recording-a",
            replace_sample_id=None,
        )

    _assert_revision_exhausted(conflict.value)


@pytest.mark.asyncio
async def test_sample_delete_propagates_revision_exhaustion() -> None:
    """删除说话人样本时，版本耗尽必须交给全局 409 handler。

    Returns:
        无返回值。
    """

    with pytest.raises(StateConflict) as conflict:
        await delete_speaker_verification_sample(
            "sample-a",
            _revision_exhausted_container(),
        )

    _assert_revision_exhausted(conflict.value)


@pytest.mark.asyncio
async def test_profile_response_omits_optional_settings_when_legacy_coordinator_returns_none() -> None:
    """旧组合根未返回设置快照时，profile 响应不能携带非法 null 字段。"""

    class LegacyCoordinator:
        async def commit_user_settings(self, **kwargs: object) -> None:
            prepare_update = kwargs["prepare_update"]
            assert callable(prepare_update)
            prepare_update(Settings())
            return None

    container = cast(
        Any,
        SimpleNamespace(
            user_settings=LegacyCoordinator(),
            speaker_verification=SimpleNamespace(
                current_whitelist_audio_paths=lambda: [],
                prepare_settings_patch=lambda _settings, *, enabled: {},
                current_profile=lambda: {
                    "enabled": False,
                    "samples": [],
                    "max_sample_count": 3,
                    "required_speech_seconds": 2,
                    "can_enable": False,
                },
            ),
        ),
    )

    response = await update_speaker_verification_profile(
        SpeakerVerificationProfileRequest(enabled=False),
        container,
    )

    assert "settings" not in response
