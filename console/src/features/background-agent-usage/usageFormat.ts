/** 使用中文千分位格式化 Token 或任务数。 */
export function formatUsageCount(value: number): string {
  return new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 0 }).format(value);
}

/** 把缓存命中率格式化为一位小数百分比；缺失时显示占位。 */
export function formatCacheHitRate(value: number | null): string {
  if (value === null) {
    return "—";
  }
  return new Intl.NumberFormat("zh-CN", {
    style: "percent",
    minimumFractionDigits: 1,
    maximumFractionDigits: 1
  }).format(value);
}

/** 把 SDK 美元参考成本格式化为固定四位小数；缺失时显示占位。 */
export function formatReferenceCost(value: number | null): string {
  return value === null ? "—" : `$${value.toFixed(4)}`;
}
