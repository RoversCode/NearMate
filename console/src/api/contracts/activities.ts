import { isStandardUuid } from "../../lib/uuid";

export type ActivityStatus =
  | "running"
  | "waiting_user"
  | "cancelling"
  | "completed"
  | "failed"
  | "cancelled"
  | "interrupted";

export type ActivityStageCode =
  | "preparing"
  | "loading_materials"
  | "processing"
  | "generating_result"
  | "delivering_result";

type ActivityFailureCode =
  | "configuration_invalid"
  | "authentication_failed"
  | "rate_limited"
  | "provider_unavailable"
  | "network_unavailable"
  | "request_timed_out"
  | "provider_stream_interrupted"
  | "invalid_provider_response"
  | "material_unavailable"
  | "agent_max_turns"
  | "agent_execution_error"
  | "agent_cli_crash"
  | "runtime_reconfigured"
  | "runtime_interrupted"
  | "cognitive_task_failed"
  | "cognitive_result_protocol_failed"
  | "unclassified_error";

type ActivityStagePlanEntry = {
  stage_code: ActivityStageCode;
  stage_label: string;
};

/** 五个稳定执行阶段的有序计划；Backend 是唯一权威定义，Console 只校验完整性与顺序。 */
export type ActivityStagePlan = [
  ActivityStagePlanEntry,
  ActivityStagePlanEntry,
  ActivityStagePlanEntry,
  ActivityStagePlanEntry,
  ActivityStagePlanEntry
];

export type ActivityStagePeriod = {
  stage_code: ActivityStageCode;
  stage_label: string;
  started_at: string | null;
  finished_at: string | null;
};

export type ActivityFailure = {
  stage_code: ActivityStageCode;
  stage_label: string;
  code: ActivityFailureCode;
  summary: string;
  technical_detail: string;
  suggested_action: string;
};

type AgentWorkPlanItemStatus = "pending" | "in_progress" | "completed";

export type AgentWorkPlanItem = {
  task_id: string;
  subject: string;
  active_form: string;
  status: AgentWorkPlanItemStatus;
  order: number;
};

export type AgentWorkPlan = {
  revision: number;
  items: AgentWorkPlanItem[];
};

type ActivityTurnOutput = {
  turn_index: number;
  content_text: string;
  created_at: string;
};

export type ActivityCompact = {
  activity_id: string;
  kind: string;
  type_label: string;
  status: ActivityStatus;
  goal_summary: string;
  failure_summary: string | null;
  stage_code: ActivityStageCode | null;
  stage_label: string | null;
  started_at: string | null;
  updated_at: string;
  finished_at: string | null;
  can_cancel: boolean;
};

export type ActivityDetail = ActivityCompact & {
  goal: string;
  stage_plan: ActivityStagePlan;
  stage_timeline: ActivityStagePeriod[];
  result_text: string;
  failure: ActivityFailure | null;
  failure_reason: string | null;
  work_plan: AgentWorkPlan | null;
  turn_outputs: ActivityTurnOutput[];
};

export type ActivitySnapshot = {
  revision: number;
  runtime_id: string;
  active: ActivityCompact[];
  recent: ActivityCompact[];
};

const ACTIVE_STATUSES = new Set<ActivityStatus>([
  "running",
  "waiting_user",
  "cancelling"
]);
const TERMINAL_STATUSES = new Set<ActivityStatus>([
  "completed",
  "failed",
  "cancelled",
  "interrupted"
]);
const FAILURE_STATUSES = new Set<ActivityStatus>(["failed", "interrupted"]);
const STAGE_CODES = new Set<ActivityStageCode>([
  "preparing",
  "loading_materials",
  "processing",
  "generating_result",
  "delivering_result"
]);
const FAILURE_CODES = new Set<ActivityFailureCode>([
  "configuration_invalid",
  "authentication_failed",
  "rate_limited",
  "provider_unavailable",
  "network_unavailable",
  "request_timed_out",
  "provider_stream_interrupted",
  "invalid_provider_response",
  "material_unavailable",
  "agent_max_turns",
  "agent_execution_error",
  "agent_cli_crash",
  "runtime_reconfigured",
  "runtime_interrupted",
  "cognitive_task_failed",
  "cognitive_result_protocol_failed",
  "unclassified_error"
]);
const WORK_PLAN_STATUSES = new Set<AgentWorkPlanItemStatus>([
  "pending",
  "in_progress",
  "completed"
]);
/** 阶段计划的固定顺序，用于校验详情响应没有缺失或私自换序。 */
const STAGE_PLAN_ORDER: readonly ActivityStageCode[] = [
  "preparing",
  "loading_materials",
  "processing",
  "generating_result",
  "delivering_result"
];

