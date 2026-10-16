const STANDARD_UUID_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

/**
 * 判断字符串是否是 RFC 变体且版本为 v1-v8 的标准 UUID。
 *
 * Args:
 *   value: 待校验字符串。
 *
 * Returns:
 *   版本和变体位均符合公共 Activity 契约时返回真。
 */
export function isStandardUuid(value: string): boolean {
  return STANDARD_UUID_PATTERN.test(value);
}
