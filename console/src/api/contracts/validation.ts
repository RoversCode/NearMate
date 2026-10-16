/**
 * 判断未知值是否为普通 JSON 对象。
 *
 * Args:
 *   value: 待校验的未知值。
 *
 * Returns:
 *   若值是非数组对象则返回 true。
 */
export function isPlainObject(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

/**
 * 判断未知值是否为字符串数组。
 *
 * Args:
 *   value: 待校验的未知值。
 *
 * Returns:
 *   若值是仅包含字符串的数组则返回 true。
 */
export function isStringArray(value: unknown): value is string[] {
  return Array.isArray(value) && value.every((item) => typeof item === "string");
}

/**
 * 判断未知值是否为可空字符串。
 *
 * Args:
 *   value: 待校验的未知值。
 *
 * Returns:
 *   若值是 null 或字符串则返回 true。
 */
export function isNullableString(value: unknown): value is string | null {
  return value === null || typeof value === "string";
}

/**
 * 判断未知数值是否为非负 JavaScript 安全整数。
 *
 * Args:
 *   value: 从 JSON 响应中读取、尚未信任的数值。
 *
 * Returns:
 *   数值可以被 JavaScript 无损表示且不小于零时返回 true。
 */
export function isNonNegativeSafeInteger(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0;
}
