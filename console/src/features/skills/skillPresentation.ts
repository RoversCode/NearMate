import type { Skill, SkillSource } from "@/api/contracts/skills";

/**
 * 把 Skill 来源代码转换成用户可读标签。
 *
 * Args:
 *   source: Backend 返回的 Skill 来源代码。
 *
 * Returns:
 *   技能库列表和详情共用的中文来源名称。
 */
export function formatSkillSource(source: SkillSource): string {
  return source === "builtin" ? "NearMate 内置" : "用户扩展";
}

/**
 * 返回 Skill 当前是否会进入下一项后台工作。
 *
 * Args:
 *   skill: 完整 Skill 状态。
 *
 * Returns:
 *   区分可加载、依赖失效和用户关闭的简短说明。
 */
export function formatSkillUsageState(skill: Skill): string {
  if (skill.usable) {
    return "下一项任务可用";
  }
  if (skill.enabled) {
    return "偏好已开启，当前不加载";
  }
  return "已关闭";
}

/**
 * 返回依赖状态的简短标签。
 *
 * Args:
 *   skill: 完整 Skill 状态。
 *
 * Returns:
 *   无依赖、依赖就绪或依赖缺失三种结果。
 */
export function formatSkillDependencyState(skill: Skill): string {
  if (skill.dependencies.length === 0) {
    return "无需外部依赖";
  }
  return skill.ready ? "依赖就绪" : "依赖缺失";
}
