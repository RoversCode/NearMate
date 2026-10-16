type FetchLike = (input: string, init?: RequestInit) => Promise<Response>;

/**
 * 请求 Backend 使用唯一 tokenizer 分类输入框粘贴文本。
 *
 * Args:
 *   httpBaseUrl: Backend 控制服务根地址。
 *   text: 本次完整粘贴文本。
 *   fetchImplementation: 可注入的 fetch，测试用于隔离网络。
 *
 * Returns:
 *   inline 表示保留为消息正文，material 表示转入材料区。
 */
export async function classifyComposerPaste(
  httpBaseUrl: string,
  text: string,
  fetchImplementation: FetchLike = globalThis.fetch.bind(globalThis)
): Promise<"inline" | "material"> {
  const response = await fetchImplementation(`${httpBaseUrl.replace(/\/+$/, "")}/api/materials/text-paste/classify`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text })
  });
  if (!response.ok) {
    throw new Error(`text paste classification failed: ${response.status}`);
  }
  const payload: unknown = await response.json();
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    throw new Error("text paste classification returned invalid payload");
  }
  const disposition = (payload as { disposition?: unknown }).disposition;
  if (disposition !== "inline" && disposition !== "material") {
    throw new Error("text paste classification returned invalid disposition");
  }
  return disposition;
}
