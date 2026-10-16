import {
  isSkillDetail,
  isSkillLibraryResponse,
  isSkillUpdateResponse,
  type SkillDetail,
  type SkillLibraryResponse,
  type SkillUpdateResponse
} from "../contracts/skills";
import { normalizeConsoleSettings } from "../contracts/settings";
import { fetchJsonWithInit, patchJson } from "../httpClient";

/**
 * 读取 NearMate 管理的完整技能库状态。
 *
 * Args:
 *   baseUrl: Backend 同源根地址。
 *   signal: 页面卸载或重试时取消旧请求的信号。
 *
 * Returns:
 *   已通过依赖、汇总和版本校验的技能库快照。
 */
export async function fetchSkillLibrary(
  baseUrl: string,
  signal?: AbortSignal
): Promise<SkillLibraryResponse> {
  const payload = await fetchJsonWithInit<unknown>(
    baseUrl,
    "/api/console/skills",
    signal === undefined ? undefined : { signal },
    "读取技能库失败"
  );
  if (!isSkillLibraryResponse(payload)) {
    throw new Error("读取技能库失败：响应格式不符合契约");
  }
  return payload;
}

/**
 * 按稳定 ID 读取一个 Skill 的说明和文件清单。
 *
 * Args:
 *   baseUrl: Backend 同源根地址。
 *   skillId: 产品 Skill 的稳定 ID。
 *   signal: 关闭详情时取消旧请求的信号。
 *
 * Returns:
 *   已通过完整契约校验的 Skill 详情。
 */
export async function fetchSkillDetail(
  baseUrl: string,
  skillId: string,
  signal?: AbortSignal
): Promise<SkillDetail> {
  const payload = await fetchJsonWithInit<unknown>(
    baseUrl,
    `/api/console/skills/${encodeURIComponent(skillId)}`,
    signal === undefined ? undefined : { signal },
    "读取 Skill 详情失败"
  );
  if (!isSkillDetail(payload) || payload.id !== skillId) {
    throw new Error("读取 Skill 详情失败：响应格式不符合契约");
  }
  return payload;
}

/**
 * 修改一个 Skill 对后续后台工作的启用偏好。
 *
 * Args:
 *   baseUrl: Backend 同源根地址。
 *   skillId: 产品 Skill 的稳定 ID。
 *   enabled: 用户希望保存的新开关状态。
 *   expectedRevision: 用户看到列表时的版本，用于阻止并发覆盖。
 *
 * Returns:
 *   更新后的完整 Skill 状态和新版本。
 */
export async function updateSkillEnabled(
  baseUrl: string,
  skillId: string,
  enabled: boolean,
  expectedRevision: number
): Promise<SkillUpdateResponse> {
  const payload = await patchJson<unknown>(
    baseUrl,
    `/api/console/skills/${encodeURIComponent(skillId)}`,
    {
      enabled,
      expected_revision: expectedRevision
    },
    enabled ? "开启 Skill 失败" : "关闭 Skill 失败"
  );
  if (
    !isSkillUpdateResponse(payload) ||
    payload.skill.id !== skillId ||
    payload.skill.enabled !== enabled
  ) {
    throw new Error("修改 Skill 开关失败：响应格式不符合契约");
  }
  return payload.settings === undefined
    ? payload
    : {
        ...payload,
        settings: normalizeConsoleSettings(payload.settings)
      };
}