/**
 * 校验并解析 Console Activity 列表响应。
 *
 * Args:
 *   value: 未受信任的 HTTP JSON 响应。
 *
 * Returns:
 *   只含公开字段、active 在前且 recent 最多两百项的类型安全快照。
 *
 * Raises:
 *   Error: 响应缺少 runtime_id、revision，或包含非法状态、阶段、时间或 UUID。
 */
export function parseActivitySnapshot(value: unknown): ActivitySnapshot {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("活动列表响应格式无效");
  }
  const candidate = value as Partial<ActivitySnapshot>;
  const revision = candidate.revision;
  const runtimeId = candidate.runtime_id;
  if (
    typeof revision !== "number" ||
    !Number.isInteger(revision) ||
    revision < 0 ||
    typeof runtimeId !== "string" ||
    !isStandardUuid(runtimeId) ||
    !Array.isArray(candidate.active) ||
    !Array.isArray(candidate.recent) ||
    candidate.recent.length > 200
  ) {
    throw new Error("活动列表响应格式无效");
  }
  // 逐条白名单重建摘要，内部字段（推理、Token 等）即使混入也不会进入 Console 状态。
  const active: ActivityCompact[] = [];
  for (const item of candidate.active) {
    if (!isActivityCompact(item, ACTIVE_STATUSES)) {
      throw new Error("活动列表响应格式无效");
    }
    active.push(pickCompactFields(item));
  }
  const recent: ActivityCompact[] = [];
  for (const item of candidate.recent) {
    if (!isActivityCompact(item, TERMINAL_STATUSES)) {
      throw new Error("活动列表响应格式无效");
    }
    recent.push(pickCompactFields(item));
  }
  return {
    revision,
    runtime_id: runtimeId,
    active,
    recent
  };
}

/**
 * 校验并解析一项 Console Activity 公开详情。
 *
 * Args:
 *   value: 未受信任的 HTTP JSON 响应。
 *
 * Returns:
 *   只含公开目标、五阶段计划、真实执行计划、结果、故障报告与安全具体原因的详情。
 *
 * Raises:
 *   Error: 响应缺少公共详情字段、阶段计划不完整或故障报告非法。
 */
export function parseActivityDetail(value: unknown): ActivityDetail {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("活动详情响应格式无效");
  }
  const raw = value as Partial<ActivityDetail>;
  const allowedStatuses = raw.finished_at === null
    ? ACTIVE_STATUSES
    : TERMINAL_STATUSES;
  const stagePlan = readStagePlan(raw.stage_plan);
  const stageTimeline = readStageTimeline(raw.stage_timeline);
  const failure = readActivityFailure(raw.failure);
  const failureReason = readFailureReason(raw.failure_reason);
  const workPlan = readAgentWorkPlan(raw.work_plan);
  const turnOutputs = readActivityTurnOutputs(raw.turn_outputs);
  const requiresFailure = FAILURE_STATUSES.has(raw.status as ActivityStatus);
  const hasConsistentFailure = failure !== undefined && (
    requiresFailure
      ? failure !== null &&
        failure.summary === raw.failure_summary &&
        failure.stage_code === raw.stage_code &&
        failure.stage_label === raw.stage_label
      : failure === null
  );
  const hasConsistentFailureReason = failureReason !== undefined && (
    failure?.code === "cognitive_task_failed"
      ? failureReason !== null
      : failureReason === null
  );
  if (
    !isActivityCompact(value, allowedStatuses) ||
    typeof raw.goal !== "string" ||
    stagePlan === null ||
    stageTimeline === null ||
    !hasConsistentFailure ||
    !hasConsistentFailureReason ||
    typeof raw.result_text !== "string" ||
    workPlan === undefined ||
    turnOutputs === undefined
  ) {
    throw new Error("活动详情响应格式无效");
  }
  return {
    ...pickCompactFields(value),
    goal: raw.goal,
    stage_plan: stagePlan,
    stage_timeline: stageTimeline,
    result_text: raw.result_text,
    failure,
    failure_reason: failureReason,
    work_plan: workPlan,
    turn_outputs: turnOutputs
  };
}

