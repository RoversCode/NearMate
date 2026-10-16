import { isStandardUuid } from "../../../shared/uuid";
import type { CoreToUiEventNameByDomain } from "./eventCatalog";
import {
  hasExactKeys,
  isNonEmptyString,
  isRecord,
  isStringList,
  preservePayloadWhen,
  type BackendPayloadParser
} from "./backendEventParserPrimitives";

/** Backend 定义的五个稳定执行阶段代码；waiting_user 与 cancelling 是生命周期状态，不是阶段。 */
type ActivityStageCode =
  | "preparing"
  | "loading_materials"
  | "processing"
  | "generating_result"
  | "delivering_result";

/** 稳定产品错误码；第三方 SDK 的具体异常类型不能直接成为公共枚举。 */
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
  | "unclassified_error";

/** 失败或中断时随公共事件携带的结构化故障报告；所有字段已由 Backend 完成脱敏。 */
type ActivityFailure = {
  stage_code: ActivityStageCode;
  stage_label: string;
  code: ActivityFailureCode;
  summary: string;
  technical_detail: string;
  suggested_action: string;
};

type TaskResultBasePayload = {
  activity_id: string;
  interaction_session_id: string;
  goal: string;
  created_at: string | number;
};

type TaskResultStartedEvent = {
  type: "task_result.started";
  payload: TaskResultBasePayload;
};

type TaskResultCompletedEvent = {
  type: "task_result.completed";
  payload: TaskResultBasePayload & {
    display_content: string;
    source_materials: string[];
    output_paths?: string[];
  };
};

type TaskResultFailedEvent = {
  type: "task_result.failed";
  payload: TaskResultBasePayload & {
    failure: ActivityFailure;
    source_materials: string[];
  };
};

type TaskResultCancelledEvent = {
  type: "task_result.cancelled";
  payload: TaskResultBasePayload & {
    message: string;
    reason: string;
    source_materials: string[];
  };
};

type BackgroundAgentInteractionBasePayload = {
  request_id: string;
  activity_id: string;
  interaction_session_id: string;
  tool_name: string;
  input_data: Record<string, unknown>;
  questions: Array<Record<string, unknown>>;
};

type BackgroundAgentPermissionPayload = BackgroundAgentInteractionBasePayload & {
  kind: "permission";
  action_summary: string;
  grant_scope: "activity";
};

type BackgroundAgentQuestionPayload = BackgroundAgentInteractionBasePayload & {
  kind: "question";
};

type BackgroundAgentInteractionPayload =
  | BackgroundAgentPermissionPayload
  | BackgroundAgentQuestionPayload;

type BackgroundAgentInteractionRequestedEvent = {
  type: "background_agent.interaction_requested";
  payload: BackgroundAgentInteractionPayload;
};

type BackgroundAgentInteractionsSnapshotEvent = {
  type: "background_agent.interactions_snapshot";
  payload: {
    items: Array<BackgroundAgentInteractionPayload & { status: "pending" | "processing" }>;
  };
};

type BackgroundAgentInteractionResolvedEvent = {
  type: "background_agent.interaction_resolved";
  payload: {
    request_id: string;
    activity_id: string;
    outcome: "allowed" | "denied" | "answered" | "cancelled";
  };
};

type BackgroundAgentInteractionSubmittedEvent = {
  type: "background_agent.interaction_submitted";
  payload: {
    request_id: string;
    activity_id: string;
  };
};

type ActivityStatus =
  | "running"
  | "waiting_user"
  | "cancelling"
  | "completed"
  | "failed"
  | "cancelled"
  | "interrupted";

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

export type ActivitySnapshot = {
  revision: number;
  runtime_id: string;
  active: ActivityCompact[];
  recent: ActivityCompact[];
};

type ActivitiesSnapshotChangedEvent = {
  type: "activities.snapshot_changed";
  payload: ActivitySnapshot;
};

export type ActivityBackendEvent =
  | TaskResultStartedEvent
  | TaskResultCompletedEvent
  | TaskResultFailedEvent
  | TaskResultCancelledEvent
  | BackgroundAgentInteractionsSnapshotEvent
  | BackgroundAgentInteractionRequestedEvent
  | BackgroundAgentInteractionSubmittedEvent
  | BackgroundAgentInteractionResolvedEvent
  | ActivitiesSnapshotChangedEvent;

