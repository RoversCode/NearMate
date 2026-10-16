type BackgroundAgentUsageTokenMetrics = {
  non_cached_input_tokens: number;
  cache_creation_input_tokens: number;
  cache_read_input_tokens: number;
  total_input_tokens: number;
  output_tokens: number;
  cache_hit_tokens: number;
  cache_hit_rate: number | null;
};

export type BackgroundAgentUsageTaskMetrics = BackgroundAgentUsageTokenMetrics & {
  task_count: number;
  complete_task_count: number;
  incomplete_task_count: number;
  reference_cost_usd: number | null;
  reference_cost_task_count: number;
};

export type BackgroundAgentUsageByModel = BackgroundAgentUsageTokenMetrics & {
  model: string;
  task_count: number;
  complete_task_count: number;
  incomplete_task_count: number;
};

export type BackgroundAgentUsageByDay = BackgroundAgentUsageTaskMetrics & {
  date: string;
};

export type BackgroundAgentUsageResponse = {
  from_date: string;
  to_date: string;
  summary: BackgroundAgentUsageTaskMetrics;
  by_model: BackgroundAgentUsageByModel[];
  by_day: BackgroundAgentUsageByDay[];
};

/**
 * 校验 Backend 返回的后台工作用量查询契约。
 *
 * Args:
 *   value: 尚未信任的接口响应。
 *
 * Returns:
 *   字段、计数和缓存公式均合法时返回真。
 */
export function isBackgroundAgentUsageResponse(
  value: unknown
): value is BackgroundAgentUsageResponse {
  if (!isRecord(value)) {
    return false;
  }
  if (
    !isIsoLocalDate(value.from_date) ||
    !isIsoLocalDate(value.to_date) ||
    value.to_date < value.from_date
  ) {
    return false;
  }
  if (!hasTaskMetrics(value.summary)) {
    return false;
  }
  if (!Array.isArray(value.by_model) || !value.by_model.every(hasModelMetrics)) {
    return false;
  }
  return (
    Array.isArray(value.by_day) &&
    value.by_day.every(
      item =>
        isRecord(item) &&
        typeof item.date === "string" &&
        /^\d{4}-\d{2}-\d{2}$/.test(item.date) &&
        hasTaskMetrics(item)
    )
  );
}

/** 严格校验不带时区的 YYYY-MM-DD 日期。 */
function isIsoLocalDate(value: unknown): value is string {
  if (typeof value !== "string") {
    return false;
  }
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
  if (match === null) {
    return false;
  }
  const parsed = new Date(Date.UTC(Number(match[1]), Number(match[2]) - 1, Number(match[3])));
  return (
    parsed.getUTCFullYear() === Number(match[1]) &&
    parsed.getUTCMonth() === Number(match[2]) - 1 &&
    parsed.getUTCDate() === Number(match[3])
  );
}

/** 校验带任务完整性与成本覆盖的统计对象。 */
function hasTaskMetrics(value: unknown): value is BackgroundAgentUsageTaskMetrics {
  if (!hasTokenMetrics(value) || !isRecord(value)) {
    return false;
  }
  const taskMetrics = value as BackgroundAgentUsageTokenMetrics & Record<string, unknown>;
  if (
    !isNonNegativeInteger(taskMetrics.task_count) ||
    !isNonNegativeInteger(taskMetrics.complete_task_count) ||
    !isNonNegativeInteger(taskMetrics.incomplete_task_count) ||
    taskMetrics.complete_task_count + taskMetrics.incomplete_task_count !== taskMetrics.task_count ||
    !isNonNegativeInteger(taskMetrics.reference_cost_task_count) ||
    taskMetrics.reference_cost_task_count > taskMetrics.task_count
  ) {
    return false;
  }
  return taskMetrics.reference_cost_usd === null || isNonNegativeFiniteNumber(taskMetrics.reference_cost_usd);
}

/** 校验按模型统计对象。 */
function hasModelMetrics(value: unknown): value is BackgroundAgentUsageByModel {
  if (!hasTokenMetrics(value) || !isRecord(value)) {
    return false;
  }
  const modelMetrics = value as BackgroundAgentUsageTokenMetrics & Record<string, unknown>;
  return (
    typeof modelMetrics.model === "string" &&
    modelMetrics.model.trim().length > 0 &&
    isNonNegativeInteger(modelMetrics.task_count) &&
    isNonNegativeInteger(modelMetrics.complete_task_count) &&
    isNonNegativeInteger(modelMetrics.incomplete_task_count) &&
    modelMetrics.complete_task_count + modelMetrics.incomplete_task_count === modelMetrics.task_count
  );
}

/** 校验四类 Token、总输入和缓存命中率使用同一口径。 */
function hasTokenMetrics(value: unknown): value is BackgroundAgentUsageTokenMetrics {
  if (!isRecord(value)) {
    return false;
  }
  const countFields: unknown[] = [
    value.non_cached_input_tokens,
    value.cache_creation_input_tokens,
    value.cache_read_input_tokens,
    value.total_input_tokens,
    value.output_tokens,
    value.cache_hit_tokens
  ];
  if (!countFields.every(isNonNegativeInteger)) {
    return false;
  }
  const [
    nonCachedInputTokens,
    cacheCreationInputTokens,
    cacheReadInputTokens,
    totalInputTokens,
    _outputTokens,
    cacheHitTokens
  ] = countFields as number[];
  if (
    totalInputTokens !==
      nonCachedInputTokens + cacheCreationInputTokens + cacheReadInputTokens ||
    cacheHitTokens !== cacheReadInputTokens
  ) {
    return false;
  }
  if (totalInputTokens === 0) {
    return value.cache_hit_rate === null;
  }
  return (
    isNonNegativeFiniteNumber(value.cache_hit_rate) &&
    value.cache_hit_rate <= 1
  );
}

/** 判断未知值是否为普通对象。 */
function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

/** 判断未知值是否为非负整数。 */
function isNonNegativeInteger(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0;
}

/** 判断未知值是否为非负有限数。 */
function isNonNegativeFiniteNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value) && value >= 0;
}