/**
 * 读取 Cognitive 自主失败时允许公开的具体原因。
 *
 * Args:
 *   value: Backend Activity 详情中的 failure_reason 字段。
 *
 * Returns:
 *   非空且不超过 Cognitive 合同上限的原因；其他失败返回空；非法输入返回 undefined。
 */
function readFailureReason(value: unknown): string | null | undefined {
  if (value === null) {
    return null;
  }
  if (
    typeof value !== "string" ||
    value.trim().length === 0 ||
    value.length > 2_000
  ) {
    return undefined;
  }
  return value;
}

/**
 * 严格读取后台 Agent 的纯文本 Turn 输出。
 *
 * Args:
 *   value: 未受信任的详情字段。
 *
 * Returns:
 *   按 turn_index 排序的输出列表；旧详情缺少该字段时返回空列表。
 */
function readActivityTurnOutputs(value: unknown): ActivityTurnOutput[] | undefined {
  if (value === undefined) {
    return [];
  }
  if (!Array.isArray(value)) {
    return undefined;
  }
  const outputs: ActivityTurnOutput[] = [];
  let previousIndex = 0;
  for (const item of value) {
    if (!item || typeof item !== "object" || Array.isArray(item)) {
      return undefined;
    }
    const rawOutput = item as Partial<ActivityTurnOutput>;
    if (
      Object.keys(item).length !== 3 ||
      typeof rawOutput.turn_index !== "number" ||
      !Number.isInteger(rawOutput.turn_index) ||
      rawOutput.turn_index <= previousIndex ||
      typeof rawOutput.content_text !== "string" ||
      rawOutput.content_text.trim().length === 0 ||
      typeof rawOutput.created_at !== "string" ||
      !isIsoDate(rawOutput.created_at)
    ) {
      return undefined;
    }
    previousIndex = rawOutput.turn_index;
    outputs.push({
      turn_index: rawOutput.turn_index,
      content_text: rawOutput.content_text,
      created_at: rawOutput.created_at
    });
  }
  return outputs;
}

/**
 * 严格读取 Background Agent 已确认的真实执行计划。
 *
 * Args:
 *   value: 未受信任的 work_plan 字段。
 *
 * Returns:
 *   合法计划；没有计划时返回空；结构非法时返回 undefined 供调用方拒绝。
 */
function readAgentWorkPlan(value: unknown): AgentWorkPlan | null | undefined {
  if (value === null) {
    return null;
  }
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return undefined;
  }
  const rawPlan = value as Partial<AgentWorkPlan>;
  if (
    Object.keys(value).length !== 2 ||
    !("revision" in value) ||
    !("items" in value) ||
    typeof rawPlan.revision !== "number" ||
    !Number.isInteger(rawPlan.revision) ||
    rawPlan.revision <= 0 ||
    !Array.isArray(rawPlan.items) ||
    rawPlan.items.length > 200
  ) {
    return undefined;
  }

  const items: AgentWorkPlanItem[] = [];
  const taskIds = new Set<string>();
  for (const [index, valueItem] of rawPlan.items.entries()) {
    if (!valueItem || typeof valueItem !== "object" || Array.isArray(valueItem)) {
      return undefined;
    }
    const rawItem = valueItem as Partial<AgentWorkPlanItem>;
    if (
      Object.keys(valueItem).length !== 5 ||
      typeof rawItem.task_id !== "string" ||
      rawItem.task_id.trim().length === 0 ||
      rawItem.task_id.length > 200 ||
      taskIds.has(rawItem.task_id) ||
      typeof rawItem.subject !== "string" ||
      rawItem.subject.trim().length === 0 ||
      rawItem.subject.length > 500 ||
      typeof rawItem.active_form !== "string" ||
      rawItem.active_form.trim().length === 0 ||
      rawItem.active_form.length > 500 ||
      typeof rawItem.status !== "string" ||
      !WORK_PLAN_STATUSES.has(rawItem.status as AgentWorkPlanItemStatus) ||
      rawItem.order !== index + 1
    ) {
      return undefined;
    }
    taskIds.add(rawItem.task_id);
    items.push({
      task_id: rawItem.task_id,
      subject: rawItem.subject,
      active_form: rawItem.active_form,
      status: rawItem.status as AgentWorkPlanItemStatus,
      order: rawItem.order
    });
  }
  return { revision: rawPlan.revision, items };
}

