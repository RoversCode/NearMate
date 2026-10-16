import {
  isNonNegativeSafeInteger,
  isPlainObject,
  isStringArray
} from "./validation";
import { isConsoleSettings, type ConsoleSettings } from "./settings";

/**
 * 描述一个 Skill 声明的外部运行依赖。
 *
 * Args:
 *   kind: 依赖类型；V1 只允许从系统 PATH 查找的可执行程序。
 *   name: 可执行程序名称，例如 ffmpeg。
 *   minimum_version: 可选最低版本；省略时只检查命令能否找到。
 */
type SkillDependency = {
  kind: "executable";
  name: string;
  minimum_version?: string;
};

/**
 * 描述 Backend 对一个外部依赖完成预检后的结果。
 *
 * Args:
 *   ready: 当前机器是否已经满足该依赖。
 *   path: 找到的可执行程序路径；未找到时为空。
 *   detected_version: 检测到的版本；未要求或无法读取时为空。
 *   reason: 依赖不可用的具体原因；可用时为空。
 */
export type SkillDependencyStatus = SkillDependency & {
  ready: boolean;
  path: string | null;
  detected_version: string | null;
  reason: string | null;
};

/**
 * 描述 Skill 的资产来源。
 *
 * Values:
 *   builtin: NearMate 随产品发布的受管资产。
 *   user: 为未来用户自定义扩展保留的来源。
 */
export type SkillSource = "builtin" | "user";

/**
 * 描述技能库列表中一项完整、可操作的 Skill 状态。
 *
 * Args:
 *   id: 跨版本稳定的 Skill ID，也是 SDK 白名单使用的名称。
 *   name: 面向用户展示的名称。
 *   summary: 一句话能力摘要。
 *   category: 搜索和筛选使用的产品分类。
 *   version: 当前受管资产版本。
 *   default_enabled: 用户从未保存偏好时采用的默认开关。
 *   source: Skill 资产来源。
 *   dependencies: 清单声明的外部依赖。
 *   trigger_scenarios: 适合调用该 Skill 的用户场景。
 *   inputs: Skill 可以处理的输入。
 *   outputs: Skill 预期产生的结果。
 *   enabled: 用户保存的全局启用偏好。
 *   ready: 当前机器是否满足全部运行依赖。
 *   usable: 是否会进入下一项后台工作的 Skill 快照。
 *   unavailable_reason: 当前不可用的首要原因；可用时为空。
 *   dependency_status: 与 dependencies 顺序对应的实时预检结果。
 */
export type Skill = {
  id: string;
  name: string;
  summary: string;
  category: string;
  version: string;
  default_enabled: boolean;
  source: SkillSource;
  dependencies: SkillDependency[];
  trigger_scenarios: string[];
  inputs: string[];
  outputs: string[];
  enabled: boolean;
  ready: boolean;
  usable: boolean;
  unavailable_reason: string | null;
  dependency_status: SkillDependencyStatus[];
};

/**
 * 描述一个因清单损坏而未进入技能库的目录。
 *
 * Args:
 *   name: 出问题的 Skill 目录名，不包含本机绝对路径。
 *   message: 清单无法加载的具体原因。
 */
type SkillLibraryIssue = {
  name: string;
  message: string;
};

/**
 * 描述技能库顶部展示的四项数量。
 *
 * Args:
 *   total: 合法 Skill 总数。
 *   enabled: 用户偏好为开启的数量。
 *   ready: 当前依赖全部就绪的数量。
 *   usable: 同时开启且依赖就绪、会进入下一任务的数量。
 */
export type SkillLibrarySummary = {
  total: number;
  enabled: number;
  ready: number;
  usable: number;
};

/**
 * 描述一次技能库列表查询的权威快照。
 *
 * Args:
 *   skills: 按 Backend 稳定顺序返回的 Skill 状态。
 *   revision: 开关提交使用的乐观锁版本。
 *   issues: 被隔离、没有进入 skills 的坏清单摘要。
 *   summary: 必须与 skills 实际状态一致的数量汇总。
 */
export type SkillLibraryResponse = {
  skills: Skill[];
  revision: number;
  issues: SkillLibraryIssue[];
  summary: SkillLibrarySummary;
};

/**
 * 描述按需加载的单项 Skill 详情。
 *
 * Args:
 *   instruction: SKILL.md 的纯文本原文，禁止作为 HTML 渲染。
 *   files: Skill 目录内的安全相对文件路径。
 *   revision: 读取详情时对应的全局 Skill 设置版本。
 */
export type SkillDetail = Skill & {
  instruction: string;
  files: string[];
  revision: number;
};

/**
 * 描述一次开关修改成功后的响应。
 *
 * Args:
 *   skill: 已保存新开关值并重新完成依赖预检的 Skill。
 *   revision: 后续开关提交必须使用的新版本。
 */
export type SkillUpdateResponse = {
  skill: Skill;
  revision: number;
  /** Skill mutation 成功时对应统一设置事务的提交点快照；旧 Backend 可省略。 */
  settings?: ConsoleSettings;
};