type ActivityEventName = CoreToUiEventNameByDomain<"activity">;

export const ACTIVITY_EVENT_PARSERS = {
  "task_result.started": payload => preservePayloadWhen(
    payload,
    value => isTaskResultPayload("task_result.started", value)
  ),
  "task_result.completed": payload => preservePayloadWhen(
    payload,
    value => isTaskResultPayload("task_result.completed", value)
  ),
  "task_result.failed": payload => preservePayloadWhen(
    payload,
    value => isTaskResultPayload("task_result.failed", value)
  ),
  "task_result.cancelled": payload => preservePayloadWhen(
    payload,
    value => isTaskResultPayload("task_result.cancelled", value)
  ),
  "background_agent.interactions_snapshot": payload => preservePayloadWhen(
    payload,
    isBackgroundAgentInteractionsSnapshotPayload
  ),
  "background_agent.interaction_requested": payload => preservePayloadWhen(
    payload,
    value => isBackgroundAgentInteractionPayload("background_agent.interaction_requested", value)
  ),
  "background_agent.interaction_submitted": payload => preservePayloadWhen(
    payload,
    value => isBackgroundAgentInteractionPayload("background_agent.interaction_submitted", value)
  ),
  "background_agent.interaction_resolved": payload => preservePayloadWhen(
    payload,
    value => isBackgroundAgentInteractionPayload("background_agent.interaction_resolved", value)
  ),
  "activities.snapshot_changed": payload => preservePayloadWhen(payload, isActivitySnapshot)
} satisfies Record<ActivityEventName, BackendPayloadParser>;

const ACTIVITY_STATUSES = new Set<ActivityStatus>([
  "running",
  "waiting_user",
  "cancelling",
  "completed",
  "failed",
  "cancelled",
  "interrupted"
]);
const ACTIVE_ACTIVITY_STATUSES = new Set<ActivityStatus>([
  "running",
  "waiting_user",
  "cancelling"
]);
const TERMINAL_ACTIVITY_STATUSES = new Set<ActivityStatus>([
  "completed",
  "failed",
  "cancelled",
  "interrupted"
]);
const FAILURE_ACTIVITY_STATUSES = new Set<ActivityStatus>(["failed", "interrupted"]);
const ACTIVITY_STAGE_CODES = new Set<ActivityStageCode>([
  "preparing",
  "loading_materials",
  "processing",
  "generating_result",
  "delivering_result"
]);
const ACTIVITY_FAILURE_CODES = new Set<ActivityFailureCode>([
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
  "unclassified_error"
]);
const TASK_RESULT_BASE_KEYS = [
  "activity_id", "interaction_session_id", "goal", "created_at"
] as const;
const ACTIVITY_SNAPSHOT_KEYS = ["revision", "runtime_id", "active", "recent"] as const;
const ACTIVITY_COMPACT_KEYS = [
  "activity_id",
  "kind",
  "type_label",
  "status",
  "goal_summary",
  "failure_summary",
  "stage_code",
  "stage_label",
  "started_at",
  "updated_at",
  "finished_at",
  "can_cancel"
] as const;

/** 校验四类任务结果事件的精确字段和稳定失败结构。 */
function isTaskResultPayload(type: string, payload: unknown): boolean {
  if (!isRecord(payload)) {
    return false;
  }
  if (
    !isNonEmptyString(payload.activity_id)
    || !isNonEmptyString(payload.interaction_session_id)
    || !isNonEmptyString(payload.goal)
    || !isTaskResultEventTime(payload.created_at)
  ) {
    return false;
  }
  if (type === "task_result.started") {
    return hasExactKeys(payload, TASK_RESULT_BASE_KEYS);
  }
  if (type === "task_result.completed") {
    const completedKeys = [...TASK_RESULT_BASE_KEYS, "display_content", "source_materials"];
    return (
      (hasExactKeys(payload, completedKeys)
        || hasExactKeys(payload, [...completedKeys, "output_paths"]))
      && typeof payload.display_content === "string"
      && isStringList(payload.source_materials)
      && (payload.output_paths === undefined || isStringList(payload.output_paths))
    );
  }
  if (type === "task_result.failed") {
    return hasExactKeys(payload, [...TASK_RESULT_BASE_KEYS, "failure", "source_materials"])
      && isActivityFailure(payload.failure)
      && isStringList(payload.source_materials);
  }
  if (type === "task_result.cancelled") {
    return hasExactKeys(payload, [
      ...TASK_RESULT_BASE_KEYS, "message", "reason", "source_materials"
    ])
      && typeof payload.message === "string"
      && typeof payload.reason === "string"
      && isStringList(payload.source_materials);
  }
  return false;
}

