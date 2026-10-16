export type BackgroundAgentInteractionAction =
  | {
      requestId: string;
      kind: "permission";
      outcome: "allow" | "deny";
    }
  | {
      requestId: string;
      kind: "question";
      messageText: string;
    };

export type BackgroundAgentPermissionDecision = Extract<
  BackgroundAgentInteractionAction,
  { kind: "permission" }
>;

/**
 * 把用户在权限卡上的明确决定提交给 Backend。
 *
 * @param httpBaseUrl Backend 控制面根地址。
 * @param decision 只包含请求编号和允许或拒绝结果的权限决定。
 * @param fetchImpl 测试可替换的 HTTP 实现。
 * @returns Backend 接受权限决定后结束的 Promise。
 */
export async function submitBackgroundAgentPermissionDecision(
  httpBaseUrl: string,
  decision: BackgroundAgentPermissionDecision,
  fetchImpl: typeof fetch = fetch
): Promise<void> {
  const response = await fetchImpl(
    `${httpBaseUrl}/api/background-agent/interactions/${encodeURIComponent(decision.requestId)}/permission-decision`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ outcome: decision.outcome })
    }
  );
  if (!response.ok) {
    throw new Error(`background agent permission decision failed: ${response.status}`);
  }
}