/**
 * 校验技能库列表响应及其汇总数字。
 *
 * Args:
 *   payload: Backend 返回但尚未信任的 JSON。
 *
 * Returns:
 *   Skill、依赖、版本和汇总数字互相一致时返回 true。
 */
export function isSkillLibraryResponse(payload: unknown): payload is SkillLibraryResponse {
  if (
    !isPlainObject(payload) ||
    !Array.isArray(payload.skills) ||
    !payload.skills.every(isSkill) ||
    !isSafeRevision(payload.revision) ||
    !Array.isArray(payload.issues) ||
    !payload.issues.every(isSkillLibraryIssue) ||
    !isSkillLibrarySummary(payload.summary)
  ) {
    return false;
  }

  const skills = payload.skills as Skill[];
  const summary = payload.summary as SkillLibrarySummary;
  return (
    new Set(skills.map(skill => skill.id)).size === skills.length &&
    summary.total === skills.length &&
    summary.enabled === skills.filter(skill => skill.enabled).length &&
    summary.ready === skills.filter(skill => skill.ready).length &&
    summary.usable === skills.filter(skill => skill.usable).length
  );
}

/**
 * 校验单个 Skill 详情响应。
 *
 * Args:
 *   payload: Backend 返回但尚未信任的 JSON。
 *
 * Returns:
 *   基础状态、说明原文、相对文件列表和版本均合法时返回 true。
 */
export function isSkillDetail(payload: unknown): payload is SkillDetail {
  if (!isPlainObject(payload) || !isSkill(payload)) {
    return false;
  }
  const detail = payload as Skill & Record<string, unknown>;
  return (
    typeof detail.instruction === "string" &&
    isStringArray(detail.files) &&
    detail.files.every(isSafeRelativeSkillPath) &&
    new Set(detail.files).size === detail.files.length &&
    isSafeRevision(detail.revision)
  );
}

/**
 * 校验 Skill 开关修改响应。
 *
 * Args:
 *   payload: Backend 返回但尚未信任的 JSON。
 *
 * Returns:
 *   包含完整 Skill 状态和新版本时返回 true。
 */
export function isSkillUpdateResponse(payload: unknown): payload is SkillUpdateResponse {
  return (
    isPlainObject(payload) &&
    isSkill(payload.skill) &&
    isSafeRevision(payload.revision) &&
    (payload.settings === undefined || isConsoleSettings(payload.settings))
  );
}

/**
 * 判断未知值是否为完整、内部自洽的 Skill 状态。
 *
 * Args:
 *   payload: 尚未信任的单项 Skill JSON。
 *
 * Returns:
 *   字段合法且 enabled、ready、usable 与依赖状态互相一致时返回 true。
 */
function isSkill(payload: unknown): payload is Skill {
  if (!isPlainObject(payload)) {
    return false;
  }
  if (
    !isSkillId(payload.id) ||
    !isNonBlankString(payload.name) ||
    !isNonBlankString(payload.summary) ||
    !isNonBlankString(payload.category) ||
    !isNonBlankString(payload.version) ||
    typeof payload.default_enabled !== "boolean" ||
    !isSkillSource(payload.source) ||
    !Array.isArray(payload.dependencies) ||
    !payload.dependencies.every(isSkillDependency) ||
    !isNonBlankStringList(payload.trigger_scenarios) ||
    !isNonBlankStringList(payload.inputs) ||
    !isNonBlankStringList(payload.outputs) ||
    typeof payload.enabled !== "boolean" ||
    typeof payload.ready !== "boolean" ||
    typeof payload.usable !== "boolean" ||
    (payload.unavailable_reason !== null && typeof payload.unavailable_reason !== "string") ||
    !Array.isArray(payload.dependency_status) ||
    !payload.dependency_status.every(isSkillDependencyStatus)
  ) {
    return false;
  }

  const dependencies = payload.dependencies as SkillDependency[];
  const dependencyStatuses = payload.dependency_status as SkillDependencyStatus[];
  return (
    payload.usable === (payload.enabled && payload.ready) &&
    payload.ready === dependencyStatuses.every(dependency => dependency.ready) &&
    (payload.ready ? payload.unavailable_reason === null : isNonBlankString(payload.unavailable_reason)) &&
    dependencies.length === dependencyStatuses.length &&
    dependencies.every((dependency, index) =>
      isSameDependency(dependency, dependencyStatuses[index])
    )
  );
}

/**
 * 判断未知值是否为没有空项或重复项的展示文本列表。
 *
 * Args:
 *   value: 待校验的场景、输入或输出列表。
 *
 * Returns:
 *   值是非空字符串组成的唯一数组时返回 true。
 */
function isNonBlankStringList(value: unknown): value is string[] {
  return (
    isStringArray(value) &&
    value.every(isNonBlankString) &&
    new Set(value).size === value.length
  );
}