/** 校验后台 Agent 的请求、提交和解决事件。 */
function isBackgroundAgentInteractionPayload(type: string, payload: unknown): boolean {
  if (
    !isRecord(payload)
    || !isStandardUuid(String(payload.request_id))
    || !isStandardUuid(String(payload.activity_id))
  ) {
    return false;
  }
  if (type === "background_agent.interaction_resolved") {
    return hasExactKeys(payload, ["request_id", "activity_id", "outcome"])
      && ["allowed", "denied", "answered", "cancelled"].includes(String(payload.outcome));
  }
  if (type === "background_agent.interaction_submitted") {
    return hasExactKeys(payload, ["request_id", "activity_id"]);
  }
  if (type !== "background_agent.interaction_requested") {
    return false;
  }

  const commonKeys = [
    "request_id",
    "activity_id",
    "interaction_session_id",
    "kind",
    "tool_name",
    "input_data",
    "questions"
  ];
  const expectedKeys = payload.kind === "permission"
    ? [...commonKeys, "action_summary", "grant_scope"]
    : commonKeys;
  if (
    !hasExactKeys(payload, expectedKeys)
    || !isNonEmptyString(payload.interaction_session_id)
    || !["permission", "question"].includes(String(payload.kind))
    || typeof payload.tool_name !== "string"
    || !isRecord(payload.input_data)
    || !Array.isArray(payload.questions)
    || !payload.questions.every(isBackgroundAgentQuestion)
  ) {
    return false;
  }
  return payload.kind === "question"
    || (isNonEmptyString(payload.action_summary) && payload.grant_scope === "activity");
}

/** 校验后台 Agent 当前全部待答卡片快照。 */
function isBackgroundAgentInteractionsSnapshotPayload(payload: unknown): boolean {
  return isRecord(payload)
    && hasExactKeys(payload, ["items"])
    && Array.isArray(payload.items)
    && payload.items.every(isBackgroundAgentInteractionSnapshotItem);
}

/** 校验一张待答卡片并复用请求事件契约。 */
function isBackgroundAgentInteractionSnapshotItem(value: unknown): boolean {
  if (!isRecord(value) || !["pending", "processing"].includes(String(value.status))) {
    return false;
  }
  const { status: _status, ...requestedPayload } = value;
  return isBackgroundAgentInteractionPayload(
    "background_agent.interaction_requested",
    requestedPayload
  );
}

/** 校验 Agent 提问的标题、正文、选项和多选标记。 */
function isBackgroundAgentQuestion(value: unknown): boolean {
  return isRecord(value)
    && hasExactKeys(value, ["question", "header", "options", "multiSelect"])
    && isNonEmptyString(value.question)
    && typeof value.header === "string"
    && typeof value.multiSelect === "boolean"
    && Array.isArray(value.options)
    && value.options.every(isBackgroundAgentQuestionOption);
}

/** 校验 Agent 提问中的单个选项。 */
function isBackgroundAgentQuestionOption(value: unknown): boolean {
  return isRecord(value)
    && hasExactKeys(value, ["label", "description"])
    && isNonEmptyString(value.label)
    && typeof value.description === "string";
}

/** 校验已经脱敏的 Activity 失败报告。 */
function isActivityFailure(value: unknown): value is ActivityFailure {
  return isRecord(value)
    && hasExactKeys(value, [
      "stage_code",
      "stage_label",
      "code",
      "summary",
      "technical_detail",
      "suggested_action"
    ])
    && ACTIVITY_STAGE_CODES.has(value.stage_code as ActivityStageCode)
    && isNonEmptyString(value.stage_label)
    && ACTIVITY_FAILURE_CODES.has(value.code as ActivityFailureCode)
    && isNonEmptyString(value.summary)
    && typeof value.technical_detail === "string"
    && isNonEmptyString(value.suggested_action);
}

