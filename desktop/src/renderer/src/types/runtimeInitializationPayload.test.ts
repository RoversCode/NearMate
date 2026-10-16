import { describe, expect, it } from "vitest";
import { parseBackendEvent } from "./backendEvents";

const runtime = {
  runtime_id: "11111111-1111-4111-8111-111111111111",
  operation_id: "22222222-2222-4222-8222-222222222222",
  revision: 3,
  state: "initializing",
  phase: "input_models",
  message: "正在准备语音输入模型",
  retryable: false
};
describe("运行时初始化公共事件", () => {
  it("接受独立的运行时初始化快照", () => {
    for (const event of [
      { type: "runtime.initialization_changed", payload: runtime },
    ]) {
      expect(parseBackendEvent(event)).toEqual({ ok: true, event });
    }
  });

  it.each([
    ["runtime_id", "bad-id"], ["operation_id", "bad-id"],
    ["revision", -1], ["revision", Number.MAX_SAFE_INTEGER + 1], ["revision", "3"],
    ["state", "starting"], ["phase", "unknown"], ["phase", "ready"], ["message", 1], ["message", " "], ["retryable", "false"], ["retryable", true], ["operation_id", null]
  ])("拒绝非法恢复字段 %s=%s", (field, value) => {
    expect(parseBackendEvent({ type: "runtime.initialization_changed", payload: { ...runtime, [field]: value } }).ok).toBe(false);
  });

  it("拒绝字段缺失和内部信息，接受没有恢复操作的空编号", () => {
    const { phase: _phase, ...missingPhase } = runtime;
    expect(parseBackendEvent({ type: "runtime.initialization_changed", payload: missingPhase }).ok).toBe(false);
    expect(parseBackendEvent({ type: "runtime.initialization_changed", payload: { ...runtime, internal_error: "secret" } }).ok).toBe(false);
    const event = { type: "runtime.initialization_changed", payload: { ...runtime, operation_id: null, state: "idle", phase: "idle" } };
    expect(parseBackendEvent(event)).toEqual({ ok: true, event });
  });
});