/**
 * 从已通过校验的值中摘取公开摘要字段。
 *
 * Args:
 *   item: 已通过 isActivityCompact 校验的值。
 *
 * Returns:
 *   只含公开字段的 Activity 摘要。
 */
function pickCompactFields(item: ActivityCompact): ActivityCompact {
  return {
    activity_id: item.activity_id,
    kind: item.kind,
    type_label: item.type_label,
    status: item.status,
    goal_summary: item.goal_summary,
    failure_summary: item.failure_summary,
    stage_code: item.stage_code,
    stage_label: item.stage_label,
    started_at: item.started_at,
    updated_at: item.updated_at,
    finished_at: item.finished_at,
    can_cancel: item.can_cancel
  };
}

/**
 * 校验指定状态分区中的公共 Activity 摘要。
 *
 * Args:
 *   value: 未受信任的摘要值。
 *   allowedStatuses: 当前 active/recent 分区允许的状态。
 *
 * Returns:
 *   字段、时间、阶段和状态语义全部合法时返回真。
 */
function isActivityCompact(
  value: unknown,
  allowedStatuses: ReadonlySet<ActivityStatus>
): value is ActivityCompact {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return false;
  }
  const item = value as Partial<ActivityCompact>;
  const isActive = ACTIVE_STATUSES.has(item.status as ActivityStatus);
  const startedAtIsValid = isActive
    ? typeof item.started_at === "string" && isIsoDate(item.started_at)
    : item.started_at === null
      || (typeof item.started_at === "string" && isIsoDate(item.started_at));
  return (
    typeof item.activity_id === "string" &&
    isStandardUuid(item.activity_id) &&
    typeof item.kind === "string" &&
    item.kind.trim().length > 0 &&
    typeof item.type_label === "string" &&
    item.type_label.trim().length > 0 &&
    typeof item.status === "string" &&
    allowedStatuses.has(item.status) &&
    typeof item.goal_summary === "string" &&
    isFailureSummary(item.status, item.failure_summary) &&
    isStageRef(item.stage_code, item.stage_label) &&
    startedAtIsValid &&
    typeof item.updated_at === "string" &&
    isIsoDate(item.updated_at) &&
    (isActive
      ? item.finished_at === null
      : typeof item.finished_at === "string" && isIsoDate(item.finished_at)) &&
    typeof item.can_cancel === "boolean" &&
    (!TERMINAL_STATUSES.has(item.status) || !item.can_cancel)
  );
}

/**
 * 校验失败摘要只存在于失败和意外中断终态。
 *
 * Args:
 *   status: 当前 Activity 状态。
 *   value: 未受信任的失败摘要。
 *
 * Returns:
 *   失败类终态携带非空摘要、其他状态携带空值时返回真。
 */
function isFailureSummary(status: ActivityStatus, value: unknown): boolean {
  if (FAILURE_STATUSES.has(status)) {
    return typeof value === "string" && value.trim().length > 0;
  }
  return value === null;
}

/**
 * 校验当前阶段代码与文案的配对合法性。
 *
 * Args:
 *   stageCode: 未受信任的阶段代码。
 *   stageLabel: 未受信任的阶段文案。
 *
 * Returns:
 *   两者同时为空，或代码属于五阶段且文案非空时返回真。
 */