/** 判断任务结果时间是否能形成稳定时间点。 */
function isTaskResultEventTime(value: unknown): boolean {
  return (typeof value === "number" && Number.isFinite(value))
    || (typeof value === "string" && isNonEmptyString(value) && Number.isFinite(Date.parse(value)));
}

/** 校验 Activity 权威快照及其活跃、终态分区。 */
function isActivitySnapshot(value: unknown): value is ActivitySnapshot {
  if (!isRecord(value)) {
    return false;
  }
  const candidate = value as Partial<ActivitySnapshot>;
  return hasExactKeys(value, ACTIVITY_SNAPSHOT_KEYS)
    && Number.isInteger(candidate.revision)
    && (candidate.revision ?? -1) >= 0
    && typeof candidate.runtime_id === "string"
    && isStandardUuid(candidate.runtime_id)
    && Array.isArray(candidate.active)
    && candidate.active.every(item => isActivityCompact(item, ACTIVE_ACTIVITY_STATUSES))
    && Array.isArray(candidate.recent)
    && candidate.recent.length <= 3
    && candidate.recent.every(item => isActivityCompact(item, TERMINAL_ACTIVITY_STATUSES));
}

/** 校验一条 Activity 摘要在指定生命周期分区中的合法性。 */
function isActivityCompact(
  value: unknown,
  allowedStatuses: ReadonlySet<ActivityStatus>
): value is ActivityCompact {
  if (!isRecord(value)) {
    return false;
  }
  const item = value as Partial<ActivityCompact>;
  const finishedAtIsValid = ACTIVE_ACTIVITY_STATUSES.has(item.status as ActivityStatus)
    ? item.finished_at === null
    : typeof item.finished_at === "string" && isIsoDate(item.finished_at);
  const startedAtIsValid = ACTIVE_ACTIVITY_STATUSES.has(item.status as ActivityStatus)
    ? typeof item.started_at === "string" && isIsoDate(item.started_at)
    : item.started_at === null
      || (typeof item.started_at === "string" && isIsoDate(item.started_at));
  const cancellationFlagIsValid = TERMINAL_ACTIVITY_STATUSES.has(item.status as ActivityStatus)
    ? item.can_cancel === false
    : typeof item.can_cancel === "boolean";

  return hasExactKeys(value, ACTIVITY_COMPACT_KEYS)
    && typeof item.activity_id === "string"
    && isStandardUuid(item.activity_id)
    && isNonEmptyString(item.kind)
    && isNonEmptyString(item.type_label)
    && ACTIVITY_STATUSES.has(item.status as ActivityStatus)
    && allowedStatuses.has(item.status as ActivityStatus)
    && typeof item.goal_summary === "string"
    && isActivityFailureSummary(item.status, item.failure_summary)
    && isStageRef(item.stage_code, item.stage_label)
    && startedAtIsValid
    && typeof item.updated_at === "string"
    && isIsoDate(item.updated_at)
    && finishedAtIsValid
    && cancellationFlagIsValid;
}

/** 校验失败摘要只在失败或中断状态出现。 */
function isActivityFailureSummary(status: unknown, value: unknown): boolean {
  if (FAILURE_ACTIVITY_STATUSES.has(status as ActivityStatus)) {
    return isNonEmptyString(value);
  }
  return value === null;
}

/** 校验阶段代码和阶段名称同时为空或同时有效。 */
function isStageRef(stageCode: unknown, stageLabel: unknown): boolean {
  if (stageCode === null && stageLabel === null) {
    return true;
  }
  return ACTIVITY_STAGE_CODES.has(stageCode as ActivityStageCode)
    && isNonEmptyString(stageLabel);
}

/** 判断字符串是否是可解析的时间。 */
function isIsoDate(value: string): boolean {
  return isNonEmptyString(value) && Number.isFinite(Date.parse(value));
}
