/**
 * 判断整段文字是否是可用的 HTTP(S) URL。
 *
 * @param value 需要检查的完整文字。
 * @returns 文字能被完整解析为 HTTP(S) URL 时返回 true。
 */
export function isHttpUrl(value: string): boolean {
  return canonicalHttpUrl(value) !== null;
}

/**
 * 比较两个 HTTP(S) URL 是否指向同一个目标。
 *
 * 这个函数只用于分类判断，绝不能用返回的规范化结果改写用户材料。
 *
 * @param left 第一个待比较 URL。
 * @param right 第二个待比较 URL。
 * @returns 两边都是合法 HTTP(S) URL 且规范化结果相同时返回 true。
 */
export function areEquivalentHttpUrls(left: string, right: string): boolean {
  const canonicalLeft = canonicalHttpUrl(left);
  const canonicalRight = canonicalHttpUrl(right);
  return canonicalLeft !== null && canonicalLeft === canonicalRight;
}

/**
 * 生成仅供内部比较使用的 URL 字符串。
 *
 * @param value 可能包含 URL 的文字。
 * @returns 合法 HTTP(S) URL 的平台标准形式；非法时返回 null。
 */
function canonicalHttpUrl(value: string): string | null {
  const candidate = value.trim();
  if (!candidate || /\s/.test(candidate)) return null;

  try {
    const parsed = new URL(candidate);
    if (parsed.protocol !== "http:" && parsed.protocol !== "https:") return null;
    return parsed.href;
  } catch {
    return null;
  }
}