/**
 * 判断字符串是否为可放入 URL 和 SDK 白名单的稳定 Skill ID。
 *
 * Args:
 *   value: 待校验的 Skill ID。
 *
 * Returns:
 *   值符合小写字母、数字、点、下划线和连字符规则时返回 true。
 */
function isSkillId(value: unknown): value is string {
  return typeof value === "string" && /^[a-z0-9][a-z0-9._-]*$/.test(value);
}

/**
 * 判断未知值是否为 V1 支持的外部命令依赖。
 *
 * Args:
 *   payload: 尚未信任的依赖声明。
 *
 * Returns:
 *   值声明了合法 executable 名称和可选最低版本时返回 true。
 */
function isSkillDependency(payload: unknown): payload is SkillDependency {
  return (
    isPlainObject(payload) &&
    payload.kind === "executable" &&
    isNonBlankString(payload.name) &&
    (payload.minimum_version === undefined || isNonBlankString(payload.minimum_version))
  );
}

/**
 * 判断未知值是否为一次依赖预检结果。
 *
 * Args:
 *   payload: 尚未信任的依赖状态。
 *
 * Returns:
 *   路径、版本、失败原因和 ready 状态互相一致时返回 true。
 */
function isSkillDependencyStatus(payload: unknown): payload is SkillDependencyStatus {
  if (!isPlainObject(payload) || !isSkillDependency(payload)) {
    return false;
  }
  const status = payload as SkillDependency & Record<string, unknown>;
  return (
    typeof status.ready === "boolean" &&
    (status.path === null || isNonBlankString(status.path)) &&
    (status.detected_version === null || isNonBlankString(status.detected_version)) &&
    (status.reason === null || isNonBlankString(status.reason)) &&
    (status.ready ? status.reason === null : status.reason !== null)
  );
}

/**
 * 判断清单声明和预检结果是否描述同一个依赖。
 *
 * Args:
 *   dependency: manifest 中的依赖声明。
 *   status: Backend 返回的同位置预检结果。
 *
 * Returns:
 *   类型、名称和最低版本完全一致时返回 true。
 */
function isSameDependency(dependency: SkillDependency, status: SkillDependencyStatus): boolean {
  return (
    dependency.kind === status.kind &&
    dependency.name === status.name &&
    dependency.minimum_version === status.minimum_version
  );
}

/**
 * 判断未知值是否为合法的清单错误摘要。
 *
 * Args:
 *   payload: 尚未信任的清单问题。
 *
 * Returns:
 *   目录名和错误原因都不是空字符串时返回 true。
 */
function isSkillLibraryIssue(payload: unknown): payload is SkillLibraryIssue {
  return (
    isPlainObject(payload) &&
    isNonBlankString(payload.name) &&
    isNonBlankString(payload.message)
  );
}

/**
 * 判断未知值是否为内部计数合理的技能库汇总。
 *
 * Args:
 *   payload: 尚未信任的汇总对象。
 *
 * Returns:
 *   四项都是非负安全整数且子集数量不超过总数时返回 true。
 */
function isSkillLibrarySummary(payload: unknown): payload is SkillLibrarySummary {
  return (
    isPlainObject(payload) &&
    isNonNegativeSafeInteger(payload.total) &&
    isNonNegativeSafeInteger(payload.enabled) &&
    isNonNegativeSafeInteger(payload.ready) &&
    isNonNegativeSafeInteger(payload.usable) &&
    payload.enabled <= payload.total &&
    payload.ready <= payload.total &&
    payload.usable <= payload.enabled &&
    payload.usable <= payload.ready
  );
}

/**
 * 判断来源是否属于 Console 已知范围。
 *
 * Args:
 *   value: 待校验的来源代码。
 *
 * Returns:
 *   值是 builtin 或 user 时返回 true。
 */
function isSkillSource(value: unknown): value is SkillSource {
  return value === "builtin" || value === "user";
}

/**
 * 判断字符串去除空白后是否仍有内容。
 *
 * Args:
 *   value: 待校验的未知值。
 *
 * Returns:
 *   值是非空白字符串时返回 true。
 */
function isNonBlankString(value: unknown): value is string {
  return typeof value === "string" && value.trim().length > 0;
}

/**
 * 判断数值是否能作为跨 JSON 往返的非负版本号。
 *
 * Args:
 *   value: 待校验的版本值。
 *
 * Returns:
 *   值可以被 JavaScript 无损表示时返回 true。
 */
function isSafeRevision(value: unknown): value is number {
  return isNonNegativeSafeInteger(value);
}

/**
 * 判断详情文件名是否为 Skill 根目录内的安全相对路径。
 *
 * Args:
 *   value: Backend 返回的相对文件路径。
 *
 * Returns:
 *   路径非空、不是绝对路径且没有父目录跳转时返回 true。
 */
function isSafeRelativeSkillPath(value: string): boolean {
  if (
    !value.trim() ||
    value.startsWith("/") ||
    value.startsWith("\\") ||
    /^[A-Za-z]:/.test(value)
  ) {
    return false;
  }
  return !value.split(/[\\/]/).some(part => part === "..");
}