function isStageRef(stageCode: unknown, stageLabel: unknown): boolean {
  if (stageCode === null && stageLabel === null) {
    return true;
  }
  return (
    typeof stageCode === "string" &&
    STAGE_CODES.has(stageCode as ActivityStageCode) &&
    typeof stageLabel === "string" &&
    stageLabel.trim().length > 0
  );
}

/**
 * 校验详情中的五阶段计划必须完整且顺序固定。
 *
 * Args:
 *   value: 未受信任的阶段计划。
 *
 * Returns:
 *   恰好五个阶段且顺序与权威定义一致时的类型安全计划；否则为空。
 */
function readStagePlan(value: unknown): ActivityStagePlan | null {
  if (!Array.isArray(value) || value.length !== STAGE_PLAN_ORDER.length) {
    return null;
  }
  const entries: ActivityStagePlanEntry[] = [];
  for (const [index, item] of value.entries()) {
    if (!item || typeof item !== "object" || Array.isArray(item)) {
      return null;
    }
    const entry = item as Partial<ActivityStagePlanEntry>;
    if (
      entry.stage_code !== STAGE_PLAN_ORDER[index] ||
      typeof entry.stage_label !== "string" ||
      entry.stage_label.trim().length === 0
    ) {
      return null;
    }
    entries.push({
      stage_code: STAGE_PLAN_ORDER[index],
      stage_label: entry.stage_label
    });
  }
  return entries as unknown as ActivityStagePlan;
}

/**
 * 校验实际进入过的阶段时间线。
 *
 * Args:
 *   value: 未受信任的阶段时间线。
 *
 * Returns:
 *   每项阶段代码、文案与时间均合法时的类型安全时间线；否则为空。
 */
function readStageTimeline(value: unknown): ActivityStagePeriod[] | null {
  if (!Array.isArray(value)) {
    return null;
  }
  const periods: ActivityStagePeriod[] = [];
  for (const item of value) {
    if (!item || typeof item !== "object" || Array.isArray(item)) {
      return null;
    }
    const period = item as Partial<ActivityStagePeriod>;
    if (
      typeof period.stage_code !== "string" ||
      !STAGE_CODES.has(period.stage_code) ||
      typeof period.stage_label !== "string" ||
      period.stage_label.trim().length === 0 ||
      typeof period.started_at !== "string" ||
      !isIsoDate(period.started_at) ||
      (period.finished_at !== null &&
        (typeof period.finished_at !== "string" || !isIsoDate(period.finished_at)))
    ) {
      return null;
    }
    periods.push({
      stage_code: period.stage_code,
      stage_label: period.stage_label,
      started_at: period.started_at,
      finished_at: period.finished_at
    });
  }
  return periods;
}

/**
 * 校验结构化故障报告。
 *
 * Args:
 *   value: 未受信任的 failure 字段。
 *
 * Returns:
 *   合法故障报告；输入为空时返回空；结构非法时返回 undefined 供调用方拒绝。
 */
function readActivityFailure(value: unknown): ActivityFailure | null | undefined {
  if (value === null) {
    return null;
  }
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return undefined;
  }
  const failure = value as Partial<ActivityFailure>;
  if (
    typeof failure.stage_code !== "string" ||
    !STAGE_CODES.has(failure.stage_code) ||
    typeof failure.stage_label !== "string" ||
    failure.stage_label.trim().length === 0 ||
    typeof failure.code !== "string" ||
    !FAILURE_CODES.has(failure.code) ||
    typeof failure.summary !== "string" ||
    failure.summary.trim().length === 0 ||
    typeof failure.technical_detail !== "string" ||
    typeof failure.suggested_action !== "string" ||
    failure.suggested_action.trim().length === 0
  ) {
    return undefined;
  }
  return {
    stage_code: failure.stage_code,
    stage_label: failure.stage_label,
    code: failure.code,
    summary: failure.summary,
    technical_detail: failure.technical_detail,
    suggested_action: failure.suggested_action
  };
}

/**
 * 判断字符串是否是可解析的非空 ISO 时间。
 *
 * Args:
 *   value: 待校验时间字符串。
 *
 * Returns:
 *   浏览器能解析该非空时间时返回真。
 */
function isIsoDate(value: string): boolean {
  return value.trim().length > 0 && Number.isFinite(Date.parse(value));
}
