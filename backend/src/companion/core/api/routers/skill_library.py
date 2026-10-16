"""提供 NearMate 受管 Skill 的 Console 接口。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt

from companion.background_agent.skill_asset_safety import SkillAssetBoundaryError
from companion.background_agent.skill_registry import (
    SkillNotFoundError,
    SkillRevisionConflictError,
)
from companion.core.api.dependencies import SkillLibraryDependency
from companion.core.user_settings_store import MAX_SAFE_SETTINGS_REVISION


router = APIRouter(prefix="/api/console/skills", tags=["skill-library"])


class SkillEnablementRequest(BaseModel):
    """描述用户对一项受管 Skill 的启停修改。

    Args:
        enabled: 是否让这项 Skill 参与之后创建的新后台工作。
        expected_revision: Console 读取列表时看到的 Skill 设置版本。
    """

    model_config = ConfigDict(extra="forbid")

    enabled: StrictBool
    expected_revision: StrictInt = Field(ge=0, le=MAX_SAFE_SETTINGS_REVISION)


@router.get("")
async def list_managed_skills(
    container: SkillLibraryDependency,
) -> dict[str, object]:
    """返回全部受管 Skill 及其短 TTL 依赖快照。

    Args:
        container: 技能库 API 的窄化依赖切面。

    Returns:
        Skill 列表、汇总数字、清单问题和乐观锁版本。
    """

    return await container.user_settings.current_skill_library_payload()


@router.get("/{skill_id}")
async def get_managed_skill(
    skill_id: str,
    container: SkillLibraryDependency,
) -> dict[str, object]:
    """返回一项受管 Skill 的安全详情。

    Args:
        skill_id: Skill 清单声明的稳定编号。
        container: 技能库 API 的窄化依赖切面。

    Returns:
        展示元数据、依赖状态、相对文件列表和纯文本说明。

    Raises:
        HTTPException: Skill 不存在时返回结构化 404。
    """

    try:
        return await container.user_settings.current_skill_detail_payload(
            skill_id
        )
    except SkillAssetBoundaryError as exception:
        raise _skill_http_error(
            status_code=409,
            code="skill_asset_changed",
            message="Skill 资产已变化，请刷新后重试",
        ) from exception
    except SkillNotFoundError as exception:
        raise _skill_http_error(
            status_code=404,
            code="skill_not_found",
            message=str(exception),
        ) from exception


@router.patch("/{skill_id}", status_code=202)
async def update_managed_skill(
    skill_id: str,
    body: SkillEnablementRequest,
    container: SkillLibraryDependency,
) -> dict[str, object]:
    """保存一项受管 Skill 的全局启停偏好。

    Args:
        skill_id: Skill 清单声明的稳定编号。
        body: 新开关值和 Console 已知的设置版本。
        container: 技能库 API 的窄化依赖切面。

    Returns:
        更新后的 Skill 状态和新版本；运行中任务不会被改变。

    Raises:
        HTTPException: Skill 不存在或版本已经变化时返回结构化错误。
    """

    try:
        return await container.user_settings.update_skill_enabled(
            skill_id=skill_id,
            enabled=body.enabled,
            expected_revision=body.expected_revision,
        )
    except SkillNotFoundError as exception:
        raise _skill_http_error(
            status_code=404,
            code="skill_not_found",
            message=str(exception),
        ) from exception
    except SkillRevisionConflictError as exception:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "skill_revision_conflict",
                "message": str(exception),
                "actual_revision": exception.actual_revision,
            },
        ) from exception


def _skill_http_error(*, status_code: int, code: str, message: str) -> HTTPException:
    """构造技能库统一的结构化 HTTP 错误。

    Args:
        status_code: 对应的 HTTP 状态码。
        code: Console 可以稳定判断的错误编号。
        message: 面向用户展示的中文原因。

    Returns:
        可直接抛出的 FastAPI HTTP 异常。
    """

    return HTTPException(
        status_code=status_code,
        detail={"code": code, "message": message},
    )
