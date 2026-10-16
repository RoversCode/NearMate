export type BackendPayloadParser = (payload: unknown) => unknown | null;

/**
 * 判断未知值是否是可以逐字段读取的普通对象。
 *
 * @param value 尚未验证的协议值。
 * @returns 值是非数组对象时返回真。
 */
export function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

/**
 * 检查对象没有携带领域契约之外的字段。
 *
 * @param value 已确认是普通对象的载荷。
 * @param allowedKeys 当前事件允许出现的字段。
 * @returns 所有实际字段都在允许列表内时返回真。
 */
export function hasOnlyKeys(
  value: Record<string, unknown>,
  allowedKeys: readonly string[]
): boolean {
  return Object.keys(value).every(key => allowedKeys.includes(key));
}

/**
 * 检查对象字段集合与协议定义完全一致。
 *
 * @param value 已确认是普通对象的载荷。
 * @param expectedKeys 当前事件必须且只能携带的字段。
 * @returns 字段数量、名称都一致时返回真。
 */
export function hasExactKeys(
  value: Record<string, unknown>,
  expectedKeys: readonly string[]
): boolean {
  const actualKeys = Object.keys(value);
  return actualKeys.length === expectedKeys.length
    && expectedKeys.every(key => Object.hasOwn(value, key));
}

/**
 * 判断未知值是否是去掉首尾空白后仍有内容的字符串。
 *
 * @param value 尚未验证的字段值。
 * @returns 值能作为公共身份或正文时返回真。
 */
export function isNonEmptyString(value: unknown): value is string {
  return typeof value === "string" && value.trim().length > 0;
}

/**
 * 判断未知值是否是纯字符串列表。
 *
 * @param value 尚未验证的字段值。
 * @returns 数组内每一项都是字符串时返回真。
 */
export function isStringList(value: unknown): value is string[] {
  return Array.isArray(value) && value.every(item => typeof item === "string");
}

/**
 * 把布尔校验器转换成统一的载荷解析结果。
 *
 * @param payload 尚未验证的事件载荷。
 * @param predicate 当前事件的字段校验器。
 * @returns 合法时保留原载荷，非法时返回空。
 */
export function preservePayloadWhen(
  payload: unknown,
  predicate: (value: unknown) => boolean
): unknown | null {
  return predicate(payload) ? payload : null;
}
