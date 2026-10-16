import type { ActivitySnapshot, TimedReminderSnapshot } from "../types/backendEvents";
import {
  shouldAcceptActivitySnapshot
} from "../lib/activitySnapshotAcceptance";
import {
  shouldAcceptTimedReminderSnapshot
} from "../lib/timedReminderSnapshotAcceptance";

export const EMPTY_ACTIVITY_SNAPSHOT: ActivitySnapshot = {
  revision: 0,
  runtime_id: "",
  active: [],
  recent: []
};

export const EMPTY_REMINDER_SNAPSHOT: TimedReminderSnapshot = {
  revision: 0,
  runtime_id: "",
  active: [],
  recent: []
};

export type ChatEnabledSkill = {
  id: string;
  name: string;
  ready: boolean;
  usable: boolean;
};

/**
 * 判断新的权威工作区快照是否可以接管当前状态。
 *
 * Args:
 *   currentActivity: 当前 Activity 快照。
 *   nextActivity: Backend 推送的新快照。
 *   currentReminder: 当前提醒快照。
 *   nextReminder: Backend 推送的新快照。
 *
 * Returns:
 *   两类快照都通过 runtime/revision 代际门禁时返回 true。
 */
export function canAcceptWorkspaceSnapshot(input: {
  currentActivity: ActivitySnapshot | null;
  nextActivity?: ActivitySnapshot;
  currentReminder: TimedReminderSnapshot | null;
  nextReminder?: TimedReminderSnapshot;
}): boolean {
  return (!input.nextActivity || shouldAcceptActivitySnapshot(input.currentActivity, input.nextActivity))
    && (!input.nextReminder || shouldAcceptTimedReminderSnapshot(input.currentReminder, input.nextReminder));
}

/**
 * 用权威快照清理已经完成的请求占位。
 *
 * Args:
 *   requestingIds: 正在等待 HTTP 或快照确认的编号集合。
 *   activeIds: 权威快照里仍存在的活动编号。
 *
 * Returns:
 *   只保留仍在 Backend 活跃区里的编号。
 */
export function retainActiveRequestIds(
  requestingIds: ReadonlySet<string>,
  activeIds: ReadonlySet<string>
): Set<string> {
  return new Set([...requestingIds].filter(id => activeIds.has(id)));
}

/**
 * 从受管 Skill 列表和设置偏好中投影安全展示数据。
 * 不满足最小展示合同的条目会被隐藏，避免把内部路径或技术字段泄露到聊天界面。
 *
 * Args:
 *   payload: `/api/console/skills` 的未知响应。
 *   preferences: Settings 快照里的 Skill 启停偏好。
 *
 * Returns:
 *   可直接展示的已启用 Skill 名称和状态。
 */
export function projectEnabledSkills(
  payload: unknown,
  preferences: Readonly<Record<string, boolean>>
): ChatEnabledSkill[] {
  if (!isRecord(payload) || !Array.isArray(payload.skills)) {
    return [];
  }

  const projected: ChatEnabledSkill[] = [];
  for (const item of payload.skills) {
    if (!isRecord(item)) {
      continue;
    }
    const id = readNonEmptyString(item.id);
    const name = readNonEmptyString(item.name);
    // Skill 列表只能呈现产品给用户看的稳定名称，路径、换行和过长技术字段
    // 会把内部资产信息带进聊天窗口，因此直接丢弃。
    if (
      !id
      || !/^[a-z0-9][a-z0-9._-]*$/.test(id)
      || !name
      || !isSafeSkillDisplayName(name)
      || preferences[id] !== true
      || item.enabled !== true
    ) {
      continue;
    }
    if (typeof item.ready !== "boolean" || typeof item.usable !== "boolean") {
      continue;
    }
    projected.push({ id, name, ready: item.ready, usable: item.usable });
  }
  return projected;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function readNonEmptyString(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

function isSafeSkillDisplayName(value: string): boolean {
  return value.length <= 80 && !/[\\/\r\n\t]/.test(value);
}
